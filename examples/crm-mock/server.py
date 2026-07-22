"""Toy in-memory CRM MCP server (examples/crm-mock, plan.md §2).

A minimal, real MCP server exposing get/create/update/delete/import/export
over an in-memory table of records. Used to exercise the saga executor (E6)
end to end: `SagaExecutor`'s 5-step/fail-at-4/auto_compensate acceptance test
runs against this server over real stdio MCP, not a mock.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("belay-crm-mock")

_records: dict[str, dict[str, Any]] = {}


@mcp.tool(name="crm.get", annotations=ToolAnnotations(readOnlyHint=True))
def get(id: str) -> dict[str, Any]:
    """Fetch one record by id. `existed: false` (and no `fields`) if absent."""
    record = _records.get(id)
    if record is None:
        return {"id": id, "existed": False}
    return {"id": id, "existed": True, "fields": dict(record)}


@mcp.tool(name="crm.create")
def create(id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create a record. Not read-only; overwrites if `id` already exists."""
    _records[id] = dict(fields)
    return {"id": id, "fields": dict(fields)}


@mcp.tool(name="crm.update")
def update(id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Overwrite a record's fields wholesale (used both forward and as undo)."""
    _records[id] = dict(fields)
    return {"id": id, "fields": dict(fields)}


@mcp.tool(name="crm.delete", annotations=ToolAnnotations(destructiveHint=True))
def delete(id: str) -> dict[str, Any]:
    """Delete a record. Marked destructive; a hint never authorizes on its own."""
    existed = id in _records
    _records.pop(id, None)
    return {"id": id, "existed": existed}


@mcp.tool(name="crm.import_records")
def import_records(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Bulk-create/overwrite records."""
    for rid, fields in records.items():
        _records[rid] = dict(fields)
    return {"count": len(records)}


@mcp.tool(name="crm.export_records", annotations=ToolAnnotations(readOnlyHint=True))
def export_records() -> dict[str, Any]:
    """Dump every record -- used by tests to assert the CRM's exact state."""
    return {"records": {rid: dict(fields) for rid, fields in _records.items()}}


@mcp.tool(name="crm.bulk_delete", annotations=ToolAnnotations(destructiveHint=True))
def bulk_delete(before_year: int) -> dict[str, Any]:
    """Delete every record with `last_seen < before_year` (plan.md §10 demo tool)."""
    to_delete = [
        rid
        for rid, fields in _records.items()
        if isinstance(fields.get("last_seen"), int) and fields["last_seen"] < before_year
    ]
    for rid in to_delete:
        del _records[rid]
    return {"deleted_ids": to_delete, "count": len(to_delete)}


if __name__ == "__main__":
    mcp.run()
