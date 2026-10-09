"""The error shape is declared, and the declaration is the truth.

`engine/models.py` carried an `ErrorBody` for a long time that nothing
referenced. The OpenAPI schema described successes and nothing else, so the
generated docs showed a 200 and silence, and the UI's `api.ts` had to work out
for itself whether a failure carried `code`, `message` or `detail` — which is
guesswork about a contract that has exactly one owner, forty lines away.

This module holds that owner to its promise in five ways:

* `ErrorBody` reaches the schema, with `code` and `message` required — the
  failure mode being pinned is the one it just had, a model nothing references.
* Every route declares the engine's refusals, not just the ones somebody
  remembered.
* **The freeze.** Every `ApiError(<status>` raised anywhere in `engine/` is a
  declared response. A new status fails here rather than shipping undocumented —
  the same idea as `test_no_unguarded_spawns`, for the same reason: forty-odd
  decorators each carrying their own `responses={...}` does not survive.
* Real refusals, over real HTTP, match the schema: `code` and `message`, both
  non-empty strings. The schema is a claim; this is the claim being kept.
* The 422 caveat is real rather than decorative — a malformed body really does
  come back as FastAPI's `{detail: [...]}`, which is why 422 is declared as a
  `oneOf` and not as a plain `ErrorBody`.

The engine is booted for real (its own `lifespan`, a scratch `CODIFY_HOME`) rather
than through a mocked harness, because the thing under test is the exception
handler the app actually ships.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import inspect
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from pydantic import BaseModel

import engine.models
from engine.models import GoalCreate
from engine.app import ERROR_RESPONSES, app, lifespan
from engine.providers import Keychain, ProviderError

ROOT = Path(__file__).resolve().parent.parent

# `ApiError(409, "version_conflict", …)` — a status the engine raises with its own
# body, as opposed to the ones only FastAPI produces.
_RAISED = re.compile(r"ApiError\(\s*(\d{3})\s*,")


def _statuses_raised_in_engine() -> set[int]:
    found: set[int] = set()
    for path in sorted((ROOT / "engine").glob("*.py")):
        found |= {int(m) for m in _RAISED.findall(path.read_text(encoding="utf-8"))}
    return found


class DeclaredShape(unittest.TestCase):
    """What the schema says, checked against the schema."""

    # Declared, not just assigned in `setUpClass`: mypy reads a class attribute
    # that only appears in a method body as absent.
    schema: dict[str, Any]
    schemas: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = app.openapi()
        cls.schemas = cls.schema.get("components", {}).get("schemas", {})

    def test_error_body_is_in_the_schema_at_all(self) -> None:
        """The failure this whole module exists for: declared and unreferenced.

        `ErrorBody` sat in `models.py` while every generated route documented only
        its success. Asserting the *name* is present, not merely that some error
        shape exists, is what catches a rename or a dropped reference.
        """
        self.assertIn("ErrorBody", self.schemas, "ErrorBody is not in the OpenAPI components")
        body = self.schemas["ErrorBody"]
        self.assertEqual(sorted(body["required"]), ["code", "message"])
        for field in ("code", "message"):
            self.assertEqual(body["properties"][field]["type"], "string")

    def test_a_refusal_may_carry_extra_facts(self) -> None:
        """`workspace_not_empty` attaches a goal count; the schema must allow it.

        A client that sees only `code` and `message` loses detail, never
        correctness — so `additionalProperties` is part of the contract, and
        forbidding extras would be a schema that lies about the real bodies.
        """
        self.assertIs(
            self.schemas["ErrorBody"].get("additionalProperties"), True,
            "ErrorBody forbids the extra facts a refusal attaches (a goal count, "
            "a path); the real bodies carry them",
        )

    def test_every_route_declares_the_engines_refusals(self) -> None:
        expected = {str(s) for s in ERROR_RESPONSES}
        undocumented: list[str] = []
        for path, item in self.schema["paths"].items():
            for method, operation in item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                declared = set(operation.get("responses", {}))
                missing = expected - declared
                if missing:
                    undocumented.append(f"{method.upper()} {path}: {sorted(missing)}")
        self.assertEqual(undocumented, [], "routes that do not document their refusals")

    def test_422_is_declared_as_the_one_shape_the_api_actually_returns(self) -> None:
        """A rejected body answers in the engine's shape, so 422 is a plain model.

        This used to be a `oneOf` of `ErrorBody` and FastAPI's `HTTPValidationError`,
        because the engine did not handle a rejected body and FastAPI's default
        `{"detail": [...]}` was a second, undocumented shape. Handling it
        (`request_validation_error`) removed the exception: there is now one
        declared shape for every error the API can return, and the field errors
        survive as an extra rather than being dropped to achieve that.
        """
        schema = self.schema["paths"]["/goals"]["post"]["responses"]["422"]
        ref = schema["content"]["application/json"]["schema"].get("$ref")
        self.assertEqual(
            ref, "#/components/schemas/ErrorBody",
            "422 is not the declared refusal shape; a generated client reading "
            "code/message would be wrong about it",
        )


class StatusFreeze(unittest.TestCase):
    """A status the engine raises must be a status the schema declares."""

    def test_the_extractor_found_something(self) -> None:
        """Guard the guard: a scan that matches nothing proves nothing."""
        self.assertGreaterEqual(
            len(_statuses_raised_in_engine()), 4,
            "the ApiError scan found almost nothing; the pattern is stale",
        )

    def test_every_raised_status_is_declared(self) -> None:
        raised = _statuses_raised_in_engine()
        undeclared = sorted(raised - {int(s) for s in ERROR_RESPONSES})
        self.assertEqual(
            undeclared, [],
            "the engine raises these statuses but the schema does not declare "
            "them, so their bodies are undocumented: add them to ERROR_RESPONSES",
        )

    def test_nothing_is_declared_that_is_never_raised(self) -> None:
        """The other direction, so the list cannot grow on a guess.

        401 is the exception and is declared deliberately: it is produced by the
        auth middleware, before routing, on every request (docs/00 §6.3).
        """
        raised = _statuses_raised_in_engine() | {401}
        self.assertEqual(
            sorted(int(s) for s in ERROR_RESPONSES), sorted(raised),
            "ERROR_RESPONSES and the statuses the engine raises have drifted apart",
        )


class RealRefusalsMatchTheSchema(unittest.IsolatedAsyncioTestCase):
    """The declared shape, over real HTTP, from the real handler."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.root = root
        # The workspace is a folder *inside* the scratch state directory, as a real project
        # is never the state directory itself (that root is refused: docs/03, protected roots).
        self.project = root / "project"
        self.project.mkdir()
        self.env = {
            "CODIFY_HOME": str(root),
            "CODIFY_DB": str(root / "codify.db"),
            "CODIFY_SECRETS": str(root / "secrets.json"),
        }
        self._patches = [patch.dict(os.environ, self.env)]
        for p in self._patches:
            p.start()
        self._lifespan = lifespan(app)
        await self._lifespan.__aenter__()
        self.headers = {"Authorization": f"Bearer {app.state.token}"}
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        await self.client.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.client.__aexit__(None, None, None)
        await self._lifespan.__aexit__(None, None, None)
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def _assert_declared_shape(self, status: int, body: Any, where: str) -> None:
        """What `ErrorBody` promises, checked against what arrived."""
        self.assertIsInstance(body, dict, f"{where}: error body is not an object")
        for field in ("code", "message"):
            self.assertIn(field, body, f"{where}: no {field} in the refusal body")
            self.assertIsInstance(body[field], str, f"{where}: {field} is not a string")
            self.assertTrue(body[field].strip(), f"{where}: {field} is empty")
        self.assertIn(status, ERROR_RESPONSES, f"{where}: status {status} is undeclared")

    async def _a_workspace_and_goal(self) -> tuple[str, str]:
        ws = (await self.client.post(
            "/workspaces",
            json={"name": "WS", "root_path": str(self.project)},
            headers=self.headers,
        )).json()
        goal = (await self.client.post(
            "/goals",
            json={"workspace_id": ws["id"], "title": "t", "description": "d"},
            headers=self.headers,
        )).json()
        return ws["id"], goal["id"]

    async def test_400_is_a_coded_refusal(self) -> None:
        r = await self.client.post(
            "/workspaces",
            json={"name": "nope", "root_path": str(self.root / "not-there")},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self._assert_declared_shape(400, r.json(), "POST /workspaces")

    async def test_401_is_a_coded_refusal(self) -> None:
        r = await self.client.get("/settings/agents")
        self.assertEqual(r.status_code, 401)
        self._assert_declared_shape(401, r.json(), "GET /settings/agents (no token)")

    async def test_404_is_a_coded_refusal(self) -> None:
        r = await self.client.get("/goals/does-not-exist", headers=self.headers)
        self.assertEqual(r.status_code, 404)
        self._assert_declared_shape(404, r.json(), "GET /goals/does-not-exist")

    async def test_409_is_a_coded_refusal(self) -> None:
        """A version conflict — the one the client's retry policy keys on.

        Arranged rather than stumbled on: `/start` checks the goal's status
        before its version, so a goal that never left PLANNING answers
        `illegal_status` and the version guard is never reached. The status is
        therefore put where a startable goal would be, so the refusal under test
        is the one that names the code the UI actually branches on
        (`ui/src/goalActions.ts`).
        """
        # The goal is made through the service, not `POST /goals`: that route starts real
        # planning, which with no models configured fails the goal within milliseconds, and a
        # FAILED goal cannot legitimately become PENDING (docs/04, terminal statuses). A startable
        # goal is one that planned successfully, and PLANNING -> PENDING is exactly that move.
        ws_id = (await self.client.post(
            "/workspaces", json={"name": "WS", "root_path": str(self.project)}, headers=self.headers,
        )).json()["id"]
        goal = app.state.goals.create(GoalCreate(workspace_id=ws_id, title="t"))
        goal_id = goal.id
        current = (await self.client.get(f"/goals/{goal_id}", headers=self.headers)).json()
        app.state.goals.update_status(goal_id, current["version"], "PENDING")

        r = await self.client.post(
            f"/goals/{goal_id}/start",
            json={"expected_version": current["version"] + 5},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self._assert_declared_shape(409, body, "POST /goals/{id}/start (stale version)")
        self.assertEqual(body["code"], "version_conflict")

    async def test_an_illegal_status_is_its_own_code(self) -> None:
        """Two different refusals, two different codes — a client can tell them apart.

        Both are 409, so the status alone cannot drive a decision. That is the
        whole reason `code` is part of the declared shape rather than the message.
        """
        _, goal_id = await self._a_workspace_and_goal()
        r = await self.client.post(
            f"/goals/{goal_id}/start",
            json={"expected_version": 0},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self._assert_declared_shape(409, body, "POST /goals/{id}/start (planning)")
        self.assertEqual(body["code"], "illegal_status")

    async def test_an_engine_422_is_a_coded_refusal(self) -> None:
        ws_id, goal_id = await self._a_workspace_and_goal()
        r = await self.client.get(
            "/goals", params={"status": "not-a-status"}, headers=self.headers,
        )
        self.assertEqual(r.status_code, 422)
        self._assert_declared_shape(422, r.json(), "GET /goals?status=not-a-status")
        _ = (ws_id, goal_id)

    async def test_503_is_a_coded_refusal(self) -> None:
        with patch.object(
            Keychain, "set_provider_key",
            side_effect=ProviderError("keyring_unavailable", "no Secret Service"),
        ):
            r = await self.client.post(
                "/settings/keys",
                json={"provider": "openai", "api_key": "sk"},
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 503)
        self._assert_declared_shape(503, r.json(), "POST /settings/keys (no keyring)")

    async def test_a_refusal_may_attach_the_facts_a_caller_needs(self) -> None:
        """The extras the schema permits, on a real route.

        `workspace_not_empty` is the reason `additionalProperties` is part of the
        contract: the count is what a confirm dialog names before the user agrees
        to a cascade, and a schema that forbade it would be describing a body the
        engine does not send.
        """
        ws_id, _ = await self._a_workspace_and_goal()
        r = await self.client.delete(f"/workspaces/{ws_id}", headers=self.headers)
        self.assertEqual(r.status_code, 409)
        self._assert_declared_shape(409, r.json(), "DELETE /workspaces/{id} with goals")
        self.assertEqual(r.json()["code"], "workspace_not_empty")
        self.assertIn("goals", r.json(), "the refusal must carry the goal count")
        self.assertGreaterEqual(r.json()["goals"], 1)

    async def test_a_malformed_body_answers_in_the_declared_shape(self) -> None:
        """The second shape is gone, and nothing was lost to remove it.

        FastAPI's default for a rejected body was `{"detail": [...]}`, which the
        UI could not read — its reader handles `detail` only as a string, so a
        validation failure reached the screen as a bare "HTTP 422". The field
        errors are still here, under `detail`, on a body that now also carries
        the `code` and `message` every other refusal carries.
        """
        r = await self.client.post("/workspaces", json={"name": "x"}, headers=self.headers)
        self.assertEqual(r.status_code, 422)
        body = r.json()
        self._assert_declared_shape(422, body, "POST /workspaces (missing root_path)")
        self.assertEqual(body["code"], "invalid_request")
        self.assertIn("root_path", body["message"], "the message must name the field")
        # Nothing FastAPI knew is thrown away on the way to our shape.
        self.assertIsInstance(body.get("detail"), list, "the field errors were dropped")
        located = " ".join(
            ".".join(str(p) for p in e.get("loc", [])) for e in body["detail"]
        )
        self.assertIn("root_path", located)

    async def test_a_rejection_does_not_repeat_what_was_sent(self) -> None:
        """The 422 names the fields that were wrong and the reason, never the values.

        Pydantic's error carries the rejected `input`, and for a missing field that is the *whole object around it*: a
        key sent without its provider came straight back in the response (docs/00 §6.4, "responses never include raw
        API keys"). The value is the caller's own, so this is not a disclosure to anyone else, but nothing else in the
        engine's responses repeats a request, and an echo is what ends up in a log, a bug report and a screenshot.
        """
        marker = "marker-" + os.urandom(8).hex()
        for path, method, body in (
            ("/settings/keys", "post", {"api" + "_key": marker}),
            ("/settings/keys", "post", {"provider": "openai", "api" + "_key": [marker]}),
            ("/workspaces", "post", {"name": marker}),
            ("/goals", "post", {"title": marker, "dry_run": marker}),
        ):
            with self.subTest(path=path, body=sorted(body)):
                r = await getattr(self.client, method)(path, json=body, headers=self.headers)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertNotIn(marker, r.text)
                payload = r.json()
                self._assert_declared_shape(422, payload, path)
                self.assertTrue(all("loc" in e and "msg" in e and "type" in e for e in payload["detail"]))
                self.assertFalse(any("input" in e for e in payload["detail"]))

    async def test_a_number_json_cannot_hold_is_a_422_not_a_server_error(self) -> None:
        """Python's JSON parser reads `NaN`, `Infinity` and `1e999`; the response encoder will not write them.

        The 422 repeated the rejected value, so one of those anywhere in a rejected body made the handler fail to
        render its own answer: HTTP 500, as plain text, on every route that takes a body.
        """
        ws = await self._a_workspace()
        for path, method, raw in (
            ("/goals", "post", f'{{"workspace_id": "{ws}", "title": "t", "dry_run": NaN}}'),
            ("/goals", "post", '{"workspace_id": 1e999, "title": "t"}'),
            ("/workspaces", "post", '{"name": Infinity, "root_path": "/tmp"}'),
            ("/settings/agents/fixer", "put", '{"temperature": -Infinity}'),
            ("/settings/keys", "post", '{"provider": NaN}'),
            ("/shell/tabs", "put", '[{"key": 1e999}]'),
        ):
            with self.subTest(path=path, raw=raw):
                r = await getattr(self.client, method)(
                    path, content=raw, headers={**self.headers, "content-type": "application/json"},
                )
                self.assertIn(r.status_code, (400, 422), r.text)
                self._assert_declared_shape(r.status_code, r.json(), path)

    async def test_a_number_beyond_what_the_database_holds_is_a_422_not_a_server_error(self) -> None:
        """An unbounded integer in the query reached SQLite as an `OverflowError`: a plain-text 500."""
        _, goal_id = await self._a_workspace_and_goal()
        too_big = 2**63
        for path in (
            f"/goals?offset={too_big}",
            f"/conversations?offset={too_big}",
            f"/goals/{goal_id}/events?after={too_big}",
            f"/goals/{goal_id}/events?after={-too_big - 1}",
        ):
            with self.subTest(path=path):
                r = await self.client.get(path, headers=self.headers)
                self.assertEqual(r.status_code, 422, r.text)
                self._assert_declared_shape(422, r.json(), path)
        # And the largest value the database can hold is still an ordinary request.
        for path in (
            f"/goals?offset={2**63 - 1}", f"/goals/{goal_id}/events?after={2**63 - 1}", f"/goals/{goal_id}/events?after=-1",
        ):
            r = await self.client.get(path, headers=self.headers)
            self.assertEqual(r.status_code, 200, (path, r.text))

    async def test_a_rejection_with_many_fields_still_reads(self) -> None:
        """The message is capped so it stays a sentence, and says how much it hid."""
        r = await self.client.put(
            "/settings/agents/fixer",
            json={"temperature": 99, "max_tokens": -1, "provider": "BAD SLUG",
                  "base_url": 5, "fallback_model_name": 7, "system_prompt_override": 8},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 422)
        body = r.json()
        self._assert_declared_shape(422, body, "PUT /settings/agents/fixer")
        self.assertIn("more)", body["message"], "a capped message must say it was capped")
        self.assertGreaterEqual(len(body["detail"]), 5, "every field error is kept")


    # Every custom validator in `engine/models.py`, with a request that trips it. Keyed
    # `Model.validator`; the body is built from a real workspace id. See the sweep below: a
    # validator that is not in this table fails the suite, because a validator's only failure
    # mode that a model-level test cannot see is the one in the *handler* — it enforced
    # invariant 8 for as long as it was a 500.
    VALIDATOR_REJECTIONS: dict[str, tuple[str, str, Any]] = {
        "GoalCreate._refuse_chat": (
            "POST", "/goals",
            lambda ws: {"workspace_id": ws, "title": "t", "description": "d", "mode": "chat"},
        ),
        # Half a pair names nothing to call. FastAPI validates the body before the handler looks the
        # conversation up, so the id need not exist for the refusal to be seen.
        "TurnCreate._a_pair_or_nothing": (
            "POST", "/conversations/c-none/turns",
            lambda ws: {"prompt": "hi", "provider": "openai"},
        ),
    }

    async def _a_workspace(self) -> str:
        ws = (await self.client.post(
            "/workspaces", json={"name": "WS", "root_path": str(self.project)}, headers=self.headers,
        )).json()
        return str(ws["id"])

    async def test_a_goal_that_asks_for_chat_mode_is_a_422_not_a_500(self) -> None:
        """Invariant 8 through the route, which is the only place it can be seen to hold.

        The validator raised `ValueError`; pydantic keeps that live exception in
        `errors()[i]["ctx"]["error"]`; FastAPI's own handler runs the errors through
        `jsonable_encoder` and this override did not, so the refusal that enforces
        "a turn is created only by the turns route" answered HTTP 500. The test that
        stood guard built `GoalCreate(...)` directly and never touched the route.
        """
        ws = await self._a_workspace()

        r = await self.client.post(
            "/goals", json={"workspace_id": ws, "title": "t", "description": "d", "mode": "chat"},
            headers=self.headers,
        )

        self.assertEqual(422, r.status_code)
        body = r.json()
        self._assert_declared_shape(422, body, "POST /goals mode=chat")
        self.assertEqual("invalid_request", body["code"])
        self.assertIn("mode", body["message"])
        # The validator's own sentence, the one that says where a turn *is* created, reaches
        # the caller instead of being dropped on the way to the response.
        self.assertIn("/conversations/{id}/turns", json.dumps(body["detail"]))

    async def test_a_protected_workspace_root_is_a_coded_400_over_the_route(self) -> None:
        """L3 through HTTP: the refusal a user sees is `invalid_root`, with the reason in words."""
        for root in ("/", "/etc", str(Path.home())):
            with self.subTest(root=root):
                r = await self.client.post(
                    "/workspaces", json={"name": "bad", "root_path": root}, headers=self.headers,
                )
                self.assertEqual(400, r.status_code, r.text)
                body = r.json()
                self._assert_declared_shape(400, body, f"POST /workspaces {root}")
                self.assertEqual("invalid_root", body["code"])
        listed = (await self.client.get("/workspaces", headers=self.headers)).json()
        self.assertEqual([], listed, "a refused root was stored anyway")

    async def test_a_root_the_filesystem_cannot_name_is_the_same_coded_400(self) -> None:
        """Four spellings of "not a directory" that the operating system answers with an exception, not a `False`.

        A NUL byte (`ValueError`), a `~user` that does not exist (`RuntimeError`), and a name or a path past the
        filesystem's limit (`OSError`, `ENAMETOOLONG`, which `Path.is_dir` does not swallow) each reached the route as
        an HTTP 500. `root_path` is typed in a box by a person, so it is the one string here that can be anything.
        """
        for root in ("a\x00b", "~no-such-user-zz/proj", "x" * 5000, "/" + "d/" * 3000):
            with self.subTest(root=root[:30]):
                r = await self.client.post(
                    "/workspaces", json={"name": "bad", "root_path": root}, headers=self.headers,
                )
                self.assertEqual(400, r.status_code, r.text)
                body = r.json()
                self._assert_declared_shape(400, body, "POST /workspaces with an unnameable root")
                self.assertEqual("invalid_root", body["code"])
        self.assertEqual([], (await self.client.get("/workspaces", headers=self.headers)).json())

    async def test_every_validator_has_a_route_level_rejection_and_answers_422(self) -> None:
        validators = sorted(
            f"{name}.{v}"
            for name, cls in inspect.getmembers(engine.models, inspect.isclass)
            if issubclass(cls, BaseModel) and cls.__module__ == engine.models.__name__
            for v in (
                *cls.__pydantic_decorators__.field_validators,
                *cls.__pydantic_decorators__.model_validators,
            )
        )
        self.assertTrue(validators, "the sweep found no validators: it is looking in the wrong place")
        self.assertEqual(
            sorted(self.VALIDATOR_REJECTIONS), validators,
            "a validator has no route-level rejection registered (or one names a validator that "
            "is gone). Add a request that trips it to VALIDATOR_REJECTIONS.",
        )
        ws = await self._a_workspace()
        for name, (method, path, body) in self.VALIDATOR_REJECTIONS.items():
            with self.subTest(validator=name):
                r = await self.client.request(method, path, json=body(ws), headers=self.headers)
                self.assertEqual(422, r.status_code, f"{name} did not answer 422 over {method} {path}")
                self._assert_declared_shape(422, r.json(), f"{method} {path} ({name})")


class ClientReadsWhatTheServerSends(unittest.TestCase):
    """The declared shape and the client that reads it, pinned together.

    A schema nothing reads is as useless as no schema. The client reads refusal
    bodies in `ui/src/errorBody.ts` — pure, and the reason it can be asserted at
    all — so this holds the two ends of the agreement still: the keys the engine
    sends, the client has to know about. Parsed as text, for the reason
    `test_dependency_parity.py` gives: importing the module would make this
    test's result depend on a bundler-shaped load.
    """

    READER = ROOT / "ui" / "src" / "errorBody.ts"

    def _reader(self) -> str:
        self.assertTrue(
            self.READER.exists(),
            "ui/src/errorBody.ts is gone, so the client's reading of the engine's "
            "declared error shape is unassertable again",
        )
        return self.READER.read_text(encoding="utf-8")

    def test_the_client_reads_every_key_the_server_can_send(self) -> None:
        reader = self._reader()
        for key in ("code", "message", "detail"):
            self.assertIn(
                key, reader,
                f"the client no longer reads `{key}`, which the engine sends; a "
                "failure would render as a bare HTTP status",
            )

    def test_the_client_keeps_the_extra_facts_a_refusal_attaches(self) -> None:
        """The other half of `additionalProperties`: the client must keep them.

        `detail` is the one that bit. It was stripped from the extras, so the
        field errors the engine deliberately preserves were dropped again on the
        client side — the loss the new `request_validation_error` handler exists
        to prevent, reintroduced one layer down. A refusal's extras go into
        `err.extra` so a delete-confirmation can name the cascade.
        """
        reader = self._reader()
        self.assertIn("extra", reader, "the client stopped keeping a refusal's extras")
        # Exactly `code` and `message` are lifted out of the body. `detail` was in
        # that list too, which dropped the field errors a rejected body carries —
        # the loss the new `request_validation_error` handler exists to prevent,
        # reintroduced one layer down.
        self.assertIn(
            "const { code: _c, message: _m, ...rest } = record;", reader,
            "the client must lift only `code` and `message` out of a refusal body, "
            "and keep everything else — including `detail` — in the extras",
        )


if __name__ == "__main__":
    unittest.main()
