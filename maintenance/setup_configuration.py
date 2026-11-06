"""Build runtime settings from validated routes and a trusted installation binding."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.secret_patterns import structure_contains_secret_like_text
from ..runtime.auxiliary import AuxiliaryRuntimeConfig
from ..runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig, configuration_value
from .backup import adjacent_backup, write_private_configuration
from .install_common import InstallError
from .shared import WORKER_OWNER, WORKER_SESSION, bind_runtime_configuration


def merge_model_fields(current: dict, changes: dict) -> dict:
    """Apply only explicitly supplied route fields, preserving all unrelated operator choices."""
    out = copy.deepcopy(current)
    for key, value in changes.items():
        if key in {"response_format", "text_format", "thinking", "headers"}:
            out[key] = copy.deepcopy(value)
            continue
        if (
            key == "consolidation"
            and isinstance(value, dict)
            and value.get("kind", "openai") != (out.get(key) or {}).get("kind", "openai")
        ):
            out[key] = copy.deepcopy(value)
            continue
        out[key] = (
            merge_model_fields(out.get(key) or {}, value) if isinstance(value, dict) and value else copy.deepcopy(value)
        )
    return out


def runtime_payload(binding, *, models=None, worker=None, host="hermes", existing=None) -> dict:
    """Reuse shared binding/serialization; existing runtime bounds and non-model fields stay unchanged."""
    value = (
        copy.deepcopy(existing)
        if existing is not None
        else dict(
            session_id=WORKER_SESSION,
            owner_id=WORKER_OWNER,
            host_adapter=host,
            auxiliary=dict(external_embedding=False, external_consolidation=False),
        )
    )
    if existing is not None:
        binding = RuntimeInstanceConfig.from_mapping(existing).binding
    if models is not None:
        value = merge_model_fields(value, models)
    if worker is not None:
        value["supervisor_enabled"] = worker == "on-demand"
    prior = RuntimeInstanceConfig.from_mapping(existing).auxiliary if existing is not None else None
    directory = (
        binding.data_directory
        if existing is None or (models is not None and (prior is None or prior.ledger_path is None))
        else None
    )
    return bind_runtime_configuration(value, binding, preserve_scopes=existing is not None, ledger_directory=directory)


def _explicit_fields(normalized: dict, supplied: dict) -> dict:
    """Keep only accepted fields present in the recipe; omission cannot reset an existing setting."""
    return {
        key: _explicit_fields(value, supplied[key])
        if isinstance(value, dict) and isinstance(supplied[key], dict)
        else value
        for key, value in normalized.items()
        if key in supplied
    }


def read_models(path: Path | None) -> dict | None:
    """Validate and normalize routes; only declared environment references may carry credentials."""
    if path is None:
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        return validate_models(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError("model configuration unavailable; use validated route definitions") from exc


def validate_models(raw: dict) -> dict:
    """Normalize one route recipe before it reaches a plan or generated configuration."""
    try:
        if set(raw) - {"auxiliary", "vector", "vector_threshold"}:
            raise ValueError
        auxiliary = AuxiliaryRuntimeConfig.from_mapping(raw["auxiliary"])
        value: dict[str, Any] = {"auxiliary": configuration_value(auxiliary)}
        if "vector" in raw:
            value["vector"] = (
                None if raw["vector"] is None else configuration_value(VectorRuntimeConfig.from_mapping(raw["vector"]))
            )
        if "vector_threshold" in raw:
            value["vector_threshold"] = None if raw["vector_threshold"] is None else float(raw["vector_threshold"])
        safe = copy.deepcopy(value)
        for name in ("embedding", "consolidation"):
            route = safe["auxiliary"].get(name)
            if route:
                # The runtime has validated this as a variable name. A generic text scanner
                # treats credential_env assignments as values, so scan the rest separately.
                route["credential_env"] = None
                endpoint = urlsplit(route.get("endpoint", ""))
                if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError
        if structure_contains_secret_like_text(safe):
            raise ValueError
        normalized = _explicit_fields(value, raw)
        if auxiliary.consolidation is not None:
            normalized["auxiliary"]["consolidation"]["kind"] = auxiliary.consolidation.kind
        # Store and ledger identity come from the binding, never from a model recipe.
        for key in ("installation_dir", "ledger_path"):
            normalized["auxiliary"].pop(key, None)
        return normalized
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError(
            "model configuration invalid; use route definitions with credential environment names"
        ) from exc


def write_configuration(path: Path, content: str | bytes, backups: list[str]) -> None:
    """Replace changed configuration with an exact rollback copy; identical files remain untouched."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if path.is_file() and path.read_bytes() == raw:
        return
    if path.is_file():
        backup = adjacent_backup(path)
        backups.append(str(backup))
    write_private_configuration(path, raw)
