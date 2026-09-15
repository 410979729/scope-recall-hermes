#!/usr/bin/env python3
"""Probe an exact Hermes source tree against Scope Recall in an isolated home."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Mapping, Sequence

try:
    from scripts.execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.execution_boundary"}:
        raise
    from execution_boundary import (  # pyright: ignore[reportMissingImports]
        validate_execution_boundary,
    )


SCHEMA_VERSION = "scope-recall.hermes-compatibility-probe.v1"
DEFAULT_OUTPUT = Path(".execution/HERMES_COMPATIBILITY_PROBE.json")
PROBE_TIMEOUT_SECONDS = 300


class HermesCompatibilityProbeError(RuntimeError):
    """Raised when the compatibility probe boundary itself is invalid."""


def _same_or_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_version(source: Path) -> str:
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        raise HermesCompatibilityProbeError("Hermes source is missing pyproject.toml")
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = payload["project"]["version"]
        if not isinstance(version, str) or not version.strip():
            raise TypeError("project.version must be a non-empty string")
        return version.strip()
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise HermesCompatibilityProbeError(
            f"Hermes source version cannot be read: {type(exc).__name__}"
        ) from exc


def _git_identity(source: Path) -> dict[str, object]:
    if not (source / ".git").exists():
        return {"commit": "unbound", "tree": "unbound", "clean": "unknown"}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    values: dict[str, bytes] = {}
    for key, args in (
        ("commit", ["rev-parse", "HEAD"]),
        ("tree", ["rev-parse", "HEAD^{tree}"]),
        ("status", ["status", "--porcelain=v1", "-z"]),
    ):
        result = subprocess.run(
            ["git", "-c", f"safe.directory={source}", "-C", str(source), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            return {"commit": "unbound", "tree": "unbound", "clean": "unknown"}
        values[key] = result.stdout
    return {
        "commit": values["commit"].decode("ascii").strip(),
        "tree": values["tree"].decode("ascii").strip(),
        "clean": not bool(values["status"]),
    }


def _is_clean_bound_git_identity(identity: Mapping[str, object]) -> bool:
    if identity.get("clean") is not True:
        return False
    for key in ("commit", "tree"):
        value = identity.get(key)
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        ):
            return False
    return True


def _run_probe_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> tuple[int, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, json.dumps(
            {"stage": "process", "classification": "unknown", "error": type(exc).__name__}
        )
    return int(result.returncode), str(result.stdout or "").strip()


_INSTALL_CODE = r"""
import json
import os
from pathlib import Path
import sys
import types

root = Path(os.environ["SCOPE_RECALL_CANDIDATE_SOURCE"]).resolve()
package = types.ModuleType("scope_recall")
package.__path__ = [str(root)]
sys.modules["scope_recall"] = package
from scope_recall import installer
home = Path(os.environ["HERMES_HOME"])
result = installer.install(home)
verified = installer.verify(home)
if result.get("ok") is not True or verified.get("ok") is not True:
    print(json.dumps({"stage": "candidate_install", "classification": "unknown"}))
    raise SystemExit(2)
print(json.dumps({"stage": "candidate_install", "classification": "passed"}))
"""


_HERMES_LOAD_CODE = r"""
import json
import os
from pathlib import Path

source = Path(os.environ["SCOPE_RECALL_HERMES_SOURCE"]).resolve()
try:
    import plugins.memory as memory_plugins
except Exception as exc:
    print(json.dumps({"stage": "hermes_import", "classification": "unknown", "error": type(exc).__name__}))
    raise SystemExit(3)
module_path = Path(memory_plugins.__file__).resolve()
try:
    module_path.relative_to(source)
except ValueError:
    print(json.dumps({"stage": "source_binding", "classification": "unknown"}))
    raise SystemExit(4)
try:
    provider = memory_plugins.load_memory_provider("scope-recall")
    if provider is None:
        print(json.dumps({"stage": "provider_load", "classification": "incompatible"}))
        raise SystemExit(5)
    available = provider.is_available()
    schemas = provider.get_tool_schemas()
    if available is not True or not isinstance(schemas, list) or not schemas:
        print(json.dumps({"stage": "provider_contract", "classification": "incompatible"}))
        raise SystemExit(6)
    names = sorted(str(item.get("name") or "") for item in schemas if isinstance(item, dict))
    required = {"scope_recall_store", "scope_recall_search", "scope_recall_context"}
    if not required.issubset(names):
        print(json.dumps({"stage": "tool_schema_contract", "classification": "incompatible"}))
        raise SystemExit(7)
    provider.shutdown()
except SystemExit:
    raise
except Exception as exc:
    print(json.dumps({"stage": "provider_contract", "classification": "incompatible", "error": type(exc).__name__}))
    raise SystemExit(8)
print(json.dumps({"stage": "complete", "classification": "compatible", "tool_count": len(names)}))
"""


def _last_json_object(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {"stage": "process", "classification": "unknown"}


def build_probe_receipt(
    *,
    candidate_source: Path,
    hermes_source: Path,
    expected_hermes_version: str,
    active_hermes_home: Path,
) -> dict[str, object]:
    candidate = candidate_source.resolve(strict=True)
    hermes = hermes_source.resolve(strict=True)
    active = active_hermes_home.resolve(strict=False)
    active_plugin = active / "plugins" / "scope-recall"
    if _same_or_within(candidate, active_plugin) or _same_or_within(
        active_plugin, candidate
    ):
        raise HermesCompatibilityProbeError(
            "ACTIVE_HERMES_SOURCE_REFUSED: candidate source overlaps active plugin"
        )
    if _same_or_within(hermes, active) or _same_or_within(active, hermes):
        raise HermesCompatibilityProbeError(
            "ACTIVE_HERMES_SOURCE_REFUSED: probe source overlaps active Hermes"
        )
    version = _source_version(hermes)
    candidate_identity = _git_identity(candidate)
    hermes_identity = _git_identity(hermes)
    started = datetime.now(timezone.utc)
    common: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "expected_hermes_version": expected_hermes_version,
        "observed_hermes_version": version,
        "candidate_source": candidate_identity,
        "hermes_source": hermes_identity,
        "support_matrix_changed": False,
        "active_instance_touched": False,
        "started_at": started.isoformat(),
    }
    if version != expected_hermes_version:
        return {
            **common,
            "result": "unknown",
            "reason": "hermes_version_mismatch",
            "stages": {"candidate_install": "not_run", "provider_load": "not_run"},
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    if not _is_clean_bound_git_identity(candidate_identity):
        return {
            **common,
            "result": "unknown",
            "reason": "candidate_source_unbound",
            "stages": {"candidate_install": "not_run", "provider_load": "not_run"},
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    if not _is_clean_bound_git_identity(hermes_identity):
        return {
            **common,
            "result": "unknown",
            "reason": "hermes_source_unbound",
            "stages": {"candidate_install": "not_run", "provider_load": "not_run"},
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    with tempfile.TemporaryDirectory(prefix="scope.recall.hermes-probe.") as temp_text:
        boundary = Path(temp_text)
        home = boundary / "hermes-home"
        targets = {
            "HOME": boundary / "user-home",
            "USERPROFILE": boundary / "user-home",
            "APPDATA": boundary / "appdata",
            "LOCALAPPDATA": boundary / "local-appdata",
            "TEMP": boundary / "temp",
            "TMP": boundary / "temp",
            "XDG_CONFIG_HOME": boundary / "xdg-config",
            "XDG_CACHE_HOME": boundary / "xdg-cache",
            "PIP_CACHE_DIR": boundary / "pip-cache",
            "HERMES_HOME": home,
            "SCOPE_RECALL_DB": boundary / "truth" / "memory.sqlite3",
            "SCOPE_RECALL_LOG_DIR": boundary / "logs",
            "SCOPE_RECALL_LEASE_DIR": boundary / "leases",
            "SCOPE_RECALL_PLUGIN_DIR": home / "plugins" / "scope-recall",
        }
        validate_execution_boundary(
            isolated_root=boundary,
            targets=targets,
            active_hermes_home=active,
        )
        for name, path in targets.items():
            if name in {"SCOPE_RECALL_DB", "SCOPE_RECALL_PLUGIN_DIR"}:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                path.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({name: str(path) for name, path in targets.items()})
        env.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "SCOPE_RECALL_CANDIDATE_SOURCE": str(candidate),
                "SCOPE_RECALL_HERMES_SOURCE": str(hermes),
            }
        )
        install_code, install_output = _run_probe_command(
            [sys.executable, "-c", _INSTALL_CODE],
            cwd=boundary,
            env=env,
        )
        install_result = _last_json_object(install_output)
        if install_code != 0:
            classification = "unknown"
            final_stage = str(install_result.get("stage") or "candidate_install")
            load_stage = "not_run"
        else:
            env["PYTHONPATH"] = str(hermes)
            load_code, load_output = _run_probe_command(
                [sys.executable, "-c", _HERMES_LOAD_CODE],
                cwd=boundary,
                env=env,
            )
            load_result = _last_json_object(load_output)
            observed_classification = str(
                load_result.get("classification") or "unknown"
            )
            if load_code == 0 and observed_classification == "compatible":
                classification = "compatible"
            elif load_code != 0 and observed_classification == "incompatible":
                classification = "incompatible"
            else:
                classification = "unknown"
            final_stage = str(load_result.get("stage") or "process")
            load_stage = observed_classification
    return {
        **common,
        "result": classification,
        "reason": final_stage,
        "stages": {
            "candidate_install": str(
                install_result.get("classification") or "unknown"
            ),
            "provider_load": load_stage,
        },
        "environment_boundary": {
            "hermes_home_kind": "isolated",
            "database_kind": "isolated",
            "active_instance_touched": False,
        },
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_ignored(root: Path, output: Path, payload: Mapping[str, object]) -> None:
    resolved = output if output.is_absolute() else root / output
    try:
        relative = resolved.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise HermesCompatibilityProbeError(
            "refusing to write compatibility receipt outside the candidate source"
        ) from exc
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative.as_posix()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    if result.returncode != 0:
        raise HermesCompatibilityProbeError(
            "refusing to write compatibility receipt to an unignored path"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--expected-hermes-version", required=True)
    parser.add_argument("--active-hermes-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.candidate_source.resolve(strict=True)
    payload = build_probe_receipt(
        candidate_source=root,
        hermes_source=args.hermes_source,
        expected_hermes_version=str(args.expected_hermes_version),
        active_hermes_home=args.active_hermes_home,
    )
    _write_ignored(root, args.output, payload)
    print(
        json.dumps(
            {
                "ok": payload["result"] == "compatible",
                "result": payload["result"],
                "observed_hermes_version": payload["observed_hermes_version"],
                "support_matrix_changed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if payload["result"] == "compatible" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HermesCompatibilityProbeError as exc:
        print(json.dumps({"ok": False, "result": "unknown", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
