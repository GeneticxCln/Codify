"""What a model may ask git to read: one table, one parser, and the child it runs in.

This replaces a denylist of flag *spellings*. The denylist named `--open-files-in-pager`
and git accepted `--open-files-in-pa=touch X`, because git takes any unambiguous prefix
of a long option; it named `-d` and `--delete` and git accepted `--del`; it refused
`git branch NAME` and accepted `git branch -v NAME`; and it looked at flags only, so
`git diff /outside/file /dev/null` printed a file from outside the workspace (git
silently turns a `diff` with a path outside the tree into `--no-index`). Each was found by
running git, and each fix by spelling would have been the next one's starting point.

So the rule here is the opposite one: **an option is refused unless this table names it,
exactly**. A spelling git has yet to grow cannot walk through a table that is only
consulted for equality, and a prefix of a listed option is not the option. The table lists
only what recon needs (history, diffs, blame, search, refs), and leaves out anything that

- writes (`--output`, `-d`, `-m`, `-c`, `--edit-description`, `--set-upstream-to`),
- runs a program (`--ext-diff`, `--textconv`, `-O`, `--open-files-in-pager`, `--exec`,
  `--show-signature`, `-v` on `tag`, which verifies through gpg),
- names a file (`--file`, `-f`, `--exclude-from`, `--orderfile`, `--contents`,
  `--ignore-revs-file`), or
- turns git at another tree (`--no-index`, `--git-dir`, `--work-tree`, `-C`, `-c`).

Positional arguments are held to the same standard as flags. Every one that could be a
path or a revision must stay inside the workspace (`FileSystemService.resolve`, which also
follows symlinks), with the one exception that has no other reading: `git grep`'s pattern.
`branch` and `tag` are reads only when a listing flag is present, because `git branch
NAME` creates a ref and a bare word is not a flag any denylist can see.

Nothing in here starts a process; `sandbox.py` and `git.py` both call in and both use
`runner_args` and `runner_env`, so a read-only git runs the same way through either door.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


class GitRefusal(Exception):
    """A read-only git argv this module will not let through, with the reason as a sentence."""


@dataclass(frozen=True)
class _Options:
    """The options one subcommand accepts, by how they take their value."""

    flags: frozenset[str]      # NAME        no value
    optional: frozenset[str]   # NAME[=]     bare, or the value attached (`--stat=80`, `-U3`)
    text: frozenset[str]       # NAME=       a value, attached; never a path
    revs: frozenset[str]       # NAME=rev    a value, attached, that names a revision
    separate: frozenset[str]   # NAME=+      short options only: value attached *or* the next word

    def names(self) -> frozenset[str]:
        return self.flags | self.optional | self.text | self.revs | self.separate


def _spec(words: str) -> _Options:
    flags: set[str] = set()
    optional: set[str] = set()
    text: set[str] = set()
    revs: set[str] = set()
    separate: set[str] = set()
    for word in words.split():
        if word.endswith("[=]"):
            optional.add(word[:-3])
        elif word.endswith("=rev"):
            revs.add(word[:-4])
        elif word.endswith("=+"):
            separate.add(word[:-2])
        elif word.endswith("="):
            text.add(word[:-1])
        else:
            flags.add(word)
    return _Options(*(frozenset(s) for s in (flags, optional, text, revs, separate)))


# Value options are attached-only (`--since=DATE`) unless marked `=+`, and only short
# options are marked: git's own parser consumes the next word for exactly those, so the
# validator and git can never disagree about which word was a value and which was not. A
# disagreement is how an unchecked word would reach git as an option.
_DIFF_FORMAT = """
--stat[=] --numstat --shortstat --dirstat[=] --summary --patch -p --no-patch -s --name-only
--name-status --raw --check --minimal --patience --histogram --diff-algorithm=
--ignore-space-change -b --ignore-all-space -w --ignore-blank-lines --ignore-space-at-eol
--ignore-cr-at-eol --word-diff[=] --unified= -U[=] --inter-hunk-context= --find-renames[=]
-M[=] --find-copies[=] -C[=] --no-renames --diff-filter= --full-index --abbrev[=] --text
--function-context -W --no-color --color[=] --ignore-submodules[=] -S=+ -G=+
--pickaxe-regex --pickaxe-all -R -z --src-prefix= --dst-prefix= --no-prefix --exit-code
--quiet
"""

_REVISION_WALK = """
--oneline --graph --decorate[=] --no-decorate --abbrev-commit --no-abbrev-commit --all
--branches[=] --tags[=] --remotes[=] --first-parent --merges --no-merges --reverse --follow
--date= --format= --pretty[=] --since= --after= --until= --before= --author= --committer=
--grep= --max-count= -n=+ --skip= --all-match --invert-grep -i --regexp-ignore-case -E
--extended-regexp --fixed-strings --topo-order --date-order --author-date-order
--ancestry-path --simplify-by-decoration --full-history --source --left-right --cherry-pick
--cherry-mark --boundary --parents --use-mailmap --diff-merges= --no-walk[=]
"""

_SPECS: dict[str, str] = {
    "status": """
        --short -s --branch -b --porcelain[=] --long --ignored[=] --untracked-files[=]
        --show-stash --ahead-behind --no-ahead-behind --renames --no-renames --column[=]
        --no-column -z
    """,
    "diff": _DIFF_FORMAT + " --cached --staged --merge-base",
    "log": _DIFF_FORMAT + _REVISION_WALK,
    "show": _DIFF_FORMAT + " --oneline --format= --pretty[=] --abbrev-commit --no-abbrev-commit"
    " --date= --decorate[=] --no-decorate --first-parent --diff-merges= --use-mailmap",
    "blame": """
        -L=+ --porcelain -p --line-porcelain --show-name --show-number -n --show-email -e -w
        --date= --abbrev= -s --root --incremental --minimal
    """,
    "ls-files": """
        --cached --deleted --modified --others -o --ignored --stage --unmerged --killed
        --exclude-standard --full-name --abbrev[=] -z --eol --directory --no-empty-directory
        --error-unmatch
    """,
    "rev-parse": """
        --abbrev-ref[=] --short[=] --verify --quiet -q --symbolic --symbolic-full-name
        --is-inside-work-tree --is-inside-git-dir --is-bare-repository --is-shallow-repository
        --show-prefix --show-toplevel --show-cdup --revs-only --no-revs --flags --no-flags
        --all --branches[=] --tags[=] --remotes[=]
    """,
    "describe": """
        --tags --always --long --all --abbrev[=] --exact-match --dirty[=] --contains
        --first-parent --match= --exclude= --candidates=
    """,
    "shortlog": """
        -s -n -e --summary --numbered --email --committer --since= --until= --after=
        --before= --no-merges --all --branches[=] --tags[=] --remotes[=] --first-parent
    """,
    "grep": """
        -i --ignore-case -v --invert-match -w --word-regexp -l --files-with-matches -L
        --files-without-match -n --line-number -H -h -I -E --extended-regexp -G --basic-regexp
        -P --perl-regexp --fixed-strings --count --heading --break --full-name --null
        --cached --max-depth= --text -o --only-matching -q --quiet --and --or --not
        -A=+ -B=+ -C=+ --context= --after-context= --before-context= -e=+ --threads=
        --max-count= -p --show-function -W --function-context --no-color --color[=]
        --all-match --no-exclude-standard
    """,
    "cat-file": "-p -t -s -e",
    "show-ref": """
        --heads --tags --head --hash[=] -s --dereference --verify --quiet -q --abbrev[=]
        --exists
    """,
    "branch": """
        --list -l --all -a --remotes -r --verbose -v --show-current --abbrev[=] --no-abbrev
        --color[=] --no-color --sort= --format= --points-at=rev --contains[=] --no-contains[=]
        --merged[=] --no-merged[=] -i --ignore-case
    """,
    "tag": """
        --list -l -n[=] --sort= --format= --points-at=rev --contains[=] --no-contains[=]
        --merged[=] --no-merged[=] -i --ignore-case --color[=] --no-color
    """,
}

_TABLE: dict[str, _Options] = {name: _spec(words) for name, words in _SPECS.items()}

# The public shape: subcommand -> every option spelling it accepts. Derived, so that the
# list of subcommands has exactly one owner (`sandbox.READ_ONLY_GIT_SUBCOMMANDS` and
# `GitService.READ_ONLY_ARGV` are both this key set; there used to be three literals).
GIT_READ_ONLY: dict[str, frozenset[str]] = {name: opts.names() for name, opts in _TABLE.items()}
READ_ONLY_GIT_SUBCOMMANDS: frozenset[str] = frozenset(GIT_READ_ONLY)

# `git branch NAME` and `git tag NAME` create a ref. Their positionals are patterns only
# when one of these is present; `-n` on `tag` implies a listing to git but is not accepted
# as one here, because a rule that has to be argued is not worth its convenience.
_LISTING_ONLY = frozenset({"branch", "tag"})
_LISTING_FLAGS = frozenset({"--list", "-l"})

# `git log -5` is `-n 5`.
_NUMERIC_SHORTHAND = frozenset({"log", "show"})
_COUNT = re.compile(r"\d+")

# `%G?` and friends, and `%(signature)`, make git run gpg to fill in the format.
_RUNS_GPG = re.compile(r"%[-+ ]?G|signature", re.IGNORECASE)
_FORMAT_OPTIONS = frozenset({"--format", "--pretty"})


def _refuse(sub: str, what: str) -> GitRefusal:
    return GitRefusal(
        f"{what}. git {sub} accepts only: {', '.join(sorted(GIT_READ_ONLY[sub]))}"
    )


def _inside(word: str, confined: Callable[[str], bool]) -> bool:
    """Is this word, read as a path or a revision, inside the workspace?

    `confined` is the workspace check (`FileSystemService.resolve`, symlinks included).
    On top of it, a `..` segment is refused wherever it sits, because a revision can carry
    a path (`HEAD:../x`) that `resolve` reads as one odd file name. `HEAD~1..HEAD` is a
    range, one segment with two dots in it, and stays allowed.
    """
    if word == "-":
        return True
    if any(part == ".." for part in re.split(r"[/:]", word)):
        return False
    return confined(word)


def validate(args: Sequence[str], confined: Callable[[str], bool]) -> None:
    """Accept `args` (a git subcommand and everything after it) or raise `GitRefusal`.

    Every word is either an option this table lists for that subcommand, the value of one
    that takes a value, or a positional that stays inside the workspace.
    """
    if not args:
        raise GitRefusal("git requires a read-only subcommand")
    sub = args[0]
    if sub.startswith("-"):
        raise GitRefusal(
            "git requires a read-only subcommand first; global options such as -c, -C and "
            "--git-dir are not the caller's to set"
        )
    opts = _TABLE.get(sub)
    if opts is None:
        raise GitRefusal(f"git {sub} is not a read-only git command")

    positionals: list[str] = []
    before_separator = 0          # positionals seen before a bare `--`
    past_separator = False
    given: set[str] = set()       # the options that were used, by their table spelling
    i = 1
    while i < len(args):
        word = args[i]
        i += 1
        if past_separator or word == "-" or not word.startswith("-"):
            positionals.append(word)
            if not past_separator:
                before_separator += 1
            continue
        if word == "--":
            past_separator = True
            continue
        if word.startswith("--"):
            name, has_value, value = word.partition("=")
            if name in _FORMAT_OPTIONS and _RUNS_GPG.search(value):
                raise _refuse(sub, f"{name} would make git run gpg to fill in a signature")
            if name in opts.flags:
                if has_value:
                    raise _refuse(sub, f"{name} takes no value")
            elif name in opts.optional:
                pass
            elif name in opts.text or name in opts.revs:
                if not has_value:
                    raise _refuse(sub, f"{name} needs its value attached, as {name}=VALUE")
                if name in opts.revs and not _inside(value, confined):
                    raise _refuse(sub, f"{name}={value} names something outside the workspace")
            else:
                raise _refuse(sub, f"{name} is not an allowed option")
            given.add(name)
            continue
        # A cluster of short options: `-sn`, `-U3`, `-ne pattern`. Read left to right the
        # way git does — flags, then at most one option that takes the rest of the word.
        letters = word[1:]
        if sub in _NUMERIC_SHORTHAND and _COUNT.fullmatch(letters):
            continue
        for position, letter in enumerate(letters):
            short = "-" + letter
            rest = letters[position + 1:]
            if short in opts.flags:
                given.add(short)
                continue
            if short in opts.optional:
                given.add(short)
                break
            if short in opts.separate:
                given.add(short)
                if not rest:
                    if i >= len(args):
                        raise _refuse(sub, f"{short} needs a value")
                    i += 1
                break
            if short in opts.text:
                if not rest:
                    raise _refuse(sub, f"{short} needs its value attached, as {short}VALUE")
                given.add(short)
                break
            raise _refuse(sub, f"{short} is not an allowed option (in {word})")

    if sub in _LISTING_ONLY and positionals and not (given & _LISTING_FLAGS):
        raise GitRefusal(
            f"git {sub} {positionals[0]} would create a ref; only the listing forms of "
            f"{sub} are read-only (add --list to match {positionals[0]} as a pattern)"
        )
    # `git grep PATTERN`: with no `-e`, the first word before any `--` is the pattern, and a
    # pattern is a regular expression, not a place. It is the one positional that can
    # legitimately begin with `/` or contain `..`.
    skip = 1 if sub == "grep" and "-e" not in given and before_separator else 0
    for word in positionals[skip:]:
        if not _inside(word, confined):
            raise _refuse(sub, f"{word} is outside the workspace or names git's own metadata")


# ── the child process ─────────────────────────────────────────────────────────

# What a read-only git needs from the engine's environment, and nothing else. The engine's
# environment holds provider API keys and the boot token; git has no use for either, and a
# repository's own config can name programs (`core.fsmonitor`, `diff.external`) that would
# be started with whatever is in it.
_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


def runner_env(base: Mapping[str, str], root: str | None = None) -> dict[str, str]:
    """The environment for a read-only git child.

    `root` is the workspace. It becomes the ceiling for repository discovery, so a workspace
    that is not itself a repository cannot silently read the history of one above it — which
    is a read outside the workspace whatever the argv says.
    """
    env = {key: base[key] for key in _KEEP if key in base}
    env.update({
        # The user's git config is not part of a recon command: not their pager, their
        # aliases nor their `safe.directory` opinions. The repository's own config still
        # applies, and is the residual trust here (see docs/04 §5).
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        # `git status` refreshes the index and writes it back; a read must not write.
        "GIT_OPTIONAL_LOCKS": "0",
        # Nothing here may stop and ask a person who is not at the terminal.
        "GIT_TERMINAL_PROMPT": "0",
    })
    if root is not None:
        env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(root))
    return env


# Diff-shaped commands run a configured external diff driver and textconv filter unless
# told not to. Both are programs named in repository config.
_NO_DRIVERS: dict[str, tuple[str, ...]] = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "blame": ("--no-textconv",),
}


def runner_args(args: Sequence[str]) -> list[str]:
    """`args` (subcommand first) with the hardening git needs, ready to follow the binary.

    The global options go before the subcommand and the per-command ones directly after it,
    ahead of anything the caller supplied, so a bare `--` cannot end their effect.
    """
    if not args:
        return []
    sub, rest = args[0], list(args[1:])
    return [
        "--no-pager",
        # Runs the configured "filesystem monitor" program, and `status`, `diff` and
        # `ls-files` all consult it.
        "-c", "core.fsmonitor=false",
        sub,
        *_NO_DRIVERS.get(sub, ()),
        *rest,
    ]
