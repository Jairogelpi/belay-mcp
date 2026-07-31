"""Native file-edit snapshot capture for rewind (E18.3,
FILE-001/002/004/005/006/008 -- not a `docs/spec.md` §9.2 citation, see
docs/adr/0020-extended-requirement-catalog.md). Captures enough of a
file's pre-edit state
to restore it later via `belay hooks rewind <event_id>` -- content-addressed
blob storage under `belay_home()/snapshots/`, indexed by
`belay/db/models.py`'s `FileSnapshotRow` (shares the same SQLite engine as
`ApprovalQueue`/`IdempotencyStore`/`LedgerStore`).

Native Edit/Write calls are ALLOWED by default (not paused like Bash) --
blocking ordinary file edits for approval would make the gate unusable for
a real coding session, and the actual safety value here is being able to
UNDO an edit afterward, not gating routine ones. A file whose content
exceeds `MAX_SNAPSHOT_BYTES` is the one case that still isn't captured
(FILE-006: "exceeding capture limits downgrades reversibility"); the edit
itself is still allowed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from belay.db.models import Base, FileSnapshotRow

#: Above this, a file's content is not captured at all. Chosen generously
#: (most source files are far smaller); still bounded, because an unbounded
#: cap would let a single huge file exhaust disk space in the snapshot
#: store.
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024  # 5 MiB


@dataclass(frozen=True)
class FileSnapshot:
    event_id: str
    session_id: str
    path: str
    existed_before: bool
    before_hash: str | None
    before_size: int | None
    state: str
    after_hash: str | None
    captured_at: str
    restored_at: str | None


def _row_to_snapshot(row: FileSnapshotRow) -> FileSnapshot:
    return FileSnapshot(
        event_id=row.event_id,
        session_id=row.session_id,
        path=row.path,
        existed_before=row.existed_before,
        before_hash=row.before_hash,
        before_size=row.before_size,
        state=row.state,
        after_hash=row.after_hash,
        captured_at=row.captured_at,
        restored_at=row.restored_at,
    )


def _blob_path(snapshots_dir: Path, content_hash: str) -> Path:
    return snapshots_dir / content_hash[:2] / content_hash


def _write_blob_if_absent(snapshots_dir: Path, data: bytes) -> str:
    content_hash = hashlib.sha256(data).hexdigest()
    blob_path = _blob_path(snapshots_dir, content_hash)
    if not blob_path.is_file():
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob_path.with_name(f"{blob_path.name}.tmp-{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, blob_path)
    return content_hash


class SnapshotStore:
    def __init__(self, engine: Engine, snapshots_dir: Path) -> None:
        self._engine = engine
        Base.metadata.create_all(self._engine)
        self._snapshots_dir = snapshots_dir

    def get(self, event_id: str) -> FileSnapshot | None:
        with DBSession(self._engine) as db:
            row = db.get(FileSnapshotRow, event_id)
            return None if row is None else _row_to_snapshot(row)

    def capture_before(self, event_id: str, session_id: str, path: Path) -> FileSnapshot:
        """Idempotent: capturing the same `event_id` twice (a retried
        PreToolUse) returns the ALREADY-recorded capture rather than
        re-reading the file -- by the time of a retry the file may already
        reflect the edit, and the first capture is the one that actually
        preceded it."""
        existing = self.get(event_id)
        if existing is not None:
            return existing

        existed_before = path.is_file()
        before_hash: str | None = None
        before_size: int | None = None
        state = "captured"
        if existed_before:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is not None and size > MAX_SNAPSHOT_BYTES:
                state = "oversized"
                before_size = size
            else:
                try:
                    data = path.read_bytes()
                except OSError:
                    state = "oversized"
                else:
                    before_size = len(data)
                    before_hash = _write_blob_if_absent(self._snapshots_dir, data)

        row = FileSnapshotRow(
            event_id=event_id,
            session_id=session_id,
            path=str(path),
            existed_before=existed_before,
            before_hash=before_hash,
            before_size=before_size,
            state=state,
            after_hash=None,
            captured_at=datetime.now(UTC).isoformat(),
            restored_at=None,
        )
        with DBSession(self._engine) as db:
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # lost a race against a concurrent capture for the same event_id

        winner = self.get(event_id)
        assert winner is not None
        return winner

    def record_after(self, event_id: str, path: Path) -> None:
        """PostToolUse: confirms the edit landed and records the resulting
        hash -- what a later rewind's conflict check (FILE-004) compares
        the CURRENT file against before restoring."""
        after_hash: str | None = None
        if path.is_file():
            try:
                after_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                after_hash = None
        with DBSession(self._engine) as db:
            row = db.get(FileSnapshotRow, event_id)
            if row is None or row.state == "oversized":
                return
            row.after_hash = after_hash
            row.state = "recorded"
            db.commit()

    def restore(self, event_id: str) -> str:
        """Rewind one captured edit. Returns a human-readable outcome
        string; never raises for an ordinary conflict (that's an expected,
        reportable outcome, not a program error).

        FILE-004: the CURRENT file must match the recorded post-edit hash
        before restoring -- if something touched the file again since (the
        agent, the user, another process), overwriting it now would destroy
        THAT change. Refuse, don't guess.

        FILE-005: a file that didn't exist before the edit is restored by
        *deleting* it, not by writing empty bytes -- a distinct
        compensation from restoring prior content.
        """
        snapshot = self.get(event_id)
        if snapshot is None:
            return "no snapshot recorded for this event_id"
        if snapshot.state == "restored":
            return f"already restored at {snapshot.restored_at}"
        if snapshot.state == "oversized":
            return "file exceeded the capture size cap -- nothing was ever snapshotted"
        if snapshot.state != "recorded":
            return f"snapshot is in state {snapshot.state!r}, not ready to restore"

        target = Path(snapshot.path)
        current_hash = None
        if target.is_file():
            try:
                current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                return f"could not read {target} to check for a conflict"

        if current_hash != snapshot.after_hash:
            return (
                f"conflict: {target} has changed since the recorded post-edit state -- "
                "refusing to overwrite; resolve manually"
            )

        if not snapshot.existed_before:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                return f"could not delete {target}: {exc}"
            outcome = f"deleted {target} (it did not exist before the original edit)"
        else:
            assert snapshot.before_hash is not None
            blob = _blob_path(self._snapshots_dir, snapshot.before_hash)
            if not blob.is_file():
                return f"snapshot blob for {target} is missing from {blob} -- cannot restore"
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.belay-restore-tmp")
            try:
                tmp.write_bytes(blob.read_bytes())
                os.replace(tmp, target)
            except OSError as exc:
                return f"could not write {target}: {exc}"
            outcome = f"restored {target} to its pre-edit content"

        with DBSession(self._engine) as db:
            row = db.get(FileSnapshotRow, event_id)
            if row is not None:
                row.state = "restored"
                row.restored_at = datetime.now(UTC).isoformat()
                db.commit()
        return outcome
