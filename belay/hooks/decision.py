"""Deterministic (no LLM) Bash-command risk classification for E18's Native
Agent Gate (`belay hooks run`).

Default-deny (confirmed product decision, plan-v2 E18): every Bash command
PAUSES for human approval unless it matches an explicit, narrow allowlist of
known-safe read-only patterns. The allowlist is a safety valve for the
overwhelmingly common read-only case (an agent checking `git status` a dozen
times a session), not a general permissiveness switch -- anything not
recognized, or recognized but combined with shell chaining/redirection/
substitution, pauses. Never a silent allow on the unknown, matching this
project's approach everywhere else (contracts default `irreversible`,
`verify-test` never trusts an agent-supplied string as a shell command --
see `belay/cli/main.py`'s `verify_test` and its `shell=False` argv build).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Bumped whenever the allowlist or metacharacter rules change. Folded into
#: an approval's plan_id (belay/hooks/gate.py) so a human's approval, granted
#: under one ruleset, is never silently reinterpreted under a different one
#: -- a rule change could turn a previously-PAUSE command into ALLOW or vice
#: versa, and an approval minted before that change shouldn't be assumed to
#: still mean the same thing.
DECISION_LOGIC_VERSION = 1


class Verdict(StrEnum):
    ALLOW = "allow"
    PAUSE = "pause"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


# Any of these lets one shell command chain into, redirect into, or
# substitute in another -- `git status; rm -rf /`, `cat f.txt | sh`,
# `` `evil` ``, `$(evil)`, `git log > /dev/sda`, `sleep 1 &`. A command
# containing any of them is never allowlisted, full stop, regardless of what
# the "safe" verb at the front looks like -- this check runs BEFORE the verb
# allowlist below, not as an afterthought layered on top of it.
_SHELL_METACHARACTERS = re.compile(r"[;&|<>`\n]|\$\(")

# Each pattern must match the ENTIRE stripped command (`fullmatch`, not
# `search`) -- a prefix match would let unexamined trailing content through.
# Deliberately narrow: read-only, no writing arguments, no network. Extend
# this only for genuinely read-only commands; when in doubt, leave it out --
# PAUSE is always safe, a wrongly-added ALLOW is not.
_SAFE_READ_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pwd", re.compile(r"pwd")),
    ("ls", re.compile(r"ls(\s+-\w+)*(\s+\S+)*")),
    ("cat (single file)", re.compile(r"cat(\s+-\w+)*\s+\S+")),
    ("head", re.compile(r"head(\s+-\w+)*(\s+-n\s*\d+)?\s+\S+")),
    ("tail", re.compile(r"tail(\s+-\w+)*(\s+-n\s*\d+)?\s+\S+")),
    ("wc", re.compile(r"wc(\s+-\w+)*\s+\S+")),
    ("grep", re.compile(r"grep(\s+-\w+)*\s+\S+\s+\S+(\s+\S+)*")),
    ("git status", re.compile(r"git\s+status(\s+-\w+)*")),
    ("git diff", re.compile(r"git\s+diff(\s+\S+)*")),
    ("git log", re.compile(r"git\s+log(\s+\S+)*")),
    ("git show", re.compile(r"git\s+show(\s+\S+)*")),
    ("git branch (list only)", re.compile(r"git\s+branch(\s+(-a|-v|--list))?")),
    ("pytest", re.compile(r"pytest(\s+\S+)*")),
    ("echo", re.compile(r"echo\s+.*")),
)


#: One compiled entry: (the operator's literal string, as written in their
#: allowlist file, and the pattern it compiles to). Kept as a tuple of
#: tuples, never a dict, mirroring `_SAFE_READ_PATTERNS` above -- order is
#: the file's own line order, and duplicate literal entries are harmless
#: (the first match wins either way).
ExtraAllowlist = tuple[tuple[str, "re.Pattern[str]"], ...]


def _compile_extra_entry(literal: str, *, exact: bool) -> re.Pattern[str]:
    # `exact=False` (bare entry, unchanged R1 fifth-slice behavior):
    # `literal` alone, or `literal` followed by whitespace and further
    # arguments -- e.g. "npm run lint" also matches "npm run lint --fix".
    # `exact=True` (R1.6, trailing "!"): `literal` alone, nothing more --
    # "npm run lint!" allows "npm run lint" but pauses "npm run lint --fix",
    # since a trailing argument can flip a read-only command into a
    # mutating one and the bare form can't express "no arguments, ever".
    # `re.escape` either way because this is a literal string the operator
    # wrote, never treated as a regex (see `load_extra_allowlist`'s
    # docstring for why).
    if exact:
        return re.compile(re.escape(literal))
    return re.compile(re.escape(literal) + r"(\s+\S.*)?")


def load_extra_allowlist(path: str | Path) -> ExtraAllowlist:
    """Parse an operator-supplied extra-safe-commands file (R1 fifth
    slice, ADR 0024; exact-match suffix added R1.6): one literal command
    prefix per line, blank lines and `#`-prefixed comment lines ignored.

    A line ending in `!` (after trimming trailing whitespace) is an
    exact-match entry: only that literal command, with nothing appended,
    allows -- e.g. `npm run lint!` allows `npm run lint` but still pauses
    `npm run lint --fix`. A bare line (no trailing `!`) keeps the original
    behavior: the literal followed by any further arguments also allows.
    Exact-match is the safer default for anything where an extra argument
    could change behavior (e.g. `--fix`, `-delete`); use the bare form only
    for commands genuinely safe with arbitrary trailing arguments.

    Deliberately literal strings, never regex: letting an operator author
    a regex here risks an accidental `.*`-shaped hole opening in a
    security allowlist, a worse failure mode than the minor inconvenience
    of listing several literal command variants by hand. Each entry still
    passes through the same shell-metacharacter guard every command is
    checked against first (`classify_bash` runs that check before ever
    consulting this allowlist) -- an entry can never itself be used to
    smuggle chaining/redirection/substitution through, even if such text
    somehow ended up in the file.

    Raises `ValueError` if any entry itself contains a shell
    metacharacter, or if a `!`-suffixed line has nothing left after
    stripping the `!`: either could never match any real command anyway
    (a metacharacter always pauses before the allowlist is even checked;
    an empty literal matches nothing), so both are refused loudly at load
    time rather than silently shipping dead, confusing configuration.
    """
    entries: list[tuple[str, re.Pattern[str]]] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        exact = line.endswith("!")
        literal = line[:-1].rstrip() if exact else line
        if not literal:
            raise ValueError(
                f"line {lineno}: {raw_line.strip()!r} has no command left after "
                "stripping the exact-match '!' suffix -- refused"
            )
        if _SHELL_METACHARACTERS.search(literal):
            raise ValueError(
                f"line {lineno}: {literal!r} contains a shell metacharacter -- it could "
                "never match any real command (metacharacters always pause before "
                "the allowlist is even checked), so this entry is refused"
            )
        entries.append((literal, _compile_extra_entry(literal, exact=exact)))
    return tuple(entries)


def classify_bash(command: str, *, extra_allowlist: ExtraAllowlist = ()) -> Decision:
    """Classify a single Bash tool call's `command` string. Never raises --
    an exception escaping the matching logic itself would defeat the point
    of a safety gate, so any unexpected input falls through to the same
    PAUSE default as anything simply unrecognized.

    `extra_allowlist` (R1 fifth slice, ADR 0024) is opt-in, operator-
    configured additional entries (see `load_extra_allowlist`) -- checked
    only after the built-in patterns above, and only ever able to turn a
    PAUSE into an ALLOW, never the reverse. Empty by default (the
    parameter's default), which is fully unchanged legacy behavior."""
    try:
        stripped = command.strip()
        if not stripped:
            return Decision(Verdict.PAUSE, "empty command")
        if _SHELL_METACHARACTERS.search(stripped):
            return Decision(
                Verdict.PAUSE,
                "command contains shell chaining/redirection/substitution "
                "(one of ; & | < > ` $( or a newline) -- never allowlisted "
                "regardless of the leading command",
            )
        for name, pattern in _SAFE_READ_PATTERNS:
            if pattern.fullmatch(stripped):
                return Decision(Verdict.ALLOW, f"matches safe-read allowlist entry: {name}")
        for literal, pattern in extra_allowlist:
            if pattern.fullmatch(stripped):
                return Decision(
                    Verdict.ALLOW,
                    f"matches operator-configured allowlist entry: {literal!r}",
                )
        return Decision(Verdict.PAUSE, "not on the safe-read allowlist")
    except Exception as exc:  # see docstring: never raise out of a gate
        return Decision(Verdict.PAUSE, f"classification error, defaulting to pause: {exc}")
