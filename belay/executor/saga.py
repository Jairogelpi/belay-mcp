"""The saga step lifecycle and compensation materialization (spec §8).

Normative step lifecycle (spec §8.1), implemented here in this **exact**
order — this is what `docs/adr/0006-e6-saga-executor.md` documents and what
`tests/executor/test_stage_order.py` pins down stage by stage:

1. **journaled** — intent (tool, args, contract) durably appended before any
   external call.
2. **capturing** — the contract's `capture` block, if declared, runs and its
   snapshot is appended. The capture tool's own contract MUST be read-only
   (`contract_invalid` otherwise).
3. **calling** — the real tool call, deduplicated by `idempotency_key` when
   declared (`belay/executor/idempotency.py`).
4. **result_recorded** — the full result is appended.
5. **compensation_registered** — the inverse call is *materialized*: `undo.args`
   is evaluated against `$args/$result/$state` right now and the concrete,
   literal args are what gets persisted. Rewind (E7) replays this frozen
   payload; it never re-evaluates the expression against later state. A
   `conditional` contract whose `conditions` don't hold at this point, or an
   `irreversible` contract, registers as not-reversible instead (spec §4.2) —
   the event still fires so `verify_coherence` (§9.2) is satisfied either way.
6. **committed**.

Any exception at any stage aborts the step with a `step_failed` event; stages
already appended before the failure remain in the ledger untouched (spec:
"any -> failed(reason)").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from belay.contracts.expressions import Scope, evaluate, parse
from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError
from belay.executor.idempotency import IdempotencyStore
from belay.ledger.store import LedgerStore

Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]
StageHook = Callable[[str], None]

#: The six normative stages of spec §8.1, in order.
STAGES: tuple[str, ...] = (
    "journaled",
    "capturing",
    "calling",
    "result_recorded",
    "compensation_registered",
    "committed",
)


def _materialize(value: Any, scope: Scope) -> Any:
    """Evaluate every `$`-prefixed expression string found in `value` against `scope`.

    Plain (non-expression) values pass through unchanged. Recurses into
    dicts/lists so a contract's `args`/`undo.args` mapping of expressions
    (spec §4.3) becomes a concrete literal payload.
    """
    if isinstance(value, str) and value.startswith("$"):
        return evaluate(parse(value), scope)
    if isinstance(value, dict):
        return {k: _materialize(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize(v, scope) for v in value]
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce an upstream result into a plain dict for use as `$state`/`$result`.

    A real MCP `CallToolResult` (spec Appendix C: the proxy's executor calls
    the upstream client directly and gets one back) carries the tool's actual
    return value nested under `.structuredContent`, not at its own top level
    -- unwrap that first, so `$result.<path>`/`$state.<as>.<path>` resolve
    against the tool's real output instead of the MCP envelope. Falls back to
    a defensive `.get("result", content)` the way FastMCP sometimes nests a
    single return value, mirroring `examples/crm-mock`'s own test harness.
    """
    structured = getattr(value, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {"value": nested}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    return {"value": value}


@dataclass(frozen=True)
class StepResult:
    """The outcome of one committed step: enough to compensate it later."""

    step_seq: int
    tool: str
    args: dict[str, Any]
    result: Any
    compensation: dict[str, Any]


@dataclass(frozen=True)
class SagaStep:
    """One step of a saga to run (spec §8.2)."""

    tool: str
    args: dict[str, Any]
    contract: Contract | None


@dataclass(frozen=True)
class SagaReport:
    """Outcome of `SagaExecutor.run_saga` (spec §8.2)."""

    committed: list[StepResult]
    failed: BaseException | None
    compensated: list[int] = field(default_factory=list)


def _fire(hook: StageHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


@dataclass
class SagaExecutor:
    """Runs steps through the spec §8.1 lifecycle and materializes compensations."""

    ledger: LedgerStore
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)
    contract_set: ContractSet | None = None

    def _check_capture_is_read_only(self, capture_tool: str) -> None:
        if self.contract_set is None:
            return
        capture_contract = self.contract_set.resolve(capture_tool)
        if capture_contract is not None and any(e.type != "read" for e in capture_contract.effects):
            raise BelayError(
                "contract_invalid",
                {"tool": capture_tool, "reason": "capture tool's own contract must be read-only"},
            )

    async def run_step(
        self,
        session_id: str,
        step_seq: int,
        tool: str,
        args: dict[str, Any],
        contract: Contract | None,
        executor: Executor,
        *,
        context: dict[str, Any] | None = None,
        on_stage: StageHook | None = None,
        set_hash: str | None = None,
    ) -> StepResult:
        scope: Scope = {"args": args, "result": None, "context": context or {}, "state": {}}

        try:
            # 1. journaled
            self.ledger.append(
                session_id,
                "step_journaled",
                {
                    "tool": tool,
                    "args": args,
                    "contract": contract.tool if contract is not None else None,
                    "reversibility": contract.reversibility if contract is not None else None,
                },
                step_seq=step_seq,
                set_hash=set_hash,
            )
            _fire(on_stage, "journaled")

            # 2. capturing
            if contract is not None and contract.capture is not None:
                capture = contract.capture
                self._check_capture_is_read_only(capture.tool)
                capture_args = _materialize(capture.args, scope)
                snapshot = _as_dict(await executor(capture.tool, capture_args))
                scope["state"][capture.as_] = snapshot
                self.ledger.append(
                    session_id,
                    "state_captured",
                    {"as": capture.as_, "snapshot": snapshot},
                    step_seq=step_seq,
                    set_hash=set_hash,
                )
            _fire(on_stage, "capturing")

            # 3. calling
            idempotency_key: str | None = None
            if contract is not None and contract.idempotency_key is not None:
                idempotency_key = str(_materialize(contract.idempotency_key, scope))
            self.ledger.append(
                session_id,
                "tool_called",
                {"tool": tool, "args": args, "idempotency_key": idempotency_key},
                step_seq=step_seq,
                set_hash=set_hash,
            )
            if idempotency_key is not None:
                record = self.idempotency.begin(idempotency_key, session_id, step_seq)
                if record.status == "done":
                    result = record.result
                else:
                    result = await executor(tool, args)
                    self.idempotency.complete(idempotency_key, _as_dict(result))
            else:
                result = await executor(tool, args)
            _fire(on_stage, "calling")

            # 4. result_recorded
            scope["result"] = _as_dict(result)
            self.ledger.append(
                session_id,
                "result_recorded",
                {"tool": tool, "result": scope["result"]},
                step_seq=step_seq,
                set_hash=set_hash,
            )
            _fire(on_stage, "result_recorded")

            # 5. compensation_registered -- materialize now, never re-evaluate later.
            compensation = self._materialize_compensation(contract, scope)
            self.ledger.append(
                session_id,
                "compensation_registered",
                compensation,
                step_seq=step_seq,
                set_hash=set_hash,
            )
            _fire(on_stage, "compensation_registered")

            # 6. committed
            self.ledger.append(
                session_id, "step_committed", {"tool": tool}, step_seq=step_seq, set_hash=set_hash
            )
            _fire(on_stage, "committed")

            return StepResult(step_seq, tool, args, result, compensation)
        except BelayError as exc:
            self.ledger.append(
                session_id,
                "step_failed",
                {"tool": tool, "error": exc.to_dict()},
                step_seq=step_seq,
                set_hash=set_hash,
            )
            raise
        except Exception as exc:
            self.ledger.append(
                session_id,
                "step_failed",
                {"tool": tool, "error": str(exc)},
                step_seq=step_seq,
                set_hash=set_hash,
            )
            raise

    def _materialize_compensation(self, contract: Contract | None, scope: Scope) -> dict[str, Any]:
        if contract is None or contract.reversibility == "irreversible":
            return {"reversible": False, "reason": "irreversible"}
        if contract.reversibility == "conditional":
            assert contract.conditions is not None
            met = all(evaluate(parse(cond), scope) for cond in contract.conditions)
            if not met:
                # spec §4.2: unmet conditions at execution time -> irreversible,
                # even though the contract nominally declares an `undo`.
                return {"reversible": False, "reason": "conditional_unmet"}
        assert contract.undo is not None
        materialized_args = _materialize(contract.undo.args, scope)
        return {"reversible": True, "tool": contract.undo.tool, "args": materialized_args}

    async def compensate(
        self, session_id: str, step: StepResult, executor: Executor
    ) -> dict[str, Any] | None:
        """Execute one step's materialized compensation, if it has one.

        Delegates to `belay.rewind.service.compensate_one` (E7): one shared
        mini-step code path (journaled via `compensation_executed`/
        `compensation_failed`) for both this in-saga auto-unwind and the
        real `belay rewind` command, replaying the frozen args verbatim --
        never re-evaluating the undo expression.
        """
        from belay.rewind.service import compensate_one

        return await compensate_one(
            self.ledger, session_id, step.step_seq, step.compensation, executor
        )

    async def run_saga(
        self,
        session_id: str,
        steps: list[SagaStep],
        executor: Executor,
        *,
        auto_compensate: bool = False,
        context: dict[str, Any] | None = None,
    ) -> SagaReport:
        """Run `steps` in order (spec §8.2); on failure, optionally auto-unwind.

        Mirrors the session-level `auto_compensate: true` behavior of §8.2:
        on a step failure, compensations run in strict reverse `step_seq`
        order over the steps already committed in *this* saga run.
        """
        committed: list[StepResult] = []
        for i, step in enumerate(steps, start=1):
            try:
                outcome = await self.run_step(
                    session_id, i, step.tool, step.args, step.contract, executor, context=context
                )
            except Exception as exc:
                compensated: list[int] = []
                if auto_compensate:
                    for done in reversed(committed):
                        await self.compensate(session_id, done, executor)
                        compensated.append(done.step_seq)
                return SagaReport(committed=committed, failed=exc, compensated=compensated)
            committed.append(outcome)
        return SagaReport(committed=committed, failed=None)
