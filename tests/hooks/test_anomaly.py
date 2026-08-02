"""Tests for `belay/hooks/anomaly.py` (R1.8.x, ADR 0026): reuses
`belay.policy.baseline.BaselineStore` directly against a hook event's own
`ledger_session_id`, same z-score math as `belay/policy/engine.py`'s own
anomaly dimension (mirrored in `tests/policy/test_anomaly.py`).

These tests seed the ledger directly with `plan_created`-shaped payloads
(same as `tests/policy/test_anomaly.py::_seed_normal_history` does for the
MCP proxy path) rather than going through `belay/hooks/gate.py::resolve_effects`,
because a real static `Contract`'s declared `count` never varies call to
call (spec §5.3's "contract"-basis is a fixed literal) -- there is no way
to produce a genuine varying-count history through the real hooks
contract-resolution path today. Seeding the ledger directly is the only
way to exercise `evaluate_anomaly`'s actual z-score logic against a
history that varies, which is exactly the scenario a future dynamic
count source (mirroring the proxy's own `sql_simulator`/`native_dry_run`
bases) would produce.
"""

from __future__ import annotations

from belay.hooks.anomaly import AnomalyConfig, evaluate_anomaly
from belay.hooks.claude_code_adapter import normalize
from belay.ledger.store import LedgerStore
from belay.supervisor.protocol import HookEvent


def _mcp_event(tool_name: str = "mcp__github__bulk_close_issues") -> HookEvent:
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {},
        "tool_use_id": "toolu_anomaly",
    }
    return normalize(raw, installation_id="test-install")


def _seed_history(ledger: LedgerStore, session_id: str, tool: str, counts: list[str]) -> None:
    for count in counts:
        ledger.append(
            session_id,
            "plan_created",
            {
                "tool": tool,
                "effects": [{"type": "update", "resource": "github.issues", "count": count}],
            },
        )


def test_cold_start_never_flags_anomaly_below_min_samples() -> None:
    ledger = LedgerStore()
    event = _mcp_event()
    from belay.hooks.gate import ledger_session_id

    _seed_history(ledger, ledger_session_id(event), event.tool_name, ["10"] * 5)  # below min 10
    config = AnomalyConfig(ledger=ledger)
    effects = [{"type": "update", "resource": "github.issues", "count": "5000"}]
    assert evaluate_anomaly(event, effects, config) is None


def test_outlier_after_baseline_established_returns_a_human_readable_reason() -> None:
    ledger = LedgerStore()
    event = _mcp_event()
    from belay.hooks.gate import ledger_session_id

    session_id = ledger_session_id(event)
    _seed_history(ledger, session_id, event.tool_name, ["10"] * 10)
    config = AnomalyConfig(ledger=ledger)
    effects = [{"type": "update", "resource": "github.issues", "count": "500"}]
    reason = evaluate_anomaly(event, effects, config)
    assert reason is not None
    assert "500" in reason
    assert "baseline" in reason
    assert event.tool_name in reason


def test_normal_count_matching_the_baseline_is_not_anomalous() -> None:
    ledger = LedgerStore()
    event = _mcp_event()
    from belay.hooks.gate import ledger_session_id

    session_id = ledger_session_id(event)
    _seed_history(ledger, session_id, event.tool_name, ["10"] * 10)
    config = AnomalyConfig(ledger=ledger)
    effects = [{"type": "update", "resource": "github.issues", "count": "10"}]
    assert evaluate_anomaly(event, effects, config) is None


def test_an_effect_with_no_parseable_count_is_silently_skipped_not_a_crash() -> None:
    ledger = LedgerStore()
    event = _mcp_event()
    config = AnomalyConfig(ledger=ledger)
    effects = [{"type": "update", "resource": "github.issues"}]  # no "count" key at all
    assert evaluate_anomaly(event, effects, config) is None


def test_baseline_is_per_session_no_cross_contamination() -> None:
    import dataclasses

    ledger = LedgerStore()
    event = _mcp_event()
    from belay.hooks.gate import ledger_session_id

    _seed_history(ledger, ledger_session_id(event), event.tool_name, ["10"] * 10)

    other_session_event = dataclasses.replace(event, host_session_id="s2")
    config = AnomalyConfig(ledger=ledger)
    effects = [{"type": "update", "resource": "github.issues", "count": "500"}]
    # A different session has zero baseline history of its own -- cold start,
    # never flagged regardless of magnitude.
    assert evaluate_anomaly(other_session_event, effects, config) is None


def test_real_static_contract_effects_never_vary_so_this_never_actually_flags(
) -> None:
    """The honest R1.8.x limitation, proven directly: `resolve_effects`
    resolves a real `Contract`'s declared effects verbatim (spec §5.3's
    "contract"-basis, a fixed literal), so feeding the *same* resolved
    effects in every call -- exactly what happens in real usage today --
    can never diverge from its own baseline mean. This isn't dead code
    (see the tests above, which prove the z-score math is correct against
    a genuinely varying history), just not yet reachable from a real,
    static-only contract."""
    from belay.contracts.model import Contract, ContractSet
    from belay.hooks.gate import ledger_session_id, resolve_effects

    contract = Contract(
        belay_contract="0.1",
        tool="mcp__github__bulk_close_issues",
        reversibility="irreversible",
        effects=[{"type": "update", "resource": "github.issues", "count": "50"}],  # type: ignore[list-item]
    )
    contract_set = ContractSet(
        contracts={"mcp__github__bulk_close_issues": contract}, set_hash="sha256:x"
    )
    ledger = LedgerStore()
    event = _mcp_event()
    session_id = ledger_session_id(event)
    config = AnomalyConfig(ledger=ledger)

    for _i in range(15):  # well past min_samples=10
        effects, _ = resolve_effects(event, contract_set)
        reason = evaluate_anomaly(event, effects, config)
        assert reason is None
        ledger.append(session_id, "plan_created", {"tool": event.tool_name, "effects": effects})
