from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from belay.intent.model import IntentContract


def load_intent_contract(path: str | Path) -> IntentContract:
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: intent contract must be a YAML mapping")
    try:
        return IntentContract.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid intent contract: {exc}") from exc
