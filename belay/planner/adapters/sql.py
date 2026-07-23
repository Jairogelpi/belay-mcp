"""The `sql_simulator` dry-run basis (plan-v2 E11, docs/adr/0011-e11-sql-dry-run.md).

`simulate_row_count` runs a contract's `sql.statement` for real against an
actual database, inside a transaction that is **never committed** -- only
ever rolled back, on every code path, success or exception -- and returns the
real affected/matched row count. That is the whole trick: instead of a
declared guess (`basis="contract"`) or a bespoke `<tool>.dry_run` sibling
(`basis="native_dry_run"`), a DB-backed tool's plan can say "this really will
touch N rows" because it just did, and then undid it.

SQLite (already a project dependency via SQLAlchemy) is the tested target.
Postgres is not exercised against a live instance in this sandbox -- see the
honesty note in the ADR before relying on it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from belay.planner.model import SqlRunner


def simulate_row_count(engine: Engine, statement: str, params: dict[str, Any]) -> int:
    """Run `statement` with `params` bound, and always roll back.

    Never calls `commit` on any path -- `trans.rollback()` runs
    unconditionally in `finally`, so an exception raised mid-execute (a
    simulated "crash mid-simulation") still leaves the transaction rolled
    back before the connection closes. `SELECT` statements return the
    fetched row count; `UPDATE`/`DELETE` return the DBAPI's real
    `rowcount` -- computed by SQLite as part of executing the statement,
    even though nothing is ever persisted.
    """
    conn = engine.connect()
    trans = conn.begin()
    try:
        result = conn.execute(text(statement), params)
        if result.returns_rows:
            return len(result.fetchall())
        return result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
    finally:
        trans.rollback()
        conn.close()


def make_sql_runner(engine: Engine) -> SqlRunner:
    """Wrap `simulate_row_count` as the async `SqlRunner` `PlanningSession.sql_runner` expects.

    SQLite (and a local Postgres) round-trip fast enough in-process that
    there is no real concurrency to gain from a genuine async driver here.
    # ponytail: sync-in-async wrapper; swap for asyncpg/aiosqlite if this
    # ever needs to not block the event loop under real load.
    """

    async def _run(statement: str, params: dict[str, Any]) -> int:
        return simulate_row_count(engine, statement, params)

    return _run
