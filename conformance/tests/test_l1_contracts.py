"""L1 -- contracts & default rule (spec §4, §9.1).

@conformance(level=1)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.errors import BelayError

from conformance.target import ConformanceTarget
from conformance.tests.fakes import make_fs_executor

pytestmark = [pytest.mark.anyio, pytest.mark.l1]

REPO_ROOT = Path(__file__).resolve().parents[2]
FS_CONTRACT = REPO_ROOT / "examples" / "contracts" / "fs.yaml"


async def test_declared_reversible_call_succeeds_and_is_ledgered(
    target: ConformanceTarget,
) -> None:
    session_id = target.new_session([FS_CONTRACT], make_fs_executor())
    await target.call(session_id, "fs.write_file", {"path": "a.txt", "content": "hi"})

    events = [e.type for e in target.ledger(session_id)]
    assert "step_journaled" in events
    assert "result_recorded" in events
    assert "compensation_registered" in events


async def test_undeclared_non_readonly_tool_is_refused(target: ConformanceTarget) -> None:
    """spec §4.6: no contract, no `readOnlyHint`, no `unsafe_passthrough` -> `contract_missing`."""
    session_id = target.new_session([FS_CONTRACT], make_fs_executor())
    with pytest.raises(BelayError) as exc_info:
        await target.call(session_id, "fs.rename_file", {"path": "a.txt"})
    assert exc_info.value.code == "contract_missing"


async def test_undeclared_readonly_hinted_tool_is_allowed_as_implicit_read(
    target: ConformanceTarget,
) -> None:
    """spec §4.6: `readOnlyHint: true` with no contract => implicit `effects: []`, allow."""
    session_id = target.new_session([FS_CONTRACT], make_fs_executor())
    result = await target.call(
        session_id, "fs.list_files", {}, read_only_hint=True
    )
    assert result is not None


async def test_ledger_chain_is_internally_verifiable(target: ConformanceTarget) -> None:
    """spec §9.2: the hash chain recomputes cleanly for an untouched session."""
    from belay.ledger.verify import verify_chain

    session_id = target.new_session([FS_CONTRACT], make_fs_executor())
    await target.call(session_id, "fs.write_file", {"path": "a.txt", "content": "hi"})

    report = verify_chain(target.ledger(session_id))
    assert report.ok, report.errors
