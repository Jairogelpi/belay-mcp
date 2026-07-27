"""belay/rewind/intent.py: --intent/--keep resolution to a --to-step cutoff."""

from __future__ import annotations

import pytest
from belay.ledger.store import LedgerStore
from belay.rewind.intent import IntentRewindError, resolve_intent_to_step


def _session_with_intents(*intents: str) -> tuple[LedgerStore, str]:
    """A session with one plan_created step per given intent label, in order."""
    ledger = LedgerStore()
    session_id = "s_test"
    for i, intent in enumerate(intents, start=1):
        ledger.append(
            session_id,
            "plan_created",
            {"tool": "fs.write_file", "args": {"path": f"f{i}.py"}, "intent_id": intent},
            step_seq=i,
        )
    return ledger, session_id


def test_contiguous_suffix_resolves_to_last_keep_step() -> None:
    ledger, sid = _session_with_intents("auth-fix", "auth-fix", "cache-refactor", "cache-refactor")
    to_step = resolve_intent_to_step(ledger.read(sid), "cache-refactor", "auth-fix")
    assert to_step == 2


def test_no_keep_given_resolves_to_zero() -> None:
    ledger, sid = _session_with_intents("cache-refactor")
    to_step = resolve_intent_to_step(ledger.read(sid), "cache-refactor", None)
    assert to_step == 0


def test_unknown_intent_raises() -> None:
    ledger, sid = _session_with_intents("auth-fix")
    with pytest.raises(IntentRewindError) as exc_info:
        resolve_intent_to_step(ledger.read(sid), "nonexistent", None)
    assert exc_info.value.code == "intent_not_found"


def test_no_tagged_steps_at_all_raises() -> None:
    ledger = LedgerStore()
    sid = "s_untagged"
    ledger.append(sid, "plan_created", {"tool": "fs.write_file", "args": {}}, step_seq=1)
    with pytest.raises(IntentRewindError) as exc_info:
        resolve_intent_to_step(ledger.read(sid), "anything", None)
    assert exc_info.value.code == "intent_not_found"


def test_keep_after_target_refused_as_interleaved() -> None:
    """auth-fix (keep) comes AFTER cache-refactor (target) -- not a safe suffix."""
    ledger, sid = _session_with_intents("cache-refactor", "auth-fix")
    with pytest.raises(IntentRewindError) as exc_info:
        resolve_intent_to_step(ledger.read(sid), "cache-refactor", "auth-fix")
    assert exc_info.value.code == "rewind_intent_not_suffix"


def test_third_intent_after_keep_refused() -> None:
    """A step tagged neither 'intent' nor 'keep' follows the kept step -- refuse
    rather than silently rewinding or ignoring it."""
    ledger, sid = _session_with_intents("auth-fix", "cache-refactor", "unrelated-thing")
    with pytest.raises(IntentRewindError) as exc_info:
        resolve_intent_to_step(ledger.read(sid), "cache-refactor", "auth-fix")
    assert exc_info.value.code == "rewind_intent_not_suffix"
    assert exc_info.value.detail["unaccounted_steps"] == [3]


def test_untagged_step_after_keep_refused() -> None:
    ledger, sid = _session_with_intents("auth-fix", "cache-refactor")
    ledger.append(
        sid, "plan_created", {"tool": "fs.write_file", "args": {"path": "f3.py"}}, step_seq=3
    )
    with pytest.raises(IntentRewindError) as exc_info:
        resolve_intent_to_step(ledger.read(sid), "cache-refactor", "auth-fix")
    assert exc_info.value.code == "rewind_intent_not_suffix"
