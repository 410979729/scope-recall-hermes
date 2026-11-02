"""Build runtime settings from validated routes and a trusted installation binding."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.secret_patterns import structure_contains_secret_like_text
from ..runtime.auxiliary import AuxiliaryRuntimeConfig
from ..runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig
from .backup import atomic_write
from .install_common import InstallError


def runtime_payload(binding, *, models=None, worker="on-demand", host="hermes") -> dict:
    """Bind validated model routes to one store; zero budget and no routes are the default."""
    value: dict[str, Any] = dict(
        binding=dict(
            agent_id=binding.agent_id,
            installation_id=binding.installation_id,
            data_directory=str(binding.data_directory),
            scope_ids=sorted(binding.scope_ids),
            test_mode=binding.test_mode,
            installation_kind=binding.installation_kind,
        ),
        allowed_scope_ids=sorted(binding.scope_ids),
        session_id="setup-background",
        host_adapter=host,
        supervisor_enabled=worker == "on-demand",
        auxiliary=dict(
            external_embedding=False, external_consolidation=False, installation_dir=str(binding.data_directory)
        ),
    )
    if models:
        value.update(copy.deepcopy(models))
        value["auxiliary"]["installation_dir"] = str(binding.data_directory)
        value["auxiliary"]["ledger_path"] = str(binding.data_directory / "auxiliary-budget.sqlite3")
        if value.get("vector"):
            without_vector = {key: member for key, member in value.items() if key != "vector"}
            space = RuntimeInstanceConfig.from_mapping(without_vector).embedding_space_id()
            value["vector"]["storage_dir"] = str(binding.data_directory / "vectors" / space)
    RuntimeInstanceConfig.from_mapping(value)
    return value


def configuration_value(value: Any) -> Any:
    """Serialize validated definitions without copying unrecognized input keys into configuration."""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: configuration_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: configuration_value(member) for key, member in value.items()}
    if isinstance(value, (frozenset, set)):
        return [configuration_value(member) for member in sorted(value)]
    if isinstance(value, (tuple, list)):
        return [configuration_value(member) for member in value]
    if isinstance(value, (Path, Decimal)):
        return str(value)
    return value


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
        if raw.get("vector") is not None:
            value["vector"] = configuration_value(VectorRuntimeConfig.from_mapping(raw["vector"]))
        if raw.get("vector_threshold") is not None:
            value["vector_threshold"] = float(raw["vector_threshold"])
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
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError(
            "model configuration invalid; use route definitions with credential environment names"
        ) from exc


def write_configuration(path: Path, content: str, backups: list[str]) -> None:
    """Replace changed configuration with an exact rollback copy; identical files remain untouched."""
    raw = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == raw:
        return
    if path.is_file():
        backup = path.parent / ".scope-recall-backups" / uuid.uuid4().hex / path.name
        atomic_write(backup, path.read_bytes())
        backups.append(str(backup))
    atomic_write(path, raw)
