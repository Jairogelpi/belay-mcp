"""YAML/JSON -> ContractSet loading, with set_hash computation (spec §4.7).

Canonical form of a contract set is JSON; YAML is only an authoring
convenience (spec §4.1). `set_hash` is the SHA-256 of the canonical JSON of
the sorted-by-tool contract mapping, so the same content produces the same
hash regardless of source file order, key order, or YAML-vs-JSON authoring
format.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from belay.canonical import canonical_hash
from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError


def _load_documents(path: Path) -> list[dict[str, Any]]:
    """Load every contract document in `path`.

    A file may hold a single contract, a YAML/JSON list of contracts, or a
    multi-document YAML stream (`---`-separated) — one file per tool server,
    one document per tool (plan.md §2: `fs.yaml`, `crm.yaml`, `email.yaml`).
    """
    text = path.read_text(encoding="utf-8")
    try:
        raw_docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise BelayError(
            "contract_invalid", {"path": str(path), "reason": f"invalid YAML/JSON: {exc}"}
        ) from exc
    docs: list[dict[str, Any]] = []
    for raw in raw_docs:
        if raw is None:
            continue
        if isinstance(raw, list):
            docs.extend(raw)
        else:
            docs.append(raw)
    for doc in docs:
        if not isinstance(doc, dict):
            raise BelayError(
                "contract_invalid", {"path": str(path), "reason": "document is not a mapping"}
            )
    return docs


def load_contract_set(paths: Sequence[str | Path]) -> ContractSet:
    """Load one or more YAML/JSON contract documents into a `ContractSet`.

    Each path is one contract document (spec §4.1). Validates every document
    against the contract model (Appendix A, including the reversibility
    `allOf` constraints of §4.2) and rejects unknown fields (§14). Raises
    `BelayError(code="contract_invalid")` on any validation failure.
    """
    contracts: dict[str, Contract] = {}
    for raw_path in paths:
        path = Path(raw_path)
        for doc in _load_documents(path):
            try:
                contract = Contract.model_validate(doc)
            except ValidationError as exc:
                raise BelayError(
                    "contract_invalid", {"path": str(path), "reason": exc.errors()}
                ) from exc
            contracts[contract.tool] = contract

    canonical_payload = {
        tool: contracts[tool].model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in sorted(contracts)
    }
    set_hash = "sha256:" + canonical_hash(canonical_payload)
    return ContractSet(contracts=contracts, set_hash=set_hash)
