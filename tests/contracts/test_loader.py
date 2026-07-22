"""ContractSet loading, set_hash stability, and unknown-field rejection (§4.7, §14)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from belay.contracts.loader import load_contract_set
from belay.errors import BelayError

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "contracts"


def test_examples_fs_yaml_loads_successfully() -> None:
    contract_set = load_contract_set([EXAMPLES_DIR / "fs.yaml"])
    assert contract_set.resolve("fs.write_file") is not None
    assert contract_set.resolve("fs.delete_file") is not None
    assert contract_set.resolve("fs.read_file") is not None
    assert contract_set.resolve("does.not_exist") is None


def test_resolve_returns_none_for_unknown_tool() -> None:
    contract_set = load_contract_set([EXAMPLES_DIR / "fs.yaml"])
    assert contract_set.resolve("nope") is None


def test_set_hash_is_a_sha256_prefixed_string() -> None:
    contract_set = load_contract_set([EXAMPLES_DIR / "fs.yaml"])
    assert contract_set.set_hash.startswith("sha256:")
    assert len(contract_set.set_hash) == len("sha256:") + 64


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


CONTRACT_YAML = """
belay_contract: "0.1"
tool: fs.touch
reversibility: irreversible
effects:
  - type: create
    resource: fs.file
    count: "1"
"""


def test_set_hash_stable_across_differently_ordered_keys(tmp_path: Path) -> None:
    ordered = {
        "belay_contract": "0.1",
        "tool": "fs.touch",
        "reversibility": "irreversible",
        "effects": [{"type": "create", "resource": "fs.file", "count": "1"}],
    }
    reordered = {
        "effects": ordered["effects"],
        "reversibility": ordered["reversibility"],
        "tool": ordered["tool"],
        "belay_contract": ordered["belay_contract"],
    }
    p1 = _write(tmp_path, "a.json", json.dumps(ordered))
    p2 = _write(tmp_path, "b.json", json.dumps(reordered))
    assert load_contract_set([p1]).set_hash == load_contract_set([p2]).set_hash


def test_set_hash_stable_across_yaml_and_json_input(tmp_path: Path) -> None:
    as_json = {
        "belay_contract": "0.1",
        "tool": "fs.touch",
        "reversibility": "irreversible",
        "effects": [{"type": "create", "resource": "fs.file", "count": "1"}],
    }
    p_yaml = _write(tmp_path, "c.yaml", CONTRACT_YAML)
    p_json = _write(tmp_path, "c.json", json.dumps(as_json))
    assert load_contract_set([p_yaml]).set_hash == load_contract_set([p_json]).set_hash


def test_one_byte_change_anywhere_changes_the_hash(tmp_path: Path) -> None:
    original = load_contract_set([_write(tmp_path, "d.yaml", CONTRACT_YAML)]).set_hash
    mutated = CONTRACT_YAML.replace("fs.touch", "fs.toucH")
    changed = load_contract_set([_write(tmp_path, "e.yaml", mutated)]).set_hash
    assert original != changed


def test_unknown_field_in_contract_is_rejected_at_load_time(tmp_path: Path) -> None:
    bad = CONTRACT_YAML + "\nsurprise_field: true\n"
    path = _write(tmp_path, "bad.yaml", bad)
    with pytest.raises(BelayError) as exc_info:
        load_contract_set([path])
    assert exc_info.value.code == "contract_invalid"


def test_invalid_reversibility_undo_combination_rejected_at_load_time(tmp_path: Path) -> None:
    bad = """
belay_contract: "0.1"
tool: fs.bad
reversibility: irreversible
undo:
  tool: fs.something
  args: {}
effects:
  - type: create
    resource: fs.file
"""
    path = _write(tmp_path, "bad2.yaml", bad)
    with pytest.raises(BelayError) as exc_info:
        load_contract_set([path])
    assert exc_info.value.code == "contract_invalid"


def test_malformed_yaml_is_contract_invalid(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.yaml", "belay_contract: [unterminated")
    with pytest.raises(BelayError) as exc_info:
        load_contract_set([path])
    assert exc_info.value.code == "contract_invalid"


def test_empty_yaml_document_in_stream_is_skipped(tmp_path: Path) -> None:
    multi = "---\n" + CONTRACT_YAML
    path = _write(tmp_path, "with_empty.yaml", multi)
    contract_set = load_contract_set([path])
    assert contract_set.resolve("fs.touch") is not None


def test_yaml_list_of_contracts_loads_each_one(tmp_path: Path) -> None:
    import yaml

    doc_a = yaml.safe_load(CONTRACT_YAML)
    doc_b = yaml.safe_load(CONTRACT_YAML.replace("fs.touch", "fs.touch3"))
    path = _write(tmp_path, "list.yaml", yaml.safe_dump([doc_a, doc_b]))
    contract_set = load_contract_set([path])
    assert contract_set.resolve("fs.touch") is not None
    assert contract_set.resolve("fs.touch3") is not None


def test_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "notmap.yaml", "- 1\n- 2\n")
    with pytest.raises(BelayError) as exc_info:
        load_contract_set([path])
    assert exc_info.value.code == "contract_invalid"


def test_multi_document_yaml_file_loads_multiple_contracts(tmp_path: Path) -> None:
    multi = CONTRACT_YAML + "\n---\n" + CONTRACT_YAML.replace("fs.touch", "fs.touch2")
    path = _write(tmp_path, "multi.yaml", multi)
    contract_set = load_contract_set([path])
    assert contract_set.resolve("fs.touch") is not None
    assert contract_set.resolve("fs.touch2") is not None
