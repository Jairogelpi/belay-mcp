"""Deterministic pre-execution check of a call against an `IntentContract`.

Runs before `resolve`/plan/policy (in `Lifecycle.govern_and_execute`) --
a violation here means the call never reaches the upstream at all, not
even as a dry-run estimate. No LLM, no heuristic guess: `fnmatch` against
a *normalized* `path`, an exact tool-name set membership check, and an
integer count.

Two failure modes a naive version of this has to close, both found in
review before this landed:

- `fnmatch` alone matches raw strings -- `src/../../etc/passwd` matches
  `src/**` character-for-character before `..` is resolved. `_normalize`
  collapses `..`/`.`/backslashes first; a path that still escapes upward
  after normalizing (or that's absolute) is denied outright as
  `path_escapes_scope`, never glob-matched at all.
- A tool call with no `path` argument silently bypassed scope entirely
  (nothing to check it against). If the contract declares `allowed_scope`
  or `forbidden_scope` at all, that declaration is a claim every mutating
  call is file-scoped -- so a call this module can't verify is denied
  (`unscoped_call`) rather than waved through. This does mean a
  non-file-shaped tool can't be used at all under an active scope; that's
  the honest tradeoff for "scope means scope," not a partial one.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from belay.intent.model import IntentContract


@dataclass(frozen=True)
class IntentViolation:
    reason: str
    detail: dict[str, Any]


def _normalize(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _escapes_scope(normalized: str) -> bool:
    return normalized.startswith("..") or posixpath.isabs(normalized) or ":" in normalized


def check_intent_contract(
    contract: IntentContract,
    tool: str,
    args: dict[str, Any],
    files_touched_so_far: frozenset[str],
) -> IntentViolation | None:
    """Return the violation blocking this call under `contract`, or `None` if it's clear."""
    if tool in contract.forbidden_tools:
        return IntentViolation(
            "forbidden_tool", {"tool": tool, "forbidden_tools": contract.forbidden_tools}
        )

    scoped = bool(contract.allowed_scope or contract.forbidden_scope)
    raw_path = args.get("path")

    if not isinstance(raw_path, str):
        if scoped:
            return IntentViolation(
                "unscoped_call",
                {
                    "tool": tool,
                    "reason": "contract declares a file scope but this call has no "
                    "checkable 'path' argument -- denied rather than silently exempted",
                },
            )
        return None

    path = _normalize(raw_path)
    if _escapes_scope(path):
        return IntentViolation(
            "path_escapes_scope", {"path": raw_path, "normalized": path}
        )

    for pattern in contract.forbidden_scope:
        if fnmatch(path, pattern):
            return IntentViolation("forbidden_scope", {"path": path, "pattern": pattern})
    if contract.allowed_scope and not any(
        fnmatch(path, pattern) for pattern in contract.allowed_scope
    ):
        return IntentViolation(
            "out_of_scope", {"path": path, "allowed_scope": contract.allowed_scope}
        )

    cap = contract.budgets.files_changed
    if cap is not None:
        projected = files_touched_so_far | {path}
        if len(projected) > cap:
            return IntentViolation(
                "budget_exceeded",
                {"budget": "files_changed", "cap": cap, "files_touched": sorted(projected)},
            )

    return None
