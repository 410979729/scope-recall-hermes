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


def _omit_fields(schema, fields):
    for field in fields:
        schema["properties"].pop(field, None)
    schema["required"] = [field for field in schema["required"] if field not in fields]


def model_consolidation_schema(*, candidate_kind: str | None = None) -> dict:
    """Expose model-owned fields; the stored contract retains code-owned metadata."""
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts/consolidation_result.schema.json").read_text(encoding="utf-8")
    )
    _omit_fields(schema, ("protocol_version", "source_refs"))
    for field in ("claim_proposals", "resume_proposals", "reference_proposals"):
        schema["required"].remove(field)
        schema["properties"][field]["default"] = []
    resume = schema["properties"]["resume_proposals"]["items"]
    _omit_fields(resume, ("evidence_refs", "source_watermark", "episode_ref"))
    span = schema["$defs"]["evidence_span"]
    for field in ("start", "end"):
        span["properties"].pop(field)
    span.pop("dependentRequired", None)
    if candidate_kind is not None:
        _omit_fields(schema, ("resume_proposals", "reference_proposals"))
        claims = schema["properties"]["claim_proposals"]
        claims["maxItems"] = 1
        claim = claims["items"]
        _omit_fields(claim, ("kind", "subject", "predicate"))
        # Specialize the existing kind constraints, retaining every other rule.
        claim["allOf"] = [
            clause.get("then" if kind == candidate_kind else "else", {}) if kind is not None else clause
            for clause in claim.get("allOf", ())
            for kind in [clause.get("if", {}).get("properties", {}).get("kind", {}).get("const")]
        ]
    return schema
