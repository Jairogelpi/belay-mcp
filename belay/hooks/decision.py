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
from enum import Enum

#: Bumped whenever the allowlist or metacharacter rules change. Folded into
#: an approval's plan_id (belay/hooks/gate.py) so a human's approval, granted
#: under one ruleset, is never silently reinterpreted under a different one
#: -- a rule change could turn a previously-PAUSE command into ALLOW or vice
#: versa, and an approval minted before that change shouldn't be assumed to
#: still mean the same thing.
DECISION_LOGIC_VERSION = 1


class Verdict(str, Enum):
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


def classify_bash(command: str) -> Decision:
    """Classify a single Bash tool call's `command` string. Never raises --
    an exception escaping the matching logic itself would defeat the point
    of a safety gate, so any unexpected input falls through to the same
    PAUSE default as anything simply unrecognized."""
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
        return Decision(Verdict.PAUSE, "not on the safe-read allowlist")
    except Exception as exc:  # see docstring: never raise out of a gate
        return Decision(Verdict.PAUSE, f"classification error, defaulting to pause: {exc}")
