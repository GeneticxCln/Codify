from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from engine.default_prompts import DEFAULT_PROMPTS
from engine.fs import FileSystemService, PathEscapeError
from engine.git import GitService
from engine.models import AgentRole, Event, EventType, PlanStep
from engine.providers import ProviderError
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class AgentOutputInvalid(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "agent_output_invalid"


class ReviewerRejection(AgentOutputInvalid):
    def __init__(self, message: str, reasons: list[str]):
        super().__init__(message)
        self.reasons = reasons


class AgentOrchestrator:
    def __init__(self, registry: AgentRegistryService, goals: GoalService):
        self.registry = registry
        self.goals = goals

    def _event(self, goal_id: str, step_id: str | None, type_: EventType, payload: dict) -> Event:
        return Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type=type_,
            payload=payload,
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        )

    async def run_agent(self, role: AgentRole, goal_id: str, step_id: str | None, user_prompt: str) -> Any:
        provider, cfg = self.registry.get_provider_for(role)
        system = cfg.system_prompt_override or DEFAULT_PROMPTS[role]
        self.goals.publish(self._event(
            goal_id, step_id, "agent_assigned",
            {"role": role, "provider": cfg.provider, "model": cfg.model_name},
        ))
        raw = await provider.complete(
            system_prompt=system,
            user_prompt=user_prompt,
            model=cfg.model_name,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise AgentOutputInvalid(f"{role} returned non-JSON output: {exc}") from exc


class ExecutorService:
    def __init__(
        self,
        goals: GoalService,
        workspaces: WorkspaceService,
        registry: AgentRegistryService,
        sandbox: SandboxService,
        git: GitService | None = None,
    ):
        self.goals = goals
        self.workspaces = workspaces
        self.orchestrator = AgentOrchestrator(registry, goals)
        self.sandbox = sandbox
        self.git = git or GitService()

    def _event(self, goal_id: str, step_id: str | None, type_: EventType, payload: dict) -> Event:
        return Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type=type_,
            payload=payload,
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        )

    # --- public -------------------------------------------------------

    async def run_planning(self, goal_id: str) -> None:
        goal = self.goals.get(goal_id)
        ws = self.workspaces.get(goal.workspace_id)
        prompt = f"Title: {goal.title}\nDescription:\n{goal.description}"
        try:
            out = await self.orchestrator.run_agent("planner", goal_id, None, prompt)
            steps = self._parse_steps(out)
            self._insert_steps(goal_id, steps)
            self._log(goal_id, None, "info", f"planner produced {len(steps)} steps")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            self._fail(goal_id, None, getattr(exc, "code", "agent_output_invalid"), str(exc))
            return
        self._set_status(goal_id, "PENDING", None)

    async def run_step(self, goal_id: str, step_id: str) -> None:
        goal = self.goals.get(goal_id)
        step = self._step(goal_id, step_id)
        ws = self.workspaces.get(goal.workspace_id)
        fs = FileSystemService(ws.root_path)
        self._set_step(goal_id, step, "IN_PROGRESS")
        try:
            summaries = await self._coder(goal_id, step, fs, goal.dry_run)
            await self._tester(goal_id, step, ws)
            await self._reviewer(goal_id, step, fs, summaries)
            await self._summarizer(goal_id, step, summaries, ws.root_path, goal.dry_run)
        except ReviewerRejection:
            # Step remains IN_PROGRESS with review_notes, goal is PAUSED; human retry required
            return
        except (AgentOutputInvalid, ProviderError) as exc:
            self._fail(goal_id, step_id, getattr(exc, "code", "agent_output_invalid"), str(exc))
            return
        except CommandNotAllowed as exc:
            self._fail(goal_id, step_id, exc.code, str(exc))
            return
        except PathEscapeError as exc:
            self._fail(goal_id, step_id, "path_escape", str(exc))
            return
        self._set_step(goal_id, step, "COMPLETED")

    async def retry_step(self, goal_id: str, step_id: str, expected_version: int) -> PlanStep:
        step = self._step(goal_id, step_id)
        if not (step.status == "FAILED" or (step.status == "IN_PROGRESS" and bool(step.review_notes))):
            from engine.services import ApiError
            raise ApiError(409, "step_not_retryable", f"step {step_id} is not in a retryable state (status={step.status})")
        self.goals.update_status(goal_id, expected_version, "RUNNING")
        self._reset_step(goal_id, step)
        await self.run_step(goal_id, step_id)
        return self._step(goal_id, step_id)

    # --- stages -------------------------------------------------------

    async def _coder(self, goal_id: str, step: PlanStep, fs: FileSystemService, dry_run: bool) -> list[dict]:
        ctx = "\n".join(f"- {p}: {fs.read_text(p)[:4000]}" for p in step.suggested_paths)
        out = await self.orchestrator.run_agent(
            "coder", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\nSuggested paths:\n{ctx}",
        )
        files = self._parse_files(out)
        if not files:
            self._log(goal_id, step.id, "warn", "coder returned an empty files list — no changes will be written")
        summaries = fs.apply(files, dry_run=dry_run)
        for s in summaries:
            self.goals.publish(self._event(goal_id, step.id, "diff", {"path": s["path"], "unified_diff": s["unified_diff"]}))
        self.goals.publish(self._event(
            goal_id, step.id, "file_change_summary",
            {"paths": [s["path"] for s in summaries], "dry_run": dry_run},
        ))
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="coder")
        return summaries

    async def _tester(self, goal_id: str, step: PlanStep, ws: Any) -> None:
        out = await self.orchestrator.run_agent(
            "tester", goal_id, step.id,
            f"Step: {step.title}\n{step.description}",
        )
        argv = out.get("argv")
        verdict = out.get("verdict")
        if argv is None:
            self._test_result(goal_id, step, argv, verdict, out.get("explanation"))
            self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="tester")
            return
        # first call with argv -> run once, then re-ask for verdict
        result = self.sandbox.run_command(ws.root_path, argv)
        second = await self.orchestrator.run_agent(
            "tester", goal_id, step.id,
            f"Command ran. exit_code={result['exit_code']}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}\n"
            "Now return the verdict with argv null.",
        )
        if second.get("argv") is not None:
            raise AgentOutputInvalid("tester second call must have argv null")
        self._test_result(goal_id, step, argv, second.get("verdict"), second.get("explanation"), result["exit_code"])
        self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="tester")

    def _test_result(self, goal_id: str, step: PlanStep, argv: list[str] | None, verdict: str | None, explanation: str | None, exit_code: int | None = None) -> None:
        if verdict not in ("pass", "fail", "skip"):
            raise AgentOutputInvalid(f"tester verdict invalid: {verdict!r}")
        self.goals.publish(self._event(
            goal_id, step.id, "test_result",
            {"argv": argv, "verdict": verdict, "explanation": explanation, "exit_code": exit_code},
        ))
        if verdict == "fail":
            raise AgentOutputInvalid(f"tests failed: {explanation}")

    async def _reviewer(self, goal_id: str, step: PlanStep, fs: FileSystemService, diffs: list[dict]) -> None:
        diff_lines = []
        for d in diffs:
            diff_lines.append(f"File: {d['path']} ({d['action']})")
            if d.get("unified_diff"):
                diff_lines.append(d["unified_diff"])
        diff_text = "\n".join(diff_lines) if diff_lines else "No changes proposed."

        out = await self.orchestrator.run_agent(
            "reviewer", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\n\nDiffs:\n{diff_text}",
        )
        decision = out.get("decision")
        reasons = out.get("reasons") or []
        if decision == "approve":
            self._set_step(goal_id, step, "IN_PROGRESS", last_agent_role="reviewer")
            return
        if decision == "request-changes":
            if not reasons:
                raise AgentOutputInvalid("reviewer request-changes requires >=1 reason")
            self._set_step(goal_id, step, "IN_PROGRESS", review_notes="\n".join(reasons), last_agent_role="reviewer")
            self._log(goal_id, step.id, "warn", f"reviewer requested changes: {reasons}")
            self._set_status(goal_id, "PAUSED", step.id)
            raise ReviewerRejection("reviewer requested changes; human retry required", reasons)
        raise AgentOutputInvalid(f"reviewer decision invalid: {decision!r}")

    async def _summarizer(self, goal_id: str, step: PlanStep, diffs: list[dict], root_path: str = "", dry_run: bool = False) -> None:
        changed = ", ".join(d["path"] for d in diffs) if diffs else "none"
        out = await self.orchestrator.run_agent(
            "summarizer", goal_id, step.id,
            f"Step: {step.title}\n{step.description}\nChanged files: {changed}",
        )
        summary = out.get("summary")
        commit_message = out.get("commit_message")
        if not summary or not commit_message:
            raise AgentOutputInvalid("summarizer requires summary and commit_message")
        self._set_step(goal_id, step, "IN_PROGRESS", commit_message=commit_message, last_agent_role="summarizer")
        self._log(goal_id, step.id, "info", summary)

        if not dry_run and root_path and self.git.is_git_repo(root_path):
            commit_hash = self.git.commit(root_path, commit_message)
            if commit_hash:
                self._log(goal_id, step.id, "info", f"git committed {commit_hash[:7]}: {commit_message}")

    # --- parsing ------------------------------------------------------

    def _parse_steps(self, out: Any) -> list[dict]:
        steps = (out or {}).get("steps")
        if not isinstance(steps, list) or not (1 <= len(steps) <= 20):
            raise AgentOutputInvalid("planner must return 1..20 steps")
        parsed = []
        for s in steps:
            title = s.get("title")
            desc = s.get("description")
            paths = s.get("suggested_paths") or []
            if not title or not desc:
                raise AgentOutputInvalid("planner step missing title/description")
            if not isinstance(paths, list):
                raise AgentOutputInvalid("planner suggested_paths must be a list")
            parsed.append({"title": title, "description": desc, "suggested_paths": [str(p) for p in paths]})
        return parsed

    def _parse_files(self, out: Any) -> list[dict]:
        files = (out or {}).get("files")
        if not isinstance(files, list):
            raise AgentOutputInvalid("coder must return files list")
        parsed = []
        for f in files:
            path = f.get("path")
            action = f.get("action")
            content = f.get("content")
            if not path or action not in ("create", "update", "delete"):
                raise AgentOutputInvalid("coder file entry invalid")
            if action == "delete" and content is not None:
                raise AgentOutputInvalid("coder delete must have null content")
            parsed.append({"path": path, "action": action, "content": content})
        return parsed

    # --- db helpers ---------------------------------------------------

    def _step(self, goal_id: str, step_id: str) -> PlanStep:
        for s in self.goals.steps(goal_id):
            if s.id == step_id:
                return s
        raise AgentOutputInvalid("step not found")

    def _insert_steps(self, goal_id: str, steps: list[dict]) -> None:
        for ordinal, s in enumerate(steps):
            self.goals._db.execute(
                "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status, review_notes, commit_message, last_agent_role) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, NULL)",
                (str(uuid.uuid4()), goal_id, ordinal, s["title"], s["description"], json.dumps(s["suggested_paths"])),
            )
        self.goals._db.commit()

    def _reset_step(self, goal_id: str, step: PlanStep) -> None:
        self.goals._db.execute(
            "UPDATE plan_steps SET status='PENDING', review_notes=NULL, commit_message=NULL, last_agent_role=NULL WHERE id = ?",
            (step.id,),
        )
        self.goals._db.commit()

    def _set_step(self, goal_id: str, step: PlanStep, status: str, **fields) -> None:
        cols = ["status = ?"]
        vals: list[Any] = [status]
        if "last_agent_role" in fields and fields["last_agent_role"] is not None:
            cols.append("last_agent_role = ?")
            vals.append(fields["last_agent_role"])
        for key in ("review_notes", "commit_message"):
            if key in fields:
                cols.append(f"{key} = ?")
                vals.append(fields[key])
        vals.append(step.id)
        self.goals._db.execute(f"UPDATE plan_steps SET {', '.join(cols)} WHERE id = ?", vals)
        self.goals._db.commit()
        self.goals.publish(self._event(
            goal_id, step.id, "step_status",
            {"status": status, **{k: v for k, v in fields.items() if k in ("review_notes", "commit_message")}},
        ))

    def _set_status(self, goal_id: str, status: str, step_id: str | None) -> None:
        current = self.goals.get(goal_id)
        updated = self.goals.update_status(goal_id, current.version, status)
        self.goals.publish(self._event(goal_id, step_id, "goal_status", {"status": status, "version": updated.version}))

    def _fail(self, goal_id: str, step_id: str | None, code: str, message: str) -> None:
        self.goals.publish(self._event(goal_id, step_id, "error", {"code": code, "message": message}))
        if step_id:
            try:
                step = self._step(goal_id, step_id)
                self._set_step(goal_id, step, "FAILED")
            except Exception:
                pass
        try:
            self._set_status(goal_id, "FAILED", step_id)
        except Exception:
            pass

    def _log(self, goal_id: str, step_id: str | None, level: str, message: str) -> None:
        self.goals.publish(self._event(goal_id, step_id, "log", {"level": level, "message": message}))
