"""belay/hooks/file_snapshot.py: native file-edit capture/restore for
rewind (E18.3, spec §9.2 FILE-001/002/004/005/006/008)."""

from __future__ import annotations

from pathlib import Path

from belay.hooks.file_snapshot import MAX_SNAPSHOT_BYTES, SnapshotStore
from sqlalchemy import create_engine


def _store(tmp_path: Path) -> SnapshotStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'snap.db'}", future=True)
    return SnapshotStore(engine, tmp_path / "snapshots")


def test_capture_of_a_new_file_records_did_not_exist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "new.txt"
    snap = store.capture_before("e1", "s1", target)
    assert snap.existed_before is False
    assert snap.before_hash is None
    assert snap.state == "captured"


def test_capture_of_an_existing_file_stores_its_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "existing.txt"
    target.write_text("original content", encoding="utf-8")

    snap = store.capture_before("e1", "s1", target)
    assert snap.existed_before is True
    assert snap.before_hash is not None
    assert snap.before_size == len(b"original content")


def test_capture_is_idempotent_for_the_same_event_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("v1", encoding="utf-8")
    first = store.capture_before("e1", "s1", target)

    target.write_text("v2 -- simulates the edit having already happened", encoding="utf-8")
    second = store.capture_before("e1", "s1", target)

    assert second == first  # NOT re-read against the now-changed file


def test_full_round_trip_restores_original_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    store.capture_before("e1", "s1", target)
    target.write_text("edited by the agent", encoding="utf-8")
    store.record_after("e1", target)

    outcome = store.restore("e1")
    assert "restored" in outcome
    assert target.read_text(encoding="utf-8") == "original"


def test_restore_of_a_newly_created_file_deletes_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "brand-new.txt"
    assert not target.exists()

    store.capture_before("e1", "s1", target)
    target.write_text("agent created this", encoding="utf-8")
    store.record_after("e1", target)

    outcome = store.restore("e1")
    assert "deleted" in outcome
    assert not target.exists()


def test_restore_refuses_on_conflict_when_file_changed_again_since(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    store.capture_before("e1", "s1", target)
    target.write_text("edited by the agent", encoding="utf-8")
    store.record_after("e1", target)

    # Something else touches the file again AFTER the recorded post-edit state.
    target.write_text("touched by someone else after the edit", encoding="utf-8")

    outcome = store.restore("e1")
    assert "conflict" in outcome
    assert target.read_text(encoding="utf-8") == "touched by someone else after the edit"


def test_restore_is_idempotent_once_already_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")
    store.capture_before("e1", "s1", target)
    target.write_text("edited", encoding="utf-8")
    store.record_after("e1", target)

    first = store.restore("e1")
    assert "restored" in first
    second = store.restore("e1")
    assert "already restored" in second


def test_restore_with_no_snapshot_reports_that_plainly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = store.restore("never-captured")
    assert "no snapshot" in outcome


def test_oversized_file_is_not_captured_but_does_not_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "huge.bin"
    target.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))

    snap = store.capture_before("e1", "s1", target)
    assert snap.state == "oversized"
    assert snap.before_hash is None

    outcome = store.restore("e1")
    assert "exceeded the capture size cap" in outcome


def test_identical_content_across_two_files_is_deduplicated_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same content", encoding="utf-8")
    b.write_text("same content", encoding="utf-8")

    snap_a = store.capture_before("e1", "s1", a)
    snap_b = store.capture_before("e2", "s1", b)
    assert snap_a.before_hash == snap_b.before_hash

    snapshots_dir = tmp_path / "snapshots"
    blob_files = list(snapshots_dir.rglob("*"))
    blob_files = [f for f in blob_files if f.is_file()]
    assert len(blob_files) == 1  # one blob, not two, for identical content


def test_restore_survives_a_fresh_store_instance_against_the_same_files(tmp_path: Path) -> None:
    """Durability: a NEW SnapshotStore (as a fresh supervisor process would
    construct) against the same engine/snapshots_dir must still be able to
    restore what an earlier instance captured."""
    engine = create_engine(f"sqlite:///{tmp_path / 'snap.db'}", future=True)
    snapshots_dir = tmp_path / "snapshots"
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")

    SnapshotStore(engine, snapshots_dir).capture_before("e1", "s1", target)
    target.write_text("edited", encoding="utf-8")
    SnapshotStore(engine, snapshots_dir).record_after("e1", target)

    reopened = SnapshotStore(engine, snapshots_dir)
    outcome = reopened.restore("e1")
    assert "restored" in outcome
    assert target.read_text(encoding="utf-8") == "original"
