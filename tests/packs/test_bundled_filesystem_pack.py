"""E22 Task 1: the pinned Filesystem pack must be resolvable from a real,
installed `belay` package (via `importlib.resources`, not a repo-relative
path) -- `belay connect`'s zero-config flow has no guarantee a repo
checkout is anywhere nearby. Also asserts the packaged bytes never quietly
drift from the canonical, developer-facing `packs/filesystem/` files this
repo's own pack tests (`tests/packs/test_filesystem_pack.py`) exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from belay.bundled_packs import PINNED_VERSION, UPSTREAM_IDENTITY, _check_pin, filesystem_pack
from belay.contracts.model import ContractSet

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_PACK_YAML = REPO_ROOT / "packs" / "filesystem" / "pack.yaml"
CANONICAL_CONTRACTS_YAML = REPO_ROOT / "packs" / "filesystem" / "contracts.yaml"


def test_resolves_through_importlib_resources_under_belay_packs() -> None:
    pack = filesystem_pack()
    assert pack.pack_yaml_path.is_file()
    assert pack.contracts_path.is_file()
    assert pack.pack_yaml_path.parent.name == "filesystem"
    assert pack.pack_yaml_path.parent.parent.name == "packs"


def test_metadata_pins_the_expected_upstream_version() -> None:
    pack = filesystem_pack()
    assert pack.upstream_identity == "@modelcontextprotocol/server-filesystem"
    assert pack.pinned_version == "2026.7.10"
    assert UPSTREAM_IDENTITY == "@modelcontextprotocol/server-filesystem"
    assert PINNED_VERSION == "2026.7.10"


def test_contracts_load_through_the_real_loader() -> None:
    pack = filesystem_pack()
    contract_set = pack.load_contracts()
    assert isinstance(contract_set, ContractSet)
    assert "write_file" in contract_set.contracts
    assert "read_file" in contract_set.contracts
    assert contract_set.resolve("create_directory") is not None


def test_packaged_bytes_match_the_canonical_repo_root_pack_files() -> None:
    pack = filesystem_pack()
    assert pack.pack_yaml_path.read_bytes() == CANONICAL_PACK_YAML.read_bytes()
    assert pack.contracts_path.read_bytes() == CANONICAL_CONTRACTS_YAML.read_bytes()


def test_upstream_launch_argv_is_exact_and_pinned(tmp_path: Path) -> None:
    pack = filesystem_pack()
    argv = pack.upstream_launch_argv(tmp_path)
    assert argv == (
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem@2026.7.10",
        str(tmp_path),
    )


def test_drift_between_metadata_and_pin_is_rejected() -> None:
    with pytest.raises(ValueError, match="drift"):
        _check_pin("@modelcontextprotocol/server-filesystem", "9.9.9")
    with pytest.raises(ValueError, match="drift"):
        _check_pin("some-other-package", "2026.7.10")
    _check_pin("@modelcontextprotocol/server-filesystem", "2026.7.10")  # does not raise


def test_bundled_pack_yaml_is_valid_yaml_and_matches_constants() -> None:
    pack = filesystem_pack()
    metadata = yaml.safe_load(pack.pack_yaml_path.read_text(encoding="utf-8"))
    assert metadata["upstream"]["identity"] == UPSTREAM_IDENTITY
    assert metadata["upstream"]["verified_version"] == PINNED_VERSION
