"""The goal pipeline: plan, write, verify, judge, record.

This module is the assembly point, not the implementation. `ExecutorService` is one class built from
layers, each in its own module and each calling only the layers below it; the layers, and the
modules that hold the parts which are not methods of the service, are listed on the class below.

**Part of the design code is a DRAFT RECONSTRUCTION, not the original.** The notice is carried, in
full, by `engine/executor_design.py`, next to the code it describes: the design stage, the brand
contract and its drift check, and the design and knowledge deliverables (whose paths also run
through `_fixer`, `_verifier` and `_critic` in `executor_steps`). Everything else was moved here
from the single file this used to be, unmodified.

`engine.executor` re-exports every name the rest of the repo imports from it (the `as` form is what
tells mypy a name is deliberately public), so `from engine.executor import ExecutorService` and the
constants the suite reads keep resolving wherever the definition now lives.
"""

from __future__ import annotations

# Re-exported: each of these is imported from `engine.executor` by the engine, the scripts or the suite.
from engine.conductor_tools import ConductorTools as ConductorTools, _Conducted as _Conducted
from engine.executor_conduct import _Conduct, _as_prose as _as_prose
from engine.executor_core import (
    DEFAULT_PARALLEL_WIDTH as DEFAULT_PARALLEL_WIDTH,
    _env_parallel_width as _env_parallel_width,
)
from engine.executor_design import (
    ARTIFACT_PROPOSED as ARTIFACT_PROPOSED,
    ARTIFACT_WRITTEN as ARTIFACT_WRITTEN,
    DELIVERABLE_FILES as DELIVERABLE_FILES,
    DELIVERABLE_ROLE as DELIVERABLE_ROLE,
    DELIVERABLE_SUBJECT as DELIVERABLE_SUBJECT,
    MAX_BRAND_DRIFTS as MAX_BRAND_DRIFTS,
    MAX_CONTRACT_FILE_CHARS as MAX_CONTRACT_FILE_CHARS,
    MAX_DESIGN_COMPONENTS as MAX_DESIGN_COMPONENTS,
    MAX_DESIGN_MD_CHARS as MAX_DESIGN_MD_CHARS,
    MAX_DRIFT_DIFF_CHARS as MAX_DRIFT_DIFF_CHARS,
)
from engine.executor_evidence import MAX_LIBRARY_ROUNDS as MAX_LIBRARY_ROUNDS
from engine.executor_steps import MAX_REFUSED_TEST_COMMANDS as MAX_REFUSED_TEST_COMMANDS, _Steps
from engine.executor_support import (
    STAGE_OUTCOMES as STAGE_OUTCOMES,
    AgentOutputInvalid as AgentOutputInvalid,
)
from engine.providers import FALLBACK_TRIGGER_CODES as FALLBACK_TRIGGER_CODES
from engine.replies import (
    REPLY_KEYS as REPLY_KEYS,
    REPLY_TOLERATES_TRUNCATION as REPLY_TOLERATES_TRUNCATION,
    extract_json as extract_json,
)


class ExecutorService(_Conduct, _Steps):
    """The goal pipeline's service: plan, write, verify, judge and record, and answer a chat turn.

    One class, assembled from layers that each live in their own module and call only the layers below
    them, so every attribute a layer reads is declared once, in a layer beneath it:

      executor_core      the state, the stage measurement, the status and step writes, the failure path
      executor_evidence  the librarian and the evidence pack
      executor_design    the design stage, the brand contract, the deliverables (a draft reconstruction)
      executor_plan      from a goal to a stored plan            (design, evidence, core)
      executor_steps     fixer, verifier, critic, scribe, retry, apply  (design, evidence, core)
      executor_conduct   a chat turn, and the conductor driving a plan  (plan, core)

    Layers over one class, not separate services, because the tests and the conductor's tools reach about
    twenty-five of these methods directly (`docs/03` section 4.2). The exceptions a stage can end with live in
    `executor_support`, the model-call path in `agent_orchestrator`, the conductor's moves in
    `conductor_tools`; constants and helpers live with the layer that owns them. `engine.executor`
    re-exports every name the rest of the repo imports from it.
    """
