"""In-memory fake tool executors backing the conformance scenarios.

Fast on purpose: no subprocess, no real MCP transport. `examples/fs-server`
and `examples/crm-mock` already cover the real-subprocess path (E3/E6); the
conformance suite only needs *a* correct implementation of each contract's
tool semantics to exercise Belay's own governance logic.
"""

from __future__ import annotations

from typing import Any


def make_fs_executor() -> Any:
    """Backs `examples/contracts/fs.yaml`: `fs.read_file`/`write_file`/`delete_file`."""
    files: dict[str, str] = {}

    async def executor(tool: str, args: dict[str, Any]) -> Any:
        if tool == "fs.list_files":
            return sorted(files)
        path = args["path"]
        if tool == "fs.read_file":
            existed = path in files
            return {"path": path, "content": files.get(path, ""), "existed": existed}
        if tool == "fs.write_file":
            files[path] = args["content"]
            return {"path": path, "bytes": len(args["content"])}
        if tool == "fs.delete_file":
            existed = path in files
            files.pop(path, None)
            return {"path": path, "existed": existed}
        raise LookupError(f"fake fs executor has no tool {tool!r}")

    return executor


def make_crm_executor() -> tuple[Any, Any]:
    """Backs `examples/contracts/crm.yaml`. Returns `(executor, snapshot)`: `snapshot()` reads

    the record table directly, so tests can assert final state without routing a read
    through policy (`crm.get` is declared `irreversible`, which pauses by default -- a
    test-only introspection hook sidesteps that rather than fighting it with policy).
    """
    records: dict[str, dict[str, Any]] = {}

    async def executor(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "crm.get":
            record = records.get(args["id"])
            if record is None:
                return {"id": args["id"], "existed": False}
            return {"id": args["id"], "existed": True, "fields": dict(record)}
        if tool in ("crm.create", "crm.update"):
            records[args["id"]] = dict(args["fields"])
            return {"id": args["id"], "fields": dict(args["fields"])}
        if tool == "crm.delete":
            existed = args["id"] in records
            records.pop(args["id"], None)
            return {"id": args["id"], "existed": existed}
        raise LookupError(f"fake crm executor has no tool {tool!r}")

    def snapshot() -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in records.items()}

    return executor, snapshot


def make_email_executor() -> Any:
    """Backs `examples/contracts/email.yaml`: `email.send` (irreversible -- no undo)."""
    sent: list[dict[str, Any]] = []

    async def executor(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "email.send":
            sent.append(dict(args))
            return {"message_id": f"m_{len(sent)}", "to": args["to"]}
        raise LookupError(f"fake email executor has no tool {tool!r}")

    return executor
