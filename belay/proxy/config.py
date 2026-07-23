"""`belay wrap` config: which upstream server to run and which contracts govern it.

Written by `belay wrap`, read by `belay run` (plan.md E3). One JSON file per
wrapped upstream server (default `belay.wrap.json` in the current directory).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class UpstreamCommand(BaseModel):
    """How to launch the wrapped MCP server as a stdio subprocess."""

    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str] = []


class WrapConfig(BaseModel):
    """Persisted result of `belay wrap` (spec §4.6, §4.7; plan.md E3)."""

    model_config = ConfigDict(extra="forbid")

    belay_wrap: str = "0.1"
    upstream: UpstreamCommand
    contracts: list[str]
    unsafe_passthrough: list[str] = []
    db: str = "belay.db"
    # E14 (plan-v2): default identity for sessions of this wrapped server;
    # `belay run --initiated-by`/`--on-behalf-of` override it per-run.
    initiated_by: str = "unknown"
    on_behalf_of: str | None = None

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> WrapConfig:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
