"""Tests for the `sql_simulator` dry-run basis (plan-v2 E11).

Real SQLite fixture DB, real rows -- no mocking of SQLAlchemy or sqlite3.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.planner.adapters.sql import make_sql_runner, simulate_row_count
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY, last_seen INTEGER)"))
    return engine


def _seed_rows(engine: Engine, last_seen_values: list[int]) -> None:
    with engine.begin() as conn:
        for value in last_seen_values:
            conn.execute(text("INSERT INTO records (last_seen) VALUES (:v)"), {"v": value})


def _total_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM records")).scalar_one()


def test_delete_dry_run_matches_real_row_count_and_db_is_unchanged(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    _seed_rows(engine, [2020, 2020, 2020, 2024])  # 3 stale, 1 fresh

    before = _total_rows(engine)
    count = simulate_row_count(
        engine, "DELETE FROM records WHERE last_seen < :cutoff", {"cutoff": 2023}
    )
    after = _total_rows(engine)

    assert count == 3
    assert after == before  # provably unchanged: rolled back, never committed


def test_zero_matching_rows_is_an_honest_zero_not_an_error(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    _seed_rows(engine, [2024, 2025])

    count = simulate_row_count(
        engine, "UPDATE records SET last_seen = 1999 WHERE last_seen < :cutoff", {"cutoff": 2000}
    )

    assert count == 0


def test_select_statement_counts_returned_rows(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    _seed_rows(engine, [2020, 2021, 2024])

    count = simulate_row_count(
        engine, "SELECT * FROM records WHERE last_seen < :cutoff", {"cutoff": 2023}
    )

    assert count == 2


def test_crash_mid_simulation_leaves_db_unchanged(tmp_path: Path) -> None:
    """Kill the connection mid-simulation (no commit, no explicit rollback).

    This is the safety property `simulate_row_count`'s `finally: rollback()`
    depends on: an *uncommitted* SQLite/DBAPI transaction that is torn down
    (connection closed, process killed) rolls back on its own. We exercise
    that guarantee directly, without going through the well-behaved
    `finally` path, to prove the DB is unchanged even in the worst case.
    """
    engine = _make_engine(tmp_path)
    _seed_rows(engine, [2020, 2020, 2020, 2020, 2020])
    before = _total_rows(engine)

    conn = engine.connect()
    conn.begin()
    conn.execute(text("DELETE FROM records WHERE last_seen < :cutoff"), {"cutoff": 2023})
    conn.close()  # simulated crash: no commit(), no explicit rollback() call

    after = _total_rows(engine)
    assert after == before == 5


async def test_make_sql_runner_wraps_simulate_row_count_as_async_callable(
    tmp_path: Path,
) -> None:
    engine = _make_engine(tmp_path)
    _seed_rows(engine, [2020, 2024])
    runner = make_sql_runner(engine)

    count = await runner("DELETE FROM records WHERE last_seen < :cutoff", {"cutoff": 2023})

    assert count == 1
    assert _total_rows(engine) == 2
