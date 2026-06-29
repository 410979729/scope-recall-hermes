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
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # package import path when installed or pytest aliases scope_recall
    from scope_recall.response_schemas import GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION
except ImportError:  # pragma: no cover - direct source checkout execution fallback
    from response_schemas import GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION

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
    try:
        from plugins.memory import load_memory_provider
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"scope-recall provider is not available in {hermes_home / 'plugins' / 'scope-recall'}; "
            "Hermes plugins.memory loader could not be imported"
        ) from exc
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
    expected_metadata: dict[str, Any] = {}
    raw_expected_metadata = case.get("expected_metadata_labels")
    if isinstance(raw_expected_metadata, dict):
        for label, expected_values in raw_expected_metadata.items():
            label = str(label)
            if label not in label_to_id:
                raise KeyError(f"unknown expected metadata label: {label}")
            if isinstance(expected_values, dict):
                expected_metadata[label_to_id[label]] = dict(expected_values)
    resolved.pop("expected_labels", None)
    resolved.pop("forbidden_labels", None)
    resolved.pop("expected_metadata_labels", None)
    if expected:
        resolved["expected_ids"] = expected
    if forbidden:
        resolved["forbidden_ids"] = forbidden
    if expected_metadata:
        resolved["expected_metadata"] = expected_metadata
    return resolved


def _mark_lifecycle(plugin: Any, memory_id: str, lifecycle: str) -> None:
    conn = plugin._require_conn()
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"stored memory not found for lifecycle marker: {memory_id}")
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except Exception:
        metadata = {}
    metadata["lifecycle"] = str(lifecycle or "").strip().lower()
    metadata[f"{metadata['lifecycle']}_by"] = "golden-benchmark-fixture"
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), memory_id))
    conn.commit()


def _mark_fact_freshness(plugin: Any, memory_id: str, item: dict[str, Any]) -> None:
    freshness = item.get("fact_freshness")
    if not isinstance(freshness, dict):
        return
    conn = plugin._require_conn()
    now = "2026-06-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT OR REPLACE INTO fact_freshness(
            id, subject_type, subject_id, fact_key, truth_type, validator_kind,
            validator_spec, ttl_days, last_checked_at, valid_until, status,
            stale_reason, superseded_by, created_at, updated_at
        ) VALUES (?, 'memory', ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"golden_fresh_{memory_id}",
            memory_id,
            str(freshness.get("fact_key") or "golden_fact"),
            str(freshness.get("truth_type") or "fixture"),
            str(freshness.get("validator_kind") or "fixture"),
            int(freshness.get("ttl_days") or 7),
            str(freshness.get("last_checked_at") or now),
            str(freshness.get("valid_until") or "2026-01-01T00:00:00+00:00"),
            str(freshness.get("status") or "unknown"),
            str(freshness.get("stale_reason") or "golden benchmark fixture"),
            str(freshness.get("superseded_by") or ""),
            now,
            now,
        ),
    )
    conn.commit()


def _playbook_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload:
        return dict(payload)
    title = str(item.get("title") or "Scope Recall：golden benchmark playbook")
    trigger = str(item.get("trigger") or title)
    task_class = str(item.get("task_class") or "golden_benchmark_task")
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": task_class,
        "title": title,
        "trigger": trigger,
        "goal": str(item.get("goal") or trigger),
        "preconditions": [{"check": "Confirm live task scope", "evidence_required": "user task and current repo state"}],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": str(item.get("step") or "Inspect current evidence before acting."),
                "evidence_required": "readback or command output",
            }
        ],
        "pitfalls": [{"signal": "stale state", "correction": "re-check live evidence"}],
        "verification": ["Evidence was read back before reuse."],
        "cleanup": ["Leave no temporary benchmark artifacts."],
        "reuse_policy": {"default_decision": "guided_reuse", "allow_direct_reuse": False},
        "status": "candidate",
        "confidence": float(item.get("confidence") or 0.88),
    }


def _setup_playbooks(plugin: Any, fixture: dict[str, Any], label_to_id: dict[str, str]) -> dict[str, str]:
    playbook_label_to_id: dict[str, str] = {}
    for item in fixture.get("playbooks") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            raise ValueError("playbook fixture missing label")
        created = json.loads(
            plugin.handle_tool_call(
                "scope_recall_playbook_create",
                {
                    "id": str(item.get("id") or "").strip(),
                    "payload": _playbook_payload(item),
                    "confidence": float(item.get("confidence") or 0.88),
                    "metadata": {"source": "golden-benchmark-fixture"},
                    "evidence_anchors": item.get("evidence_anchors") if isinstance(item.get("evidence_anchors"), list) else [],
                    "related_skills": item.get("related_skills") if isinstance(item.get("related_skills"), list) else [],
                },
            )
        )
        playbook = created.get("playbook") if isinstance(created.get("playbook"), dict) else {}
        playbook_id = str(playbook.get("id") or "")
        if not playbook_id:
            raise RuntimeError(f"failed to create playbook fixture {label}: {created}")
        desired_status = str(item.get("status") or "candidate").strip().lower()
        if desired_status and desired_status != "candidate":
            reviewed = json.loads(
                plugin.handle_tool_call(
                    "scope_recall_playbook_review",
                    {"id": playbook_id, "action": desired_status, "reason": "golden benchmark fixture"},
                )
            )
            if not reviewed.get("reviewed"):
                raise RuntimeError(f"failed to set playbook fixture status {label}: {reviewed}")
        playbook_label_to_id[label] = playbook_id
        label_to_id[f"playbook:{label}"] = playbook_id
    return playbook_label_to_id


def _resolve_experience_case_labels(case: dict[str, Any], label_to_id: dict[str, str]) -> dict[str, Any]:
    resolved = dict(case)
    expected = []
    for label in case.get("expected_playbook_labels") or []:
        if label not in label_to_id:
            raise KeyError(f"unknown expected playbook label: {label}")
        expected.append(label_to_id[label])
    forbidden = []
    for label in case.get("forbidden_playbook_labels") or []:
        if label not in label_to_id:
            raise KeyError(f"unknown forbidden playbook label: {label}")
        forbidden.append(label_to_id[label])
    resolved["expected_playbook_ids"] = expected
    resolved["forbidden_playbook_ids"] = forbidden
    return resolved


def _run_experience_cases(plugin: Any, cases: list[dict[str, Any]], playbook_label_to_id: dict[str, str], *, limit: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for case in cases:
        resolved = _resolve_experience_case_labels(case, playbook_label_to_id)
        query = str(resolved.get("query") or "")
        payload = json.loads(plugin.handle_tool_call("scope_recall_experience_preflight", {"query": query, "limit": limit}))
        playbook = payload.get("playbook") if isinstance(payload.get("playbook"), dict) else {}
        selected_id = str(playbook.get("id") or "")
        row_failures: list[str] = []
        expected_decision = str(resolved.get("expected_decision") or "")
        if expected_decision and str(payload.get("decision") or "") != expected_decision:
            row_failures.append(f"decision_mismatch:{payload.get('decision')}:expected={expected_decision}")
        for expected_id in resolved.get("expected_playbook_ids") or []:
            if selected_id != expected_id:
                row_failures.append(f"expected_playbook_not_selected:{expected_id}:selected={selected_id}")
        for forbidden_id in resolved.get("forbidden_playbook_ids") or []:
            if selected_id == forbidden_id:
                row_failures.append(f"forbidden_playbook_selected:{forbidden_id}")
        payload_text = json.dumps(payload, ensure_ascii=False).lower()
        for term in resolved.get("required_terms") or []:
            normalized_term = str(term or "").strip().lower()
            if normalized_term and normalized_term not in payload_text:
                row_failures.append(f"required_term_missing:{normalized_term}")
        if row_failures:
            failures.extend(f"{query}: {failure}" for failure in row_failures)
        rows.append({"query": query, "decision": payload.get("decision"), "selected_id": selected_id, "passed": not row_failures, "failures": row_failures})
    return {"query_count": len(cases), "passed": not failures, "failures": failures, "results": rows}


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
                    payload = {key: value for key, value in item.items() if key not in {"label", "lifecycle", "fact_freshness"}}
                    stored = json.loads(plugin.handle_tool_call("scope_recall_store", payload))
                    memory_id = str(stored.get("id") or "")
                    if not memory_id:
                        raise RuntimeError(f"failed to store golden fixture {label}: {stored}")
                    label_to_id[label] = memory_id
                    lifecycle = str(item.get("lifecycle") or "").strip().lower()
                    if lifecycle:
                        _mark_lifecycle(plugin, memory_id, lifecycle)
                    _mark_fact_freshness(plugin, memory_id, item)
                playbook_label_to_id = _setup_playbooks(plugin, fixture, label_to_id)
                recall_cases = [case for case in (fixture.get("cases") or []) if str(case.get("surface") or case.get("kind") or "recall") == "recall"]
                experience_cases = [case for case in (fixture.get("cases") or []) if str(case.get("surface") or case.get("kind") or "recall") == "experience"]
                resolved_cases = [_resolve_case_labels(case, label_to_id) for case in recall_cases]
                result = json.loads(
                    plugin.handle_tool_call(
                        "scope_recall_benchmark",
                        {"cases": resolved_cases, "limit": limit, "auto_explain_on_fail": auto_explain_on_fail},
                    )
                )
                experience_result = _run_experience_cases(plugin, experience_cases, playbook_label_to_id, limit=limit) if experience_cases else {"query_count": 0, "passed": True, "failures": [], "results": []}
                if experience_cases:
                    result["experience"] = experience_result
                    result["query_count"] = int(result.get("query_count") or 0) + int(experience_result.get("query_count") or 0)
                    result["passed"] = bool(result.get("passed")) and bool(experience_result.get("passed"))
                    result["failures"] = [*(result.get("failures") or []), *(experience_result.get("failures") or [])]
        finally:
            plugin.shutdown()
    result["schema_version"] = GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION
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
