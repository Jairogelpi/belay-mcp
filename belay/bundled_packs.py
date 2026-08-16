"""Resolves and validates the pinned Filesystem pack Belay ships inside its
own wheel (E22), so `belay connect` works against a real `pip install
belay-mcp` with no repo checkout nearby -- unlike the developer-facing
`packs/filesystem/` at the repo root, which is not shipped (see
`belay/packs/__init__.py`).

`filesystem_pack()` is the one function `belay/cli/connection.py` calls;
everything else here exists to make that call fail loudly, not silently,
on drift between this module's hardcoded upstream pin and the packaged
`pack.yaml`'s own metadata -- two independent statements of the same fact
(the exact pinned launch command E22 registers with Codex/Claude) that
must never quietly diverge.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path

import yaml

from belay.contracts.loader import load_contract_set
from belay.contracts.model import ContractSet

#: The exact upstream package identity and version this bundled pack was
#: verified against (spec §11.2 `upstream.identity`/`upstream.verified_version`
#: in `packs/filesystem/pack.yaml`, copied byte-for-byte into
#: `belay/packs/filesystem/pack.yaml`). Restated here, independently of the
#: YAML, so `_check_pin` can catch the packaged file drifting out from under
#: this module (or vice versa) instead of one silently winning.
UPSTREAM_IDENTITY = "@modelcontextprotocol/server-filesystem"
PINNED_VERSION = "2026.7.10"


@dataclass(frozen=True)
class BundledFilesystemPack:
    """A resolved, on-disk copy of the pinned Filesystem pack, ready to load
    contracts from or construct the exact upstream launch argv."""

    pack_yaml_path: Path
    contracts_path: Path
    upstream_identity: str
    pinned_version: str

    def upstream_launch_argv(self, project_dir: Path) -> tuple[str, str, str, str]:
        """The exact, pinned upstream command E22 registers and preflights
        (Task 4): `npx -y <identity>@<version> <project_dir>`."""
        return (
            "npx",
            "-y",
            f"{self.upstream_identity}@{self.pinned_version}",
            str(project_dir),
        )

    def load_contracts(self) -> ContractSet:
        return load_contract_set([str(self.contracts_path)])


def _check_pin(identity: str, version: str) -> None:
    """Raise if `identity`/`version` (read from the packaged pack.yaml) don't
    match this module's own hardcoded pin. Kept as a small, pure, directly
    testable function so a test can assert the rejection without having to
    corrupt the real packaged resource file."""
    if identity != UPSTREAM_IDENTITY or version != PINNED_VERSION:
        raise ValueError(
            "bundled filesystem pack metadata drift: pack.yaml declares "
            f"upstream.identity={identity!r} upstream.verified_version={version!r}, but "
            f"belay/bundled_packs.py is pinned to identity={UPSTREAM_IDENTITY!r} "
            f"version={PINNED_VERSION!r} -- update both together, never one alone"
        )


def _resource_path(name: str) -> Path:
    ref = importlib.resources.files("belay.packs").joinpath("filesystem", name)
    path = Path(str(ref))
    if not path.is_file():
        raise FileNotFoundError(
            f"bundled filesystem pack resource missing: belay/packs/filesystem/{name} "
            "-- check pyproject.toml packages this file with the belay wheel"
        )
    return path


def filesystem_pack() -> BundledFilesystemPack:
    """Resolve the bundled Filesystem pack through `importlib.resources`
    (works from a real installed wheel, not just a repo checkout) and
    validate its metadata still matches this module's pin."""
    pack_yaml_path = _resource_path("pack.yaml")
    contracts_path = _resource_path("contracts.yaml")

    metadata = yaml.safe_load(pack_yaml_path.read_text(encoding="utf-8"))
    upstream = metadata.get("upstream", {}) if isinstance(metadata, dict) else {}
    identity = str(upstream.get("identity"))
    version = str(upstream.get("verified_version"))
    _check_pin(identity, version)

    return BundledFilesystemPack(
        pack_yaml_path=pack_yaml_path,
        contracts_path=contracts_path,
        upstream_identity=identity,
        pinned_version=version,
    )
