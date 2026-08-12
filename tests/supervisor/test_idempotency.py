"""belay/supervisor/idempotency.py: durable (SQLite-backed) idempotency for
hook events -- the P0 fix for an earlier in-memory-dict version that lost
its cache on every supervisor restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from belay.supervisor.idempotency import IdempotencyStore, content_digest, event_key
from belay.supervisor.protocol import HookEvent
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'idem.db'}", future=True)
    try:
        yield engine
    finally:
        engine.dispose()


def _event(command: str = "rm -rf /tmp/x", event_id: str = "toolu_1") -> HookEvent:
    return HookEvent(
        schema_version=1,
        installation_id="install1",
        trust_tier="T1",
        host="claude-code",
        host_version=None,
        adapter_version="claude-code/1",
        host_session_id="sess1",
        event_id=event_id,
        phase="pre",
        surface="shell",
        tool_name="Bash",
        normalized_identity="Bash",
        args={"command": command},
        cwd="/home/user/project",
        repo_identity="/home/user/project",
        os_user="jairo",
        monotonic_ns=123,
        wall_clock="2026-07-27T00:00:00+00:00",
    )


def test_get_returns_none_when_absent(engine: Engine) -> None:
    store = IdempotencyStore(engine)
    assert store.get("nope") is None


def test_record_then_get_round_trips(engine: Engine) -> None:
    store = IdempotencyStore(engine)
    key = event_key("i1", "claude-code", "s1", "e1", "pre")
    digest = "abc123"
    response = {"hookSpecificOutput": {"permissionDecision": "deny"}}
    store.record_if_absent(key, digest, response)

    cached = store.get(key)
    assert cached is not None
    assert cached.request_digest == digest
    assert cached.response == response


def test_survives_a_fresh_store_instance_against_the_same_engine(
    engine: Engine, tmp_path: Path
) -> None:
    """Simulates a supervisor restart: a NEW IdempotencyStore (as a fresh
    process would construct) against the SAME underlying file must still
    see prior decisions -- the whole point of moving off an in-memory dict."""
    key = event_key("i1", "claude-code", "s1", "e1", "pre")
    IdempotencyStore(engine).record_if_absent(key, "digest1", {"a": 1})
    engine.dispose()

    reopened_engine = create_engine(f"sqlite:///{tmp_path / 'idem.db'}", future=True)
    try:
        reopened = IdempotencyStore(reopened_engine)  # a different Store, same file
        cached = reopened.get(key)
        assert cached is not None
        assert cached.response == {"a": 1}
    finally:
        reopened_engine.dispose()


def test_record_if_absent_second_call_returns_the_first_winner(engine: Engine) -> None:
    store = IdempotencyStore(engine)
    key = event_key("i1", "claude-code", "s1", "e1", "pre")
    first = store.record_if_absent(key, "digestA", {"answer": "first"})
    second = store.record_if_absent(key, "digestB", {"answer": "second"})
    assert second == first
    final = store.get(key)
    assert final is not None
    assert final.response == {"answer": "first"}  # never overwritten


def test_content_digest_is_stable_for_the_same_logical_event() -> None:
    a = _event("rm -rf /tmp/x")
    b = _event("rm -rf /tmp/x")
    assert content_digest(a) == content_digest(b)


def test_content_digest_ignores_timestamps_and_environment_fields() -> None:
    a = _event("rm -rf /tmp/x")
    import dataclasses

    b = dataclasses.replace(
        a,
        monotonic_ns=999999,
        wall_clock="2030-01-01T00:00:00+00:00",
        trust_tier="T0",
        adapter_version="claude-code/2",
        os_user="someone-else",
    )
    assert content_digest(a) == content_digest(b)


def test_content_digest_differs_for_a_different_command() -> None:
    a = _event("rm -rf /tmp/x")
    b = _event("rm -rf /tmp/y")
    assert content_digest(a) != content_digest(b)


def test_content_digest_differs_for_a_different_cwd() -> None:
    import dataclasses

    a = _event("rm -rf /tmp/x")
    b = dataclasses.replace(a, cwd="/somewhere/else")
    assert content_digest(a) != content_digest(b)


def test_event_key_distinguishes_every_component() -> None:
    base = event_key("i1", "claude-code", "s1", "e1", "pre")
    assert base != event_key("i2", "claude-code", "s1", "e1", "pre")
    assert base != event_key("i1", "codex", "s1", "e1", "pre")
    assert base != event_key("i1", "claude-code", "s2", "e1", "pre")
    assert base != event_key("i1", "claude-code", "s1", "e2", "pre")
    assert base != event_key("i1", "claude-code", "s1", "e1", "post")
