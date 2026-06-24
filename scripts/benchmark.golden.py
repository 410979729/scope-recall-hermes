#!/usr/bin/env python3
"""Run repository-owned golden recall benchmark cases.

The runner creates an isolated temporary Hermes home by default, copies the
current source checkout into ``plugins/scope-recall``, stores labeled fixture
memories through the public ``scope_recall_store`` tool, resolves labels to
runtime ids, then executes ``scope_recall_benchmark`` assertions.

Safety boundary: an existing ``--hermes-home`` is read-only by default.  The
fixture config is written only to the isolated benchmark home unless the caller
passes the explicit ``--overwrite-config`` maintenance flag, in which case the
original config is backed up and restored in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from plugins.memory import load_memory_provider

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "golden_recall_cases.json"
COPY_IGNORE_PATTERNS = (
    ".git",
    ".hermes",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "*.pyc",
    "build",
    "dist",
    "*.egg-info",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scope-recall golden benchmark")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="Golden cases JSON file")
    parser.add_argument(
        "--hermes-home",
        default="",
        help=(
            "Existing Hermes home to benchmark only with --overwrite-config. "
            "Without --overwrite-config it is treated as read-only metadata and an isolated temp home is used."
        ),
    )
    parser.add_argument("--keep-home", action="store_true", help="Do not delete the temporary Hermes home")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--auto-explain-on-fail", action="store_true")
    parser.add_argument(
        "--overwrite-config",
        action="store_true",
        help="DANGEROUS maintenance mode: temporarily write fixture config to --hermes-home, with automatic backup/restore.",
    )
    return parser.parse_args()


def _write_config(hermes_home: Path, config: dict[str, Any]) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _config_path(hermes_home: Path) -> Path:
    return hermes_home / "scope-recall" / "config.json"


@contextmanager
def _temporary_fixture_config(hermes_home: Path, config: dict[str, Any], *, allow_overwrite: bool) -> Iterator[dict[str, str]]:
    """Write fixture config with fail-closed handling for real homes.

    Normal benchmark homes are isolated and can receive the fixture config
    directly.  Existing homes require the explicit maintenance flag; their
    original config is restored even when provider init or benchmark assertions
    fail.  Provider discovery happens before this context is entered, so a
    missing provider cannot clobber config.
    """

    config_path = _config_path(hermes_home)
    if not allow_overwrite:
        _write_config(hermes_home, config)
        yield {}
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = config_path.with_suffix(config_path.suffix + f".golden-benchmark-backup.{uuid.uuid4().hex}")
    had_original = config_path.exists()
    if had_original:
        shutil.copy2(config_path, backup_path)
    _write_config(hermes_home, config)
    try:
        yield {"config_backup": str(backup_path), "config_restored": "false"}
    finally:
        if had_original:
            shutil.copy2(backup_path, config_path)
        else:
            config_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)


def _copy_source_plugin(source_root: Path, hermes_home: Path) -> Path:
    plugin_dir = hermes_home / "plugins" / "scope-recall"
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, plugin_dir, ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS))
    return plugin_dir


@contextmanager
def _provider_environment(hermes_home: Path) -> Iterator[None]:
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        yield
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


def _load_provider_for_home(hermes_home: Path) -> Any:
    with _provider_environment(hermes_home):
        plugin = load_memory_provider("scope-recall")
    if plugin is None:
        raise RuntimeError(f"scope-recall provider is not available in {hermes_home / 'plugins' / 'scope-recall'}")
    return plugin


def _resolve_case_labels(case: dict[str, Any], label_to_id: dict[str, str]) -> dict[str, Any]:
    resolved = dict(case)
    resolved.pop("name", None)
    expected = []
    for label in case.get("expected_labels") or []:
        if label not in label_to_id:
            raise KeyError(f"unknown expected label: {label}")
        expected.append(label_to_id[label])
    forbidden = []
    for label in case.get("forbidden_labels") or []:
        if label not in label_to_id:
            raise KeyError(f"unknown forbidden label: {label}")
        forbidden.append(label_to_id[label])
    resolved.pop("expected_labels", None)
    resolved.pop("forbidden_labels", None)
    if expected:
        resolved["expected_ids"] = expected
    if forbidden:
        resolved["forbidden_ids"] = forbidden
    return resolved


def _mark_archived(plugin: Any, memory_id: str) -> None:
    conn = plugin._require_conn()
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"stored memory not found for archive marker: {memory_id}")
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except Exception:
        metadata = {}
    metadata["lifecycle"] = "archived"
    metadata["archived_by"] = "golden-benchmark-fixture"
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


def run_golden(
    cases_path: Path,
    hermes_home: Path,
    *,
    limit: int,
    auto_explain_on_fail: bool,
    overwrite_config: bool = False,
) -> dict[str, Any]:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    fixture_config = fixture.get("config") if isinstance(fixture.get("config"), dict) else {}
    plugin = _load_provider_for_home(hermes_home)
    label_to_id: dict[str, str] = {}
    config_info: dict[str, str] = {}
    with _temporary_fixture_config(hermes_home, fixture_config, allow_overwrite=overwrite_config) as info:
        config_info = dict(info)
        try:
            with _provider_environment(hermes_home):
                plugin.initialize(
                    "session-golden-benchmark",
                    hermes_home=str(hermes_home),
                    platform="cli",
                    agent_context="primary",
                    agent_identity="yuheng",
                    agent_workspace="hermes",
                    user_id="joy",
                )
                for item in fixture.get("setup") or []:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label") or "").strip()
                    if not label:
                        raise ValueError("setup item missing label")
                    payload = {key: value for key, value in item.items() if key not in {"label", "lifecycle"}}
                    stored = json.loads(plugin.handle_tool_call("scope_recall_store", payload))
                    memory_id = str(stored.get("id") or "")
                    if not memory_id:
                        raise RuntimeError(f"failed to store golden fixture {label}: {stored}")
                    label_to_id[label] = memory_id
                    if str(item.get("lifecycle") or "").lower() == "archived":
                        _mark_archived(plugin, memory_id)
                resolved_cases = [_resolve_case_labels(case, label_to_id) for case in fixture.get("cases") or []]
                result = json.loads(
                    plugin.handle_tool_call(
                        "scope_recall_benchmark",
                        {"cases": resolved_cases, "limit": limit, "auto_explain_on_fail": auto_explain_on_fail},
                    )
                )
        finally:
            plugin.shutdown()
    result["golden_name"] = fixture.get("name", cases_path.stem)
    result["case_file"] = str(cases_path)
    result["label_to_id"] = label_to_id
    result["hermes_home"] = str(hermes_home)
    if config_info:
        config_info["config_restored"] = "true"
        result["config_safety"] = config_info
    return result


def main() -> int:
    args = parse_args()
    cases_path = Path(args.cases).expanduser().resolve()
    temp_dir = ""
    source_hermes_home = ""
    overwrite_config = bool(args.overwrite_config)
    if overwrite_config:
        if not args.hermes_home:
            raise SystemExit("--overwrite-config requires --hermes-home")
        hermes_home = Path(args.hermes_home).expanduser().resolve()
        hermes_home.mkdir(parents=True, exist_ok=True)
    else:
        if args.hermes_home:
            source_hermes_home = str(Path(args.hermes_home).expanduser().resolve())
        temp_dir = tempfile.mkdtemp(prefix="scope-recall-golden-")
        hermes_home = Path(temp_dir)
        _copy_source_plugin(ROOT, hermes_home)
    try:
        payload = run_golden(
            cases_path,
            hermes_home,
            limit=max(1, int(args.limit)),
            auto_explain_on_fail=bool(args.auto_explain_on_fail),
            overwrite_config=overwrite_config,
        )
        if source_hermes_home:
            payload["source_hermes_home"] = source_hermes_home
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("passed") else 1
    finally:
        if temp_dir and not args.keep_home:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
