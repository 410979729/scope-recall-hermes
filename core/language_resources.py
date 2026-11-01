"""Load packaged language data without depending on a source checkout."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any


def _resource_text(name: str) -> str:
    local = Path(__file__).resolve().parents[1] / "prompts" / name
    if local.is_file():
        return local.read_text(encoding="utf-8")
    return files("scope_recall").joinpath(f"prompts/{name}").read_text(encoding="utf-8")


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "pattern" in value and "flags" in value:
            return re.compile(value["pattern"], value["flags"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    return value


@lru_cache(maxsize=8)
def language_rules(name: str) -> dict:
    """Return one immutable-by-convention rule table shipped with the package."""
    if name not in {"credentials", "query_time", "aliases"}:
        raise ValueError("unknown language table")
    return _decode(json.loads(_resource_text(f"{name}.json")))


@lru_cache(maxsize=8)
def prompt_text(name: str) -> str:
    """Return an allowlisted prompt template; callers supply authorized input separately."""
    if name not in {"consolidation", "candidate", "fragment", "repair", "support", "merge", "entity"}:
        raise ValueError("unknown prompt")
    return _resource_text(f"{name}.txt").rstrip("\r\n")
