"""E22 Task 2: canonical project identity + the `.belay/connection.json`
manifest schema -- pure data/pure functions, no subprocess, no client
registration (that's Task 3/5)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from belay.cli.connection_models import (
    SCHEMA_VERSION,
    ClientTarget,
    ConnectionInspection,
    ConnectionManifest,
    FileSnapshot,
    RuntimeInfo,
    TargetInspection,
    _canonicalize,
    canonical_path_key,
    classify_target_state,
    default_project_name,
    project_hash,
    validate_name,
)

# --------------------------------------------------------------------------
# Path/name identity
# --------------------------------------------------------------------------


def test_posix_canonicalization_preserves_case() -> None:
    lower = _canonicalize("/Users/dev/MyProject", platform="linux")
    upper = _canonicalize("/Users/dev/myproject", platform="linux")
    assert lower != upper
    assert lower == "/Users/dev/MyProject"


def test_windows_canonicalization_case_folds() -> None:
    a = _canonicalize("C:/Users/dev/MyProject", platform="win32")
    b = _canonicalize("C:/Users/dev/myproject", platform="win32")
    assert a == b == "c:/users/dev/myproject"


def test_separator_normalization_backslash_and_forward_slash_match() -> None:
    backslash = _canonicalize("C:\\Users\\dev\\proj", platform="win32")
    forward = _canonicalize("C:/Users/dev/proj", platform="win32")
    assert backslash == forward


def test_ascii_slug_collapses_and_truncates(tmp_path: Path) -> None:
    weird = tmp_path / "  My_Cool!! Project 42 -- ünïcödé  "
    weird.mkdir()
    name = default_project_name(weird)
    slug = name.rsplit("-", 1)[0]
    assert slug.islower() or slug.isdigit() or "-" in slug
    assert all(c.islower() or c.isdigit() or c == "-" for c in slug)
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    long_dir = tmp_path / ("x" * 200)
    long_dir.mkdir()
    long_name = default_project_name(long_dir)
    long_slug = long_name.rsplit("-", 1)[0]
    assert len(long_slug) <= 32


def test_project_hash_is_first_eight_lowercase_hex_chars(tmp_path: Path) -> None:
    h = project_hash(tmp_path)
    assert len(h) == 8
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)

    import hashlib

    expected = hashlib.sha256(canonical_path_key(tmp_path).encode("utf-8")).hexdigest()[:8]
    assert h == expected


def test_distinct_directories_with_same_basename_get_distinct_names(tmp_path: Path) -> None:
    a = tmp_path / "a" / "foo"
    b = tmp_path / "b" / "foo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    name_a = default_project_name(a)
    name_b = default_project_name(b)
    assert name_a != name_b
    assert name_a.startswith("foo-")
    assert name_b.startswith("foo-")


@pytest.mark.parametrize(
    "name",
    ["my-project", "a", "proj123", "a-b-c-d", "x" * 63],
)
def test_validate_name_accepts_valid(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "-abc", "abc-", "ABC", "my_project", "my project", "x" * 64, "a..b"],
)
def test_validate_name_rejects_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        validate_name(name)


# --------------------------------------------------------------------------
# FileSnapshot
# --------------------------------------------------------------------------


def test_file_snapshot_of_missing_file(tmp_path: Path) -> None:
    snap = FileSnapshot.capture(tmp_path / "nope.json")
    assert snap.existed is False
    assert snap.content_b64 is None
    assert snap.sha256 is None
    assert snap.raw_bytes() is None


def test_file_snapshot_round_trips_bytes_losslessly(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    raw = b'{"a": 1}\r\n\xff\xfe binary-ish tail'
    target.write_bytes(raw)
    snap = FileSnapshot.capture(target)
    assert snap.existed is True
    assert snap.raw_bytes() == raw
    assert snap.sha256 is not None and snap.sha256.startswith("sha256:")

    restored = FileSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert restored == snap
    assert base64.b64decode(restored.content_b64) == raw


def test_classify_target_state() -> None:
    missing = FileSnapshot(path="x", existed=False, content_b64=None, sha256=None)
    assert classify_target_state(recorded_after_sha256="sha256:aaa", current=missing) == "missing"

    present = FileSnapshot(path="x", existed=True, content_b64="Zm9v", sha256="sha256:aaa")
    assert classify_target_state(recorded_after_sha256="sha256:aaa", current=present) == "healthy"

    changed = FileSnapshot(path="x", existed=True, content_b64="YmFy", sha256="sha256:bbb")
    assert classify_target_state(recorded_after_sha256="sha256:aaa", current=changed) == "modified"
    assert classify_target_state(recorded_after_sha256=None, current=present) == "modified"


# --------------------------------------------------------------------------
# ConnectionManifest
# --------------------------------------------------------------------------


def _runtime(project_dir: Path) -> RuntimeInfo:
    return RuntimeInfo(
        wrap_path=str(project_dir / ".belay" / "belay.wrap.json"),
        db_path=str(project_dir / ".belay" / "belay.db"),
        contracts_path=str(project_dir / ".belay" / "contracts.yaml"),
        upstream_argv=(
            "npx", "-y", "@modelcontextprotocol/server-filesystem@2026.7.10", str(project_dir),
        ),
    )


def _target(client: str, name: str) -> ClientTarget:
    path = f"/fake/{client}/config"
    return ClientTarget(
        client=client,
        name=name,
        config_path=path,
        before=FileSnapshot(path=path, existed=False, content_b64=None, sha256=None),
    )


@pytest.mark.parametrize(
    "status", ["connecting", "connected", "rollback_incomplete", "disconnected"]
)
def test_manifest_round_trips_every_status(tmp_path: Path, status: str) -> None:
    manifest = ConnectionManifest.new(
        name="proj-aabbccdd", project_dir=tmp_path, runtime=_runtime(tmp_path),
        targets=(_target("codex", "proj-aabbccdd"),),
    ).evolve(status=status)
    path = manifest.save(tmp_path)
    assert path == tmp_path / ".belay" / "connection.json"

    loaded = ConnectionManifest.load(tmp_path)
    assert loaded is not None
    assert loaded.status == status
    assert loaded == manifest


def test_manifest_snapshot_bytes_are_lossless_through_a_full_save_load_cycle(
    tmp_path: Path,
) -> None:
    raw = b"line1\r\nline2\nno-trailing-newline\xef\xbb\xbf"
    target = FileSnapshot(
        path="/fake/codex/config",
        existed=True,
        content_b64=base64.b64encode(raw).decode("ascii"),
        sha256="sha256:" + __import__("hashlib").sha256(raw).hexdigest(),
    )
    ct = ClientTarget(client="codex", name="proj", config_path="/fake/codex/config", before=target)
    manifest = ConnectionManifest.new(
        name="proj", project_dir=tmp_path, runtime=_runtime(tmp_path), targets=(ct,)
    )
    manifest.save(tmp_path)

    loaded = ConnectionManifest.load(tmp_path)
    assert loaded is not None
    assert loaded.targets[0].before.raw_bytes() == raw


def test_manifest_post_write_hash_recorded_on_targets(tmp_path: Path) -> None:
    ct = _target("claude", "proj")
    ct2 = ct.with_after("sha256:deadbeef")
    assert ct.after_sha256 is None
    assert ct2.after_sha256 == "sha256:deadbeef"

    manifest = ConnectionManifest.new(
        name="proj", project_dir=tmp_path, runtime=_runtime(tmp_path), targets=(ct2,)
    )
    manifest.save(tmp_path)
    loaded = ConnectionManifest.load(tmp_path)
    assert loaded is not None
    assert loaded.targets[0].after_sha256 == "sha256:deadbeef"


def test_manifest_timestamps_are_utc_iso(tmp_path: Path) -> None:
    manifest = ConnectionManifest.new(name="proj", project_dir=tmp_path, runtime=_runtime(tmp_path))
    assert manifest.created_at.endswith("+00:00")
    assert manifest.updated_at.endswith("+00:00")

    later = manifest.evolve(status="connected")
    assert later.updated_at.endswith("+00:00")
    assert later.updated_at >= manifest.created_at
    assert later.created_at == manifest.created_at  # created_at never changes on evolve


def test_manifest_rejects_unknown_schema_version(tmp_path: Path) -> None:
    manifest = ConnectionManifest.new(name="proj", project_dir=tmp_path, runtime=_runtime(tmp_path))
    data = manifest.to_dict()
    data["schema_version"] = SCHEMA_VERSION + 1
    manifest_path = tmp_path / ".belay" / "connection.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        ConnectionManifest.load(tmp_path)


def test_manifest_load_returns_none_when_absent(tmp_path: Path) -> None:
    assert ConnectionManifest.load(tmp_path) is None


def test_manifest_hook_target_optional(tmp_path: Path) -> None:
    manifest = ConnectionManifest.new(name="proj", project_dir=tmp_path, runtime=_runtime(tmp_path))
    assert manifest.hook_target is None
    manifest.save(tmp_path)
    assert ConnectionManifest.load(tmp_path).hook_target is None  # type: ignore[union-attr]

    hooked = manifest.evolve(hook_target=_target("claude-code-hooks", "proj"))
    hooked.save(tmp_path)
    loaded = ConnectionManifest.load(tmp_path)
    assert loaded is not None and loaded.hook_target is not None
    assert loaded.hook_target.client == "claude-code-hooks"


# --------------------------------------------------------------------------
# ConnectionInspection
# --------------------------------------------------------------------------


def test_connection_inspection_reports_per_target_state() -> None:
    inspection = ConnectionInspection(
        name="proj",
        project_dir="/fake/proj",
        manifest_status="connected",
        runtime_state="healthy",
        targets=(
            TargetInspection(client="codex", name="proj", config_path="/x", state="healthy"),
            TargetInspection(
                client="claude", name="proj", config_path="/y", state="modified",
                detail="hand-edited",
            ),
        ),
    )
    assert inspection.healthy is False  # one target modified
    assert inspection.rollback_incomplete is False
    assert inspection.has_conflict is False


def test_connection_inspection_healthy_requires_everything_healthy() -> None:
    inspection = ConnectionInspection(
        name="proj",
        project_dir="/fake/proj",
        manifest_status="connected",
        runtime_state="healthy",
        targets=(
            TargetInspection(client="codex", name="proj", config_path="/x", state="healthy"),
        ),
        hook_state="healthy",
    )
    assert inspection.healthy is True


def test_connection_inspection_rollback_incomplete_and_conflict_flags() -> None:
    inspection = ConnectionInspection(
        name="proj",
        project_dir="/fake/proj",
        manifest_status="rollback_incomplete",
        runtime_state="healthy",
        targets=(
            TargetInspection(client="codex", name="proj", config_path="/x", state="conflict"),
        ),
        failure="codex registration failed",
    )
    assert inspection.rollback_incomplete is True
    assert inspection.has_conflict is True
    assert inspection.healthy is False
