"""Skills: a sequence the conductor can pick up, kept as data rather than code.

Until this existed the order the eight roles run in was compiled into
`ExecutorService.run_planning` and `run_step`. That is a reasonable place for
it to start and a bad place for it to stay: the sequence is a *decision*, the
conductor is the thing that decides, and a decision expressed as control flow
cannot be read, replaced or argued with by the model executing it.

So the sequence moves out here, as a skill. A skill is a name, a one-line
description and a body of instructions. The conductor is shown the names and
descriptions and pulls a body only when it wants one, so a long skill costs no
context until it is used.

## The rule that makes workspace skills safe

**A skill is data, not a capability.**

`<workspace>/.codify/skills/*.md` arrives with a cloned repository, which makes
its contents untrusted input — the same trust level as any other file in that
clone. So nothing in this module can change what the engine is willing to do:

- A skill cannot define a move. It can only sequence moves that already exist
  and were already offered to the conductor.
- A skill cannot widen `validate_argv`; the allowlist lives in `engine/sandbox.py`
  and no text anywhere reaches it.
- A skill cannot make the `write` move skip the approval gate, because that gate
  is checked in the move against the goal's stored status, not against anything
  the model was told.
- A body is never executed, imported or evaluated. It is a string that is handed
  to a model as a tool result, which is why the worst a hostile skill can do is
  try to talk the conductor into something it already had the power to do.

That last point is the whole design. A skill can be *wrong* — it can send the
conductor down a silly order — and being wrong is recoverable. It cannot be
*empowered*, and that is the property worth stating out loud in a file that
reads attacker-influenceable text.

## Override

Built-ins ship in `engine/builtin_skills/`. A workspace file with a built-in's
name replaces it, so a project can redefine `ship-a-change` for its own
conventions — and every replacement is reported in `SkillSet.shadows` so the
transcript can say a built-in was shadowed rather than quietly swapping out the
instructions the user thinks they are running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Where a workspace keeps its skills, relative to the workspace root.
SKILLS_DIRNAME = ".codify/skills"

# The skills that ship with Codify. Beside this module rather than in the data
# directory because they are part of the engine's behaviour, not user state.
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_skills"

# A skill name has to survive being a tool argument, a log line and a filename,
# so it is the same character class the provider slugs use.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# The same ceiling as `system_prompt_override` (docs/03 §4), for the same
# reason: a body is prompt text, and an unbounded one is a way to spend a
# context window on a single tool result.
MAX_SKILL_CHARS = 32768

# A description is one line in a menu, so it is clipped rather than rejected.
# A skill with a ten-paragraph description is still a usable skill.
MAX_DESCRIPTION_CHARS = 200

# A cap on how many skills one workspace can contribute, so a directory of
# ten thousand files cannot turn the menu into the whole prompt.
MAX_WORKSPACE_SKILLS = 100


@dataclass(frozen=True)
class Skill:
    """One skill, as loaded. `source` is "built-in" or "workspace"."""

    name: str
    description: str
    body: str
    source: str

    @property
    def is_workspace(self) -> bool:
        return self.source == "workspace"


@dataclass(frozen=True)
class SkillSet:
    """What discovery found, and what it refused.

    `problems` is not decoration. A skill written with a bad name or an empty
    body is silently absent from the menu otherwise, and the person who wrote it
    has no way to find out why the conductor never reached for it.
    """

    skills: tuple[Skill, ...]
    problems: tuple[str, ...]
    shadows: tuple[str, ...]

    def get(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def names(self) -> list[str]:
        return [s.name for s in self.skills]

    def menu(self) -> str:
        """The list the conductor is shown: name and description, never bodies.

        Deliberately just names and descriptions. The bodies are what the
        `use_skill` call is for, and a menu that inlined them would pay for
        every skill on every turn to use none of them.
        """
        if not self.skills:
            return "(no skills are available in this workspace)"
        return "\n".join(
            f"- {s.name}{' (workspace)' if s.is_workspace else ''}: {s.description}"
            for s in self.skills
        )


def _strip_header(text: str) -> tuple[dict[str, str], str]:
    """Split an optional `---` header from the body.

    A deliberately small parser rather than a YAML dependency: the header only
    ever holds `name` and `description`, both single-line, and a skill file that
    happens to contain a colon in its body must not be able to break loading.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    header: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return header, "\n".join(lines[index + 1 :])
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("name", "description"):
            header[key] = value.strip()
    # An unterminated header is not a header. The whole text is the body, which
    # is the honest reading of a file that never closed it.
    return {}, text


def _first_line(body: str) -> str:
    """The first line that could be a description: not blank, not a heading."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def parse_skill(
    text: str, fallback_name: str, source: str
) -> tuple[Skill | None, str | None]:
    """One skill file, or the reason it is not one.

    Returns `(skill, None)` or `(None, problem)`. Never raises: a malformed file
    in a checked-out repository must not be able to stop a turn, and the reason
    is returned rather than logged here so the caller decides where it belongs.
    """
    if not text.strip():
        return None, f"{fallback_name}: the file is empty"
    if len(text) > MAX_SKILL_CHARS:
        return None, (
            f"{fallback_name}: {len(text)} characters, over the "
            f"{MAX_SKILL_CHARS} limit"
        )
    header, body = _strip_header(text)
    name = (header.get("name") or fallback_name).strip().lower()
    if not NAME_RE.match(name):
        return None, (
            f"{fallback_name}: {name!r} is not a valid skill name "
            "(lowercase letters, digits and dashes, starting with a letter or digit)"
        )
    if not body.strip():
        return None, f"{name}: the file has a header but no instructions"
    description = (header.get("description") or _first_line(body)).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return (
        Skill(name=name, description=description, body=body.strip(), source=source),
        None,
    )


def _read_file(path: Path, source: str) -> tuple[Skill | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"
    except UnicodeDecodeError:
        return None, f"{path.name}: not valid UTF-8 text"
    return parse_skill(text, path.stem.lower(), source)


def _skill_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix == ".md"),
        key=lambda p: p.name,
    )


def builtin_skills() -> tuple[list[Skill], list[str]]:
    skills: list[Skill] = []
    problems: list[str] = []
    for path in _skill_files(BUILTIN_DIR):
        skill, problem = _read_file(path, "built-in")
        if skill is not None:
            skills.append(skill)
        elif problem:
            problems.append(f"built-in skill ignored — {problem}")
    return skills, problems


def workspace_skills(root: str | None) -> tuple[list[Skill], list[str]]:
    if not root:
        return [], []
    directory = Path(root) / SKILLS_DIRNAME
    files = _skill_files(directory)
    skills: list[Skill] = []
    problems: list[str] = []
    if len(files) > MAX_WORKSPACE_SKILLS:
        problems.append(
            f"{SKILLS_DIRNAME} holds {len(files)} skills; only the first "
            f"{MAX_WORKSPACE_SKILLS} were read"
        )
        files = files[:MAX_WORKSPACE_SKILLS]
    for path in files:
        skill, problem = _read_file(path, "workspace")
        if skill is not None:
            skills.append(skill)
        elif problem:
            problems.append(f"workspace skill ignored — {problem}")
    return skills, problems


def load_skills(root: str | None) -> SkillSet:
    """Every skill available for a workspace, with workspace files winning.

    Built-ins are loaded first and a workspace skill of the same name replaces
    it in place, so the menu order is stable regardless of which layer a skill
    came from and a shadowed built-in keeps its position.
    """
    builtin, builtin_problems = builtin_skills()
    workspace, workspace_problems = workspace_skills(root)

    ordered: dict[str, Skill] = {s.name: s for s in builtin}
    shadows: list[str] = []
    for skill in workspace:
        if skill.name in ordered:
            shadows.append(skill.name)
        ordered[skill.name] = skill

    return SkillSet(
        skills=tuple(ordered[name] for name in sorted(ordered)),
        problems=tuple([*builtin_problems, *workspace_problems]),
        shadows=tuple(shadows),
    )
