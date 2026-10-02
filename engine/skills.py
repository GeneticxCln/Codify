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

## A skill may say which moves it is written around

`moves: recon, plan, write` in the header names them. It is a hint and never a
grant. The names are checked against the moves that exist (the caller passes
them in, so this module imports nothing of the conductor), and a name that is
not one is dropped and reported rather than obeyed. `use_skill` then says which
declared moves are not on the menu right now, so a model is told before it is
refused. Nothing here adds a move to a menu, makes a refused move run, or
widens anything. An unknown header key is reported too: it used to vanish, which
made a misspelt `moves:` look exactly like a skill that declared none.

## Override

Built-ins ship in `engine/builtin_skills/`. A workspace file with a built-in's
name replaces it, so a project can redefine `ship-a-change` for its own
conventions — and every replacement is reported in `SkillSet.shadows` so the
transcript can say a built-in was shadowed rather than quietly swapping out the
instructions the user thinks they are running.
"""

from __future__ import annotations

import re
from collections.abc import Collection
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

# The header keys a skill may use, and the most moves one may declare. A list longer than there are moves is
# not a longer list of moves.
HEADER_KEYS = ("name", "description", "moves")
MAX_DECLARED_MOVES = 24

# What a move's name looks like. Checked before the name is compared with the real ones, so a hostile "name"
# is dropped by its shape and never echoed back in full.
_MOVE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# How much of a refused name or key is repeated in a report: a header is untrusted text.
_SHOWN = 40
_SHOWN_COUNT = 5


@dataclass(frozen=True)
class Skill:
    """One skill, as loaded. `source` is "built-in" or "workspace"."""

    name: str
    description: str
    body: str
    source: str
    # The moves the header says the skill is written around, already checked against the moves that exist
    # when the loader was given them. A hint (see the module docstring), never a grant.
    moves: tuple[str, ...] = ()
    # What was wrong with the header and did not stop the skill loading: a move that does not exist, a key
    # nothing reads. Reported by the loader as problems, so the person who wrote it can find out.
    notes: tuple[str, ...] = ()

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


def _strip_header(text: str) -> tuple[dict[str, str], list[str], str]:
    """Split an optional `---` header from the body, and name the keys nothing reads.

    A deliberately small parser rather than a YAML dependency: the header only
    ever holds `name`, `description` and `moves`, all single-line, and a skill
    file that happens to contain a colon in its body must not be able to break
    loading. A key that is none of those is returned as well, not dropped in
    silence.
    """
    if not text.startswith("---"):
        return {}, [], text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [], text
    header: dict[str, str] = {}
    unknown: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return header, unknown, "\n".join(lines[index + 1 :])
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in HEADER_KEYS:
            header[key] = value.strip()
        elif _KEY_RE.match(key) and key not in unknown:
            unknown.append(key)
    # An unterminated header is not a header. The whole text is the body, which
    # is the honest reading of a file that never closed it.
    return {}, [], text


def _shown(names: list[str]) -> str:
    """Names for a report: clipped, few, and quoted, because they came from an untrusted header."""
    shown = [repr(n[:_SHOWN] + ("…" if len(n) > _SHOWN else "")) for n in names[:_SHOWN_COUNT]]
    more = len(names) - _SHOWN_COUNT
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _declared_moves(value: str, known: Collection[str] | None) -> tuple[tuple[str, ...], list[str]]:
    """The moves a header declares, and a note for everything that was dropped or cut.

    A name is kept when it is well-formed and, if the caller said which moves exist, one of them. Order is the
    skill's, a repeat is said once, and nothing past `MAX_DECLARED_MOVES` is read.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name or name in kept:
            continue
        if not _MOVE_RE.match(name) or (known is not None and name not in known):
            if name not in dropped:
                dropped.append(name)
            continue
        kept.append(name)
    notes: list[str] = []
    if dropped:
        notes.append(f"declares moves that do not exist, ignored: {_shown(dropped)}")
    if len(kept) > MAX_DECLARED_MOVES:
        notes.append(f"declares {len(kept)} moves; only the first {MAX_DECLARED_MOVES} were read")
        kept = kept[:MAX_DECLARED_MOVES]
    return tuple(kept), notes


def _first_line(body: str) -> str:
    """The first line that could be a description: not blank, not a heading."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def parse_skill(
    text: str, fallback_name: str, source: str, known_moves: Collection[str] | None = None
) -> tuple[Skill | None, str | None]:
    """One skill file, or the reason it is not one.

    Returns `(skill, None)` or `(None, problem)`. Never raises: a malformed file
    in a checked-out repository must not be able to stop a turn, and the reason
    is returned rather than logged here so the caller decides where it belongs.
    A header problem that does not stop the skill loading rides on `Skill.notes`.

    `known_moves` is the set of moves that exist, passed in so this module needs
    no knowledge of the conductor. Without it only a move's spelling is checked.
    """
    if not text.strip():
        return None, f"{fallback_name}: the file is empty"
    if len(text) > MAX_SKILL_CHARS:
        return None, (
            f"{fallback_name}: {len(text)} characters, over the "
            f"{MAX_SKILL_CHARS} limit"
        )
    header, unknown, body = _strip_header(text)
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
    moves, notes = _declared_moves(header.get("moves", ""), known_moves)
    if unknown:
        notes.append(
            f"has header keys nothing reads: {_shown(unknown)} "
            f"(a skill's header holds {', '.join(HEADER_KEYS)})"
        )
    return (
        Skill(
            name=name, description=description, body=body.strip(), source=source,
            moves=moves, notes=tuple(notes),
        ),
        None,
    )


# The most bytes a skill file is read for. `MAX_SKILL_CHARS` is a limit on characters and a character takes at
# most four bytes of UTF-8, so a file over four bytes per allowed character is over the limit whatever it holds.
# The read stops one byte past that, so a hostile file is refused by what was read, not by reading all of it.
MAX_SKILL_BYTES = MAX_SKILL_CHARS * 4


def _read_file(
    path: Path, source: str, known_moves: Collection[str] | None = None
) -> tuple[Skill | None, str | None]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SKILL_BYTES + 1)
        if len(raw) > MAX_SKILL_BYTES:
            return None, (
                f"{path.stem.lower()}: over {MAX_SKILL_BYTES:,} bytes, past the "
                f"{MAX_SKILL_CHARS}-character limit; the rest was not read"
            )
        text = raw.decode("utf-8")
    except OSError as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"
    except UnicodeDecodeError:
        return None, f"{path.name}: not valid UTF-8 text"
    return parse_skill(text, path.stem.lower(), source, known_moves)


def _skill_files(directory: Path, within: Path | None = None) -> tuple[list[Path], list[str]]:
    """The skill files in `directory`, and a sentence for every entry refused.

    **A symlink is refused, and that is the whole point of this function.**
    `Path.is_file()` follows links, so a cloned repository could ship
    `.codify/skills/notes.md -> ~/.ssh/id_rsa` — or `/etc/passwd`, or anything
    else on this machine — and the shell would read it *as instructions* and
    hand the contents to the model, which has a browser that can send data out
    (`docs/01`). Skill files are repository content, so the rule is the one the
    filesystem service already applies to every other workspace path: the file
    that is opened has to be the file that was named, inside the workspace.

    A refused entry is reported rather than skipped in silence: a skill a user
    can see in their repository and not in the menu is a mystery, and the
    sentence is what makes it a fact instead.

    **The directory is held to the same rule as the files in it.** Checking each
    file's parent against `directory.resolve()` cannot notice a linked directory,
    because by then the link has been followed and every file in the far directory
    looks like a real file inside it. `within` is the workspace: the skills directory
    must resolve to exactly `<workspace>/.codify/skills`, which refuses a link at
    `.codify` or at `skills` alike.
    """
    if not directory.is_dir():
        return [], []
    root = directory.resolve()
    if within is not None and root != within.resolve() / SKILLS_DIRNAME:
        return [], [
            f"{SKILLS_DIRNAME}: refused — it resolves to {root}, outside the workspace's own "
            f"{SKILLS_DIRNAME}; a link to a directory elsewhere would load someone else's files "
            "as instructions"
        ]
    files: list[Path] = []
    problems: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.suffix != ".md":
            continue
        if path.is_symlink() or path.resolve().parent != root:
            problems.append(
                f"{path.name}: refused — a skill file must be a real file inside "
                f"{SKILLS_DIRNAME}, not a link to one somewhere else"
            )
            continue
        if path.is_file():
            files.append(path)
    return files, problems


def _noted(skill: Skill) -> list[str]:
    """A loaded skill's header notes, as reported problems that name it and its source."""
    return [f"{skill.source} skill {skill.name!r}: {note}" for note in skill.notes]


def builtin_skills(known_moves: Collection[str] | None = None) -> tuple[list[Skill], list[str]]:
    skills: list[Skill] = []
    problems: list[str] = []
    files, refused = _skill_files(BUILTIN_DIR)
    problems.extend(f"built-in skill ignored — {problem}" for problem in refused)
    for path in files:
        skill, problem = _read_file(path, "built-in", known_moves)
        if skill is not None:
            skills.append(skill)
            problems.extend(_noted(skill))
        elif problem:
            problems.append(f"built-in skill ignored — {problem}")
    return skills, problems


def workspace_skills(
    root: str | None, known_moves: Collection[str] | None = None
) -> tuple[list[Skill], list[str]]:
    if not root:
        return [], []
    directory = Path(root) / SKILLS_DIRNAME
    files, refused = _skill_files(directory, within=Path(root))
    skills: list[Skill] = []
    problems: list[str] = [
        f"workspace skill ignored — {problem}" for problem in refused
    ]
    if len(files) > MAX_WORKSPACE_SKILLS:
        problems.append(
            f"{SKILLS_DIRNAME} holds {len(files)} skills; only the first "
            f"{MAX_WORKSPACE_SKILLS} were read"
        )
        files = files[:MAX_WORKSPACE_SKILLS]
    for path in files:
        skill, problem = _read_file(path, "workspace", known_moves)
        if skill is not None:
            skills.append(skill)
            problems.extend(_noted(skill))
        elif problem:
            problems.append(f"workspace skill ignored — {problem}")
    return skills, problems


def load_skills(root: str | None, known_moves: Collection[str] | None = None) -> SkillSet:
    """Every skill available for a workspace, with workspace files winning.

    `known_moves` is the set of moves that exist; the moves a skill declares are checked against it.

    Built-ins are loaded first and a workspace skill of the same name replaces
    it in place, so the menu order is stable regardless of which layer a skill
    came from and a shadowed built-in keeps its position.
    """
    builtin, builtin_problems = builtin_skills(known_moves)
    workspace, workspace_problems = workspace_skills(root, known_moves)

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
