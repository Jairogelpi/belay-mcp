"""`BelayConformanceTarget`: the reference adapter, wired straight to `belay/`.

Reuses the real production wiring (`belay.proxy.lifecycle.Lifecycle`,
`belay.executor.saga.SagaExecutor`, `belay.rewind.service.RewindService`)
rather than re-implementing any governance logic -- the conformance suite
must exercise the actual code paths, not a parallel model of them.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from belay.contracts.loader import load_contract_set
from belay.executor.saga import SagaExecutor
from belay.ledger.model import Event
from belay.ledger.store import LedgerStore
from belay.policy.model import PolicyDoc, default_policy, load_policy
from belay.proxy.lifecycle import Lifecycle
from belay.rewind.service import RewindService

from conformance.target import Executor

_session_ids = (f"conf_{n}" for n in itertools.count(1))


@dataclass
class _Session:
    lifecycle: Lifecycle
    executor: Executor


class BelayConformanceTarget:
    """Drives this repo's own implementation -- the "does Belay conform to
    Belay" sanity check, and the default for `belay-conformance run --target belay`.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def new_session(
        self,
        contract_paths: list[Path],
        executor: Executor,
        *,
        policy_path: Path | None = None,
    ) -> str:
        session_id = next(_session_ids)
        contract_set = load_contract_set(contract_paths)
        policy: PolicyDoc = load_policy(policy_path) if policy_path else default_policy()
        ledger = LedgerStore()
        lifecycle = Lifecycle(
            contract_set=contract_set,
            unsafe_passthrough_tools=frozenset(),
            ledger=ledger,
            session_id=session_id,
            policy=policy,
        )
        lifecycle.start_session()
        self._sessions[session_id] = _Session(lifecycle, executor)
        return session_id

    async def call(
        self, session_id: str, tool: str, args: dict[str, Any], *, read_only_hint: bool = False
    ) -> Any:
        sess = self._sessions[session_id]
        return await sess.lifecycle.govern_and_execute(
            tool, args, read_only_hint=read_only_hint, executor=sess.executor
        )

    def approve(
        self, session_id: str, approval_id: str, *, approved_by: str = "conformance-suite"
    ) -> None:
        sess = self._sessions[session_id]
        assert sess.lifecycle.approval_stage is not None
        sess.lifecycle.approval_stage.queue.approve(approval_id, approved_by)

    def ledger(self, session_id: str) -> list[Event]:
        return self._sessions[session_id].lifecycle.ledger.read(session_id)

    async def run_saga(
        self, session_id: str, steps: list[Any], *, auto_compensate: bool = True
    ) -> Any:
        sess = self._sessions[session_id]
        saga = SagaExecutor(
            ledger=sess.lifecycle.ledger, contract_set=sess.lifecycle.contract_set
        )
        return await saga.run_saga(
            session_id, steps, sess.executor, auto_compensate=auto_compensate
        )

    async def rewind(
        self, session_id: str, *, dry_run: bool = False, by: str = "conformance-suite"
    ) -> Any:
        sess = self._sessions[session_id]
        assert sess.lifecycle.approval_stage is not None
        service = RewindService(
            ledger=sess.lifecycle.ledger,
            contract_set=sess.lifecycle.contract_set,
            approvals=sess.lifecycle.approval_stage.queue,
        )
        return await service.rewind(session_id, sess.executor, dry_run=dry_run, by=by)
