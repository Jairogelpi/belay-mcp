"""Canonical project identity, byte-lossless file snapshots, client targets,
the `.belay/connection.json` manifest schema, and a read-only inspection
result (E22 Task 2).

Pure data + pure functions only -- no subprocess calls, no official-CLI
invocation, no client registration. `belay/cli/client_registration.py`
(Task 3) and `belay/cli/connection.py` (Tasks 4-6) are the only callers
that actually touch a filesystem beyond reading/writing this manifest, or
spawn a process.

Project identity (`default_project_name`) has to be deterministic across
runs of the same project directory and, in practice, stable enough that a
second `belay connect` in the same directory recognizes its own prior
connection. It also has to tell apart two different directories that
happen to share a basename (`/a/foo` vs `/b/foo`) -- an ASCII slug of the
basename alone can collide, so every generated name carries an 8-hex-char
suffix hashed from the full, OS-canonicalized path.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from belay.cli.client_configs import atomic_write

#: Bumped whenever the on-disk shape of `.belay/connection.json` changes in
#: a way older code cannot safely interpret. `ConnectionManifest.from_dict`
#: rejects anything else outright rather than guessing -- a manifest is the
#: sole authority `disconnect`/`repair` trust for compare-and-swap removal,
#: so silently misreading one is worse than refusing to load it.
SCHEMA_VERSION = 1

ConnectionStatus = Literal["connecting", "connected", "rollback_incomplete", "disconnected"]

#: `healthy`: current bytes match the recorded post-write snapshot exactly.
#: `missing`: the target no longer exists (or never existed and should have).
#: `modified`: the target exists but its bytes no longer match what was
#:   recorded -- could be an unrelated hand-edit, could be a concurrent
#:   writer; the caller (`connection.py`) decides which given more context
#:   (e.g. did it change since *our* snapshot, or since a prior process's).
#: `conflict`: `connection.py`'s own compare-and-swap found the target
#:   changed out from under an in-flight operation -- a stronger claim than
#:   plain `modified` (this state is never derived from a single hash
#:   comparison alone, see `classify_target_state`'s docstring).
TargetState = Literal["healthy", "missing", "modified", "conflict"]


# --------------------------------------------------------------------------
# Project identity
# --------------------------------------------------------------------------

#: Names must be safe to hand, unquoted-ish, to `codex mcp add <name> --
#: ...` / `claude mcp add ... <name> -- ...` and to use as a JSON object key
#: (Claude Desktop's `mcpServers.<name>`) -- lowercase ASCII alphanumerics
#: and hyphens, must start and end with an alphanumeric, 1-63 chars (same
#: shape as a DNS label, a familiar and sufficient restriction).
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: Cap on the human-readable slug portion of a generated name (before the
#: `-<8 hex>` suffix) -- long enough to stay recognizable, short enough
#: that `<slug>-<8 hex>` never approaches `_NAME_RE`'s 63-char ceiling.
_SLUG_MAX_LEN = 32

_SLUG_TOKEN_RE = re.compile(r"[a-z0-9]+")


def validate_name(name: str) -> str:
    """Validate a user-supplied `--name`. Returns `name` unchanged, or raises
    `ValueError` with a message safe to print directly to the user."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid connection name {name!r}: must be 1-63 lowercase ASCII letters, "
            "digits, or hyphens, and must start and end with a letter or digit"
        )
    return name


def _canonicalize(path_str: str, *, platform: str) -> str:
    """Pure string transform behind `canonical_path_key` -- separated out so
    both branches (POSIX case-preserving, Windows case-folding) are directly
    testable without needing to actually run on each OS.

    Both branches normalize `\\` to `/` first ("separator normalization"):
    two spellings of the same real Windows path (`C:\\a\\b` from `str(Path)`
    vs `C:/a/b` from a POSIX-style construction) must hash identically, and
    doing this before the platform check keeps Windows' case-fold and
    POSIX's case-preserve branches symmetric.
    """
    normalized = path_str.replace("\\", "/")
    if platform == "win32":
        # Windows' filesystem is case-insensitive (NTFS default): normcase's
        # own semantics -- lowercase -- collapse `C:/Foo` and `c:/foo` to the
        # same identity, matching what the real filesystem treats as "the
        # same file".
        normalized = normalized.lower()
    return normalized


def canonical_path_key(project_dir: Path) -> str:
    """The exact string hashed into a project's identity: the resolved
    (symlinks/`..` collapsed) absolute path, separator-normalized, and
    case-folded only on Windows."""
    resolved = project_dir.resolve()
    return _canonicalize(str(resolved), platform=sys.platform)


def project_hash(project_dir: Path) -> str:
    """First 8 lowercase hex characters of the SHA-256 of
    `canonical_path_key` -- short enough to stay in a readable server name,
    long enough (32 bits) that two real projects colliding is not a
    practical concern for a single machine's set of connected projects."""
    key = canonical_path_key(project_dir)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _slugify(basename: str) -> str:
    ascii_only = basename.encode("ascii", "ignore").decode("ascii").lower()
    tokens = _SLUG_TOKEN_RE.findall(ascii_only)
    slug = "-".join(tokens) if tokens else "project"
    slug = slug[:_SLUG_MAX_LEN].rstrip("-")
    return slug or "project"


def default_project_name(project_dir: Path) -> str:
    """`belay connect`'s zero-config default `--name`: an ASCII slug of the
    directory's own basename, plus an 8-hex-char suffix derived from the
    full canonical path -- so two different directories that happen to
    share a basename (`/a/foo`, `/b/foo`) still get distinct names."""
    slug = _slugify(project_dir.resolve().name)
    return f"{slug}-{project_hash(project_dir)}"


# --------------------------------------------------------------------------
# File snapshots (compare-and-swap evidence)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSnapshot:
    """A point-in-time, byte-exact capture of one file (or its absence).
    `content_b64` (not raw text) so binary/non-UTF-8 content and exact
    newline/BOM bytes round-trip losslessly through JSON -- `atomic_restore`
    downstream needs the literal original bytes back, not a text
    re-encoding of them."""

    path: str
    existed: bool
    content_b64: str | None
    sha256: str | None  # "sha256:<hex>", matching client_configs.sha256_of's format

    @staticmethod
    def capture(path: Path) -> FileSnapshot:
        if not path.is_file():
            return FileSnapshot(path=str(path), existed=False, content_b64=None, sha256=None)
        raw = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return FileSnapshot(
            path=str(path),
            existed=True,
            content_b64=base64.b64encode(raw).decode("ascii"),
            sha256=digest,
        )

    def raw_bytes(self) -> bytes | None:
        return base64.b64decode(self.content_b64) if self.content_b64 is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "existed": self.existed,
            "content_b64": self.content_b64,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSnapshot:
        return cls(
            path=str(data["path"]),
            existed=bool(data["existed"]),
            content_b64=data.get("content_b64"),
            sha256=data.get("sha256"),
        )


def sha256_of_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def classify_target_state(
    *, recorded_after_sha256: str | None, current: FileSnapshot
) -> TargetState:
    """Compare a target's recorded post-write hash against its current
    on-disk state. Never returns `"conflict"` -- that state is reserved for
    `connection.py`'s own compare-and-swap decisions, which know more
    context (e.g. "changed since *this operation's* snapshot" vs "already
    different when we started") than a single hash comparison can express.
    """
    if not current.existed:
        return "missing"
    if recorded_after_sha256 is None or current.sha256 != recorded_after_sha256:
        return "modified"
    return "healthy"


# --------------------------------------------------------------------------
# Client targets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientTarget:
    """One official-client registration (or the Claude Desktop JSON-merge
    fallback) Belay owns for a connection: which client, what name it
    registered, which config file that touched, and the before/after
    snapshots needed for rollback and compare-and-swap removal."""

    client: str  # "codex" | "claude" | "claude-desktop"
    name: str
    config_path: str
    before: FileSnapshot
    after_sha256: str | None = None

    def with_after(self, after_sha256: str) -> ClientTarget:
        return ClientTarget(
            client=self.client,
            name=self.name,
            config_path=self.config_path,
            before=self.before,
            after_sha256=after_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "name": self.name,
            "config_path": self.config_path,
            "before": self.before.to_dict(),
            "after_sha256": self.after_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientTarget:
        return cls(
            client=str(data["client"]),
            name=str(data["name"]),
            config_path=str(data["config_path"]),
            before=FileSnapshot.from_dict(data["before"]),
            after_sha256=data.get("after_sha256"),
        )


# --------------------------------------------------------------------------
# Runtime (the generated, protected proxy) -- Task 4 fills these in
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeInfo:
    """The protected Belay proxy runtime `belay connect` generates under
    `<project>/.belay/` before registering any client -- `belay.wrap.json`,
    the ledger database, and the exact pinned upstream launch argv every
    registered client is pointed at."""

    wrap_path: str
    db_path: str
    contracts_path: str
    upstream_argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "wrap_path": self.wrap_path,
            "db_path": self.db_path,
            "contracts_path": self.contracts_path,
            "upstream_argv": list(self.upstream_argv),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeInfo:
        return cls(
            wrap_path=str(data["wrap_path"]),
            db_path=str(data["db_path"]),
            contracts_path=str(data["contracts_path"]),
            upstream_argv=tuple(str(a) for a in data["upstream_argv"]),
        )


# --------------------------------------------------------------------------
# The manifest -- .belay/connection.json
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ConnectionManifest:
    """The sole authority `belay disconnect`/`belay repair`/`belay doctor`
    trust for what Belay itself registered for this project and where --
    never re-derived by guessing from client config contents alone."""

    schema_version: int
    name: str
    project_dir: str
    status: ConnectionStatus
    created_at: str
    updated_at: str
    runtime: RuntimeInfo
    targets: tuple[ClientTarget, ...]
    hook_target: ClientTarget | None = None
    disconnected_at: str | None = None
    #: Set only while/after a failed transaction -- the original error
    #: `belay doctor`/`repair` surface instead of a generic "something's
    #: wrong" when `status == "rollback_incomplete"`.
    failure: str | None = None

    @staticmethod
    def manifest_path(project_dir: Path) -> Path:
        return project_dir / ".belay" / "connection.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "project_dir": self.project_dir,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "disconnected_at": self.disconnected_at,
            "failure": self.failure,
            "runtime": self.runtime.to_dict(),
            "targets": [t.to_dict() for t in self.targets],
            "hook_target": self.hook_target.to_dict() if self.hook_target else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionManifest:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported .belay/connection.json schema_version: {version!r} "
                f"(this belay understands only {SCHEMA_VERSION!r}) -- upgrade belay, or "
                "run `belay disconnect`/remove .belay/connection.json by hand to reset"
            )
        hook_target = data.get("hook_target")
        return cls(
            schema_version=version,
            name=str(data["name"]),
            project_dir=str(data["project_dir"]),
            status=data["status"],
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            disconnected_at=data.get("disconnected_at"),
            failure=data.get("failure"),
            runtime=RuntimeInfo.from_dict(data["runtime"]),
            targets=tuple(ClientTarget.from_dict(t) for t in data["targets"]),
            hook_target=ClientTarget.from_dict(hook_target) if hook_target else None,
        )

    def save(self, project_dir: Path) -> Path:
        """Atomically write this manifest (temp file + `os.replace`, via
        `client_configs.atomic_write` -- no half-written manifest ever left
        on disk, matching every other belay-managed config write)."""
        path = self.manifest_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def load(cls, project_dir: Path) -> ConnectionManifest | None:
        path = cls.manifest_path(project_dir)
        if not path.is_file():
            return None
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def evolve(self, **changes: Any) -> ConnectionManifest:
        """A copy with `changes` applied and `updated_at` refreshed --
        every manifest transition (connecting -> connected, etc.) goes
        through here so `updated_at` can never be forgotten."""
        changes.setdefault("updated_at", _utc_now_iso())
        return dataclasses.replace(self, **changes)

    @classmethod
    def new(
        cls,
        *,
        name: str,
        project_dir: Path,
        runtime: RuntimeInfo,
        targets: tuple[ClientTarget, ...] = (),
        hook_target: ClientTarget | None = None,
        status: ConnectionStatus = "connecting",
    ) -> ConnectionManifest:
        now = _utc_now_iso()
        return cls(
            schema_version=SCHEMA_VERSION,
            name=name,
            project_dir=str(project_dir),
            status=status,
            created_at=now,
            updated_at=now,
            runtime=runtime,
            targets=targets,
            hook_target=hook_target,
        )


# --------------------------------------------------------------------------
# Read-only inspection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetInspection:
    client: str
    name: str
    config_path: str
    state: TargetState
    detail: str = ""


@dataclass(frozen=True)
class ConnectionInspection:
    """A read-only report of a project's connection health -- produced by
    `inspect_connection` (Task 5), never by anything that writes. `belay
    doctor` renders this directly; `belay repair` uses it to decide what's
    safely repairable vs. an unresolved conflict."""

    name: str
    project_dir: str
    manifest_status: ConnectionStatus | None  # None: no manifest at all
    runtime_state: TargetState
    targets: tuple[TargetInspection, ...]
    hook_state: TargetState | None = None
    failure: str | None = None

    @property
    def rollback_incomplete(self) -> bool:
        return self.manifest_status == "rollback_incomplete"

    @property
    def has_conflict(self) -> bool:
        return any(t.state == "conflict" for t in self.targets)

    @property
    def healthy(self) -> bool:
        if self.manifest_status != "connected":
            return False
        if self.runtime_state != "healthy":
            return False
        if self.hook_state is not None and self.hook_state != "healthy":
            return False
        return all(t.state == "healthy" for t in self.targets)
