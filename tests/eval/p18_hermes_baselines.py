"""P18 TEST-only public-history drivers for Hermes arms A, B and D.

Each arm receives the same small public synthetic fixture and writes only to its
own TEST home.  The drivers retain source text and source metadata as an
archive envelope; they do not extract claims, read gold/sealed material, or
initialize the current Scope Recall Core schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping

from p18_arm_provision import PUBLIC_FIXTURE, load_public_fixture


AUTOMATIC_MEMORY_BUDGET_TOKENS = 1200
EXPLICIT_INSPECTION_BUDGET_TOKENS = 4000
FROZEN_HERMES_COMMIT = "79445a496c86a19332ad786494b8384d2167e2d0"
BASELINE_578B = "578b955802df753f2e2208e26eab6f71971285a0"
PLAN_SUMMARY_SHA256 = "c9aacd64f56cb3b1ccb7d22e70f0baf1d2642c393baf2b7d80465f32d30b1556"
ADMITTED_PLAN_SHA256 = frozenset({PLAN_SUMMARY_SHA256, "861e05fc83594ceb1b86ca5430d66899ac9584d05499806e4fdbd4d31035f9b4"})
CORE_CONDITIONS_PER_ARM = 240
CORE_PLAN_ROOT = Path(__file__).resolve().parents[2] / ".execution" / "TEST-P18-SEALED-RUN-PLAN-v6"
FROZEN_HERMES_SOURCE = Path(r"F:\SCOPERECALL更新项目\TEST-Hermes-runtime-v1\hermes-source-79445")
LEGACY_B_ARCHIVE = (
    Path(__file__).resolve().parents[2]
    / ".execution"
    / "TEST-P18-ARMS-PREP-v1"
    / "public-arm-plan-final"
    / "hermes_a2a"
    / "arm-B"
    / "source"
    / "baseline-578b"
)


class HermesBaselineError(ValueError):
    """Malformed public input or an unsafe TEST baseline destination."""


def _safe_home(value: str | Path) -> Path:
    home = Path(value).expanduser().resolve()
    lowered = str(home).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise HermesBaselineError("formal F:\\Agents homes are forbidden")
    if home.exists() and any(home.iterdir()):
        raise HermesBaselineError("baseline home must be new and empty")
    home.mkdir(parents=True, exist_ok=True)
    return home


def _source_key(index: int) -> str:
    return f"P18-public-synthetic/{index:03d}"


def _envelope(event: Mapping[str, Any], index: int) -> dict[str, Any]:
    source_type = event.get("source_type")
    speaker_role = event.get("speaker_role")
    text = event.get("text")
    if not all(isinstance(value, str) and value.strip() for value in (source_type, speaker_role, text)):
        raise HermesBaselineError("public history event has invalid source/text fields")
    occurred_at = event.get("occurred_at")
    if occurred_at is not None and not isinstance(occurred_at, str):
        raise HermesBaselineError("public history occurred_at must be a string")
    # This is an archival envelope, not a model-extracted fact or answer.
    envelope = {
        "dataset_id": event.get("dataset_id", "P18-SYNTHETIC-PUBLIC"),
        "source_event_key": event.get("event_id", _source_key(index)),
        "source_type": source_type,
        "speaker_role": speaker_role,
        "occurred_at": occurred_at,
        "text": text,
    }
    for field in ("source_revision", "recorded_at", "time_precision"):
        if field in event:
            envelope[field] = event[field]
    return envelope


def _envelopes(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    index = 1
    for row in rows:
        history = row.get("history")
        if not isinstance(history, list):
            raise HermesBaselineError("public fixture history must be a list")
        for event in history:
            if not isinstance(event, Mapping):
                raise HermesBaselineError("public fixture event must be an object")
            result.append(_envelope(event, index))
            index += 1
    if not result:
        raise HermesBaselineError("public fixture has no history events")
    return tuple(result)


def _query_text(row: Mapping[str, Any]) -> str:
    query = row.get("query")
    if not isinstance(query, Mapping) or not isinstance(query.get("text"), str) or not query["text"].strip():
        raise HermesBaselineError("public fixture query is invalid")
    return str(query["text"])


def _first_query(rows: Iterable[Mapping[str, Any]]) -> str:
    for row in rows:
        return _query_text(row)
    raise HermesBaselineError("public fixture has no query")


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff]+", value.casefold())))


def _simple_search(entries: Iterable[str], query: str) -> list[str]:
    terms = _terms(query)
    if not terms:
        return []
    scored: list[tuple[int, int, str]] = []
    for index, entry in enumerate(entries):
        folded = entry.casefold()
        score = sum(term in folded for term in terms)
        if score:
            scored.append((-score, index, entry))
    return [entry for _score, _index, entry in sorted(scored)]


def _truncate_tokens(value: str, budget: int) -> tuple[str, int, bool]:
    matches = list(re.finditer(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\s]", value))
    if len(matches) <= budget:
        return value, len(matches), False
    return value[: matches[budget - 1].end()], budget, True


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_artifact(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise HermesBaselineError(f"{label}_path_invalid")
    target = (base / value).resolve()
    if not target.is_file() or not target.is_relative_to(base.resolve()):
        raise HermesBaselineError(f"{label}_path_invalid")
    return target


def _verify_baseline_rows(rows: list[Mapping[str, Any]], arm: str) -> None:
    if len(rows) != CORE_CONDITIONS_PER_ARM:
        raise HermesBaselineError("baseline_core_denominator_invalid")
    expected = {"arm_id", "kind", "model_input", "ordinal", "query_record", "source_records", "source_sequence", "unit_id"}
    for ordinal, row in enumerate(rows, 1):
        if set(row) != expected or row.get("arm_id") != arm or row.get("kind") != "core_condition":
            raise HermesBaselineError("baseline_core_unit_schema_invalid")
        if row.get("ordinal") != ordinal or row.get("unit_id") != f"core-{arm}-{ordinal:03d}":
            raise HermesBaselineError("baseline_core_unit_identity_invalid")
        model_input = row.get("model_input")
        query_record = row.get("query_record")
        source_records = row.get("source_records")
        if not isinstance(model_input, Mapping) or set(model_input) != {"history", "query"} or not isinstance(model_input.get("query"), Mapping):
            raise HermesBaselineError("baseline_model_input_invalid")
        if not isinstance(query_record, Mapping) or not isinstance(query_record.get("text"), str):
            raise HermesBaselineError("baseline_query_record_invalid")
        if model_input.get("query", {}).get("text") != query_record.get("text"):
            raise HermesBaselineError("baseline_query_binding_invalid")
        if not isinstance(source_records, list) or not source_records:
            raise HermesBaselineError("baseline_source_records_invalid")
        for source in source_records:
            if not isinstance(source, Mapping) or not all(
                isinstance(source.get(field), (str, int)) and source.get(field) != ""
                for field in ("event_id", "sequence", "source_type", "speaker_role", "text", "occurred_at")
            ):
                raise HermesBaselineError("baseline_source_record_invalid")
        if row.get("source_sequence") != "source_capture_then_actual_arm_extraction":
            raise HermesBaselineError("baseline_source_sequence_invalid")


def admit_baseline_core_unit(
    unit_path: str | Path,
    formal_config_path: str | Path,
    *,
    arm: str,
) -> dict[str, Any]:
    """Validate frozen P18 evidence and one A/B/D core unit before any output."""
    if arm not in {"A", "B", "D"}:
        raise HermesBaselineError("baseline_arm_invalid")
    from p18_formal_evidence import verify_formal_run_config

    config_path = Path(formal_config_path).expanduser().resolve()
    readiness = verify_formal_run_config(config_path)
    if not readiness.formal_execution_allowed:
        raise HermesBaselineError("formal_config_not_ready:" + ",".join(readiness.reasons))
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    baseline_inputs = raw.get("baseline_inputs") if isinstance(raw, Mapping) else None
    if not isinstance(baseline_inputs, Mapping) or baseline_inputs.get("arm_id") != arm:
        raise HermesBaselineError("formal_baseline_inputs_missing_or_invalid")
    unit_binding = baseline_inputs.get("unit")
    declared_unit = _relative_artifact(config_path.parent, unit_binding.get("path") if isinstance(unit_binding, Mapping) else None, "baseline_unit")
    actual_unit = Path(unit_path).expanduser().resolve()
    declared_sha = unit_binding.get("sha256") if isinstance(unit_binding, Mapping) else None
    if declared_unit != actual_unit or not isinstance(declared_sha, str) or _sha256(declared_unit) != declared_sha:
        raise HermesBaselineError("formal_baseline_unit_hash_mismatch")
    rows: list[Mapping[str, Any]] = []
    for line in declared_unit.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise HermesBaselineError("baseline_unit_row_invalid")
        rows.append(value)
    _verify_baseline_rows(rows, arm)
    if unit_binding.get("record_count") != CORE_CONDITIONS_PER_ARM:
        raise HermesBaselineError("formal_baseline_unit_denominator_invalid")
    plan_binding = baseline_inputs.get("plan")
    plan = _relative_artifact(config_path.parent, plan_binding.get("path") if isinstance(plan_binding, Mapping) else None, "baseline_plan")
    if not isinstance(plan_binding, Mapping) or plan_binding.get("sha256") != _sha256(plan):
        raise HermesBaselineError("formal_baseline_plan_hash_mismatch")
    if _sha256(plan) not in ADMITTED_PLAN_SHA256:
        raise HermesBaselineError("baseline_plan_not_frozen")
    plan_raw = json.loads(plan.read_text(encoding="utf-8"))
    if not isinstance(plan_raw, Mapping) or plan_raw.get("status") != "READY_FOR_FORMAL_FREEZE" or plan_raw.get("freeze_ready") is not True:
        raise HermesBaselineError("baseline_plan_not_ready")
    if baseline_inputs.get("dataset_id") != plan_raw.get("dataset_id") or baseline_inputs.get("raw_sha256") != plan_raw.get("raw_sha256"):
        raise HermesBaselineError("baseline_dataset_binding_mismatch")
    return {
        "readiness": dict(readiness.details),
        "config_sha256": _sha256(config_path),
        "unit_path": str(declared_unit),
        "unit_sha256": declared_sha,
        "unit_count": len(rows),
        "plan_path": str(plan),
        "plan_sha256": _sha256(plan),
        "dataset_id": baseline_inputs.get("dataset_id"),
        "raw_sha256": baseline_inputs.get("raw_sha256"),
        "rows": rows,
    }


def _storage_files(home: Path) -> dict[str, Any]:
    files = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file())
    return {
        "files": files,
        "db_like_files": [item for item in files if item.lower().endswith((".db", ".sqlite", ".sqlite3"))],
        "scope_recall_like_files": [item for item in files if "scope_recall" in item.lower() or "lancedb" in item.lower()],
    }


def _base_receipt(arm: str, home: Path, fixture_sha256: str) -> dict[str, Any]:
    return {
        "schema": "scope-recall.p18.hermes-baseline-run.v2",
        "arm": arm,
        "status": "FAILED",
        "fixture_sha256": fixture_sha256,
        "frozen_hermes_commit": FROZEN_HERMES_COMMIT,
        "hermes_home": str(home),
        "automatic_memory_budget_tokens": AUTOMATIC_MEMORY_BUDGET_TOKENS,
        "explicit_inspection_budget_tokens": EXPLICIT_INSPECTION_BUDGET_TOKENS,
        "network_calls": 0,
        "model_calls": 0,
        "gateway_started": False,
        "vault_read": False,
    }


def run_native_memory(home: str | Path, *, fixture_path: str | Path = PUBLIC_FIXTURE) -> dict[str, Any]:
    """Load/query the frozen Hermes native MemoryStore through public methods."""
    rows, fixture_sha256 = load_public_fixture(fixture_path)
    envelopes = _envelopes(rows)
    root = _safe_home(home)
    os.environ["HERMES_HOME"] = str(root)
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    from tools.memory_tool_store import MemoryStore

    store = MemoryStore(memory_char_limit=4000, user_char_limit=1375)
    store.load_from_disk()
    writes = [store.add("memory", json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in envelopes]
    fresh = MemoryStore(memory_char_limit=4000, user_char_limit=1375)
    fresh.load_from_disk()
    # The frozen native store has no query/search API. Its public read contract
    # is the load-time system-prompt snapshot; arm A must not borrow D's
    # lexical-search implementation.
    native_context = fresh.format_for_system_prompt("memory") or ""
    context_entries = [entry for entry in fresh.memory_entries if entry in native_context]
    receipt = _base_receipt("A", root, fixture_sha256)
    receipt.update(
        {
            "status": "PASS" if len(fresh.memory_entries) == len(envelopes) else "FAILED",
            "backend": "Hermes built-in MemoryStore",
            "load_api": "MemoryStore.load_from_disk + MemoryStore.add",
            "query_driver": "MemoryStore.format_for_system_prompt(memory)",
            "records_seen": len(envelopes),
            "write_successes": sum(bool(item.get("success")) for item in writes),
            "reloaded_entries": len(fresh.memory_entries),
            "recall_count": len(context_entries),
            "matched_source_keys": [json.loads(item)["source_event_key"] for item in context_entries],
            "native_context_sha256": hashlib.sha256(native_context.encode("utf-8")).hexdigest(),
            "native_context": native_context,
            "native_context_chars": len(native_context),
            "storage": _storage_files(root),
            "module_path": str(__import__("tools.memory_tool_store").memory_tool_store.__file__),
            "source_text_preserved": all(json.loads(item)["text"] in item for item in fresh.memory_entries),
        }
    )
    return receipt


_LEGACY_PROVIDER_SCRIPT = r'''
import importlib, importlib.util, json, os, pathlib, socket, sys
archive = pathlib.Path(sys.argv[1])
hermes = pathlib.Path(sys.argv[2])
payload = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
os.environ["HERMES_HOME"] = payload["home"]
sys.path.insert(0, str(hermes))
spec = importlib.util.spec_from_file_location("scope_recall", archive / "__init__.py", submodule_search_locations=[str(archive)])
module = importlib.util.module_from_spec(spec)
sys.modules["scope_recall"] = module
assert spec.loader is not None
spec.loader.exec_module(module)
provider_module = importlib.import_module("scope_recall.provider")
def blocked(*args, **kwargs):
    raise RuntimeError("network transport disabled for TEST baseline")
socket.create_connection = blocked
provider = provider_module.ScopeRecallMemoryProvider()
stored = []
writes = []
try:
    provider.initialize(payload["session_id"], hermes_home=payload["home"], platform="cli", agent_context="primary", agent_identity="P18-B-public-history", agent_workspace="P18-B-TEST", user_id="P18-public-test-user")
    print(json.dumps({"initialized_status": provider.runtime_status_view()}, ensure_ascii=False, default=str), file=sys.stderr)
    try:
        connection = provider.query_connection()
        print(json.dumps({"database_list": [tuple(row) for row in connection.execute("PRAGMA database_list").fetchall()], "journal_mode": tuple(connection.execute("PRAGMA journal_mode").fetchone() or ())}, default=str), file=sys.stderr)
    except Exception as diagnostic_error:
        print(json.dumps({"connection_diagnostic": type(diagnostic_error).__name__ + ": " + str(diagnostic_error)}, default=str), file=sys.stderr)
    for item in payload["envelopes"]:
        text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        writes.append(text)
        stored.append(provider.store_now(content=text, source=item["source_type"], target="project", session_id=payload["session_id"], metadata={"dataset_id": item["dataset_id"], "source_event_key": item["source_event_key"], "speaker_role": item["speaker_role"], "occurred_at": item["occurred_at"]}))
    flushed = provider.flush(timeout=5.0)
    recalled = provider.prefetch(payload["query"], session_id="P18-B-new-session")
    print(json.dumps({"module": str(provider_module.__file__), "stored": stored, "writes": writes, "flushed": bool(flushed), "recalled": recalled}, ensure_ascii=False))
finally:
    provider.shutdown(timeout=5.0)
'''


def run_exact_578b_provider(
    home: str | Path,
    *,
    fixture_path: str | Path = PUBLIC_FIXTURE,
    baseline_ref: str = BASELINE_578B,
) -> dict[str, Any]:
    """Load/query the archived 578b provider through its public provider API."""
    rows, fixture_sha256 = load_public_fixture(fixture_path)
    envelopes = _envelopes(rows)
    root = _safe_home(home)
    if baseline_ref != BASELINE_578B:
        raise HermesBaselineError("unsupported_legacy_baseline_ref")
    receipt = _base_receipt("B", root, fixture_sha256)
    if not LEGACY_B_ARCHIVE.is_dir() or not FROZEN_HERMES_SOURCE.is_dir():
        receipt.update({"status": "UNSUPPORTED", "reason": "exact_578b_source_or_hermes_source_missing"})
        return receipt
    session_id = "P18-B-public-history-session"
    payload_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            payload_file = Path(handle.name)
            json.dump({"home": str(root), "session_id": session_id, "query": _first_query(rows), "envelopes": envelopes}, handle, ensure_ascii=False)
        child_temp = root / "child-temp"
        child_temp.mkdir(parents=True, exist_ok=True)
        # Python/SQLite on Windows requires a writable TEMP/TMP directory for
        # the archived provider's WAL/FTS work.  Keep the child hermetic while
        # supplying TEST-owned temp roots; an env containing PATH alone makes
        # the public provider fail later at its first INSERT with the generic
        # ``unable to open database file`` error.  Force pipe text to UTF-8 as
        # the archived provider may otherwise inherit a Windows legacy code
        # page while this controller runs in UTF-8 mode.
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "TEMP": str(child_temp),
            "TMP": str(child_temp),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-c", _LEGACY_PROVIDER_SCRIPT, str(LEGACY_B_ARCHIVE), str(FROZEN_HERMES_SOURCE), str(payload_file)],
                cwd=str(root),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            receipt.update({"status": "UNSUPPORTED", "baseline_ref": baseline_ref, "reason": "legacy_provider_child_unavailable", "detail": type(exc).__name__})
            return receipt
    finally:
        if payload_file is not None:
            payload_file.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = result.stderr[-5000:]
        receipt.update({"status": "UNSUPPORTED", "baseline_ref": baseline_ref, "reason": "legacy_provider_child_failed", "detail": detail})
        return receipt
    try:
        child = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        receipt.update({"status": "UNSUPPORTED", "baseline_ref": baseline_ref, "reason": "legacy_provider_child_output_invalid"})
        return receipt
    writes = child.get("writes", [])
    stored = child.get("stored", [])
    recalled = child.get("recalled")
    stored_successes = sum(
        bool(item[1]) if isinstance(item, list) and len(item) > 1 else False
        for item in stored
    )
    receipt.update(
        {
            "status": "PASS" if child.get("flushed") and stored_successes == len(envelopes) and recalled else "FAILED",
            "baseline_ref": baseline_ref,
            "provider_api": "isolated archived child: initialize + store_now + flush + prefetch + shutdown",
            "provider_module": child.get("module"),
            "network_guard": "socket.create_connection blocked; child env contains only TEST PATH/TEMP/TMP plus UTF-8 controls",
            "child_temp": str(child_temp),
            "records_seen": len(envelopes),
            "stored_successes": stored_successes,
            "stored_results": stored,
            "flush": bool(child.get("flushed")),
            "new_session_query": _first_query(rows),
            "recall_count": 1 if recalled else 0,
            "recall_text_sha256": hashlib.sha256(str(recalled).encode()).hexdigest() if recalled else None,
            "recall_text": recalled,
            "storage": _storage_files(root),
            "source_text_preserved_in_write_payload": all(item["text"] in payload for item, payload in zip(envelopes, writes)),
            "shutdown": "PASS",
        }
    )
    return receipt


def run_archive_search(home: str | Path, *, fixture_path: str | Path = PUBLIC_FIXTURE) -> dict[str, Any]:
    """Write a raw public archive and perform deterministic literal search."""
    rows, fixture_sha256 = load_public_fixture(fixture_path)
    envelopes = _envelopes(rows)
    root = _safe_home(home)
    archive = root / "raw-history.jsonl"
    archive.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in envelopes), encoding="utf-8", newline="\n")
    query = _first_query(rows)
    entries = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    matches = [item for item in entries if all(term in item["text"].casefold() for term in _terms(query))]
    remaining = AUTOMATIC_MEMORY_BUDGET_TOKENS
    matched_records: list[dict[str, Any]] = []
    for item in matches:
        body, token_count, truncated = _truncate_tokens(item["text"], remaining)
        if not body:
            break
        matched_records.append({"source_event_key": item["source_event_key"], "source_ref": item["source_event_key"], "body": body, "token_count": token_count, "truncated": truncated})
        remaining -= token_count
        if remaining <= 0:
            break
    receipt = _base_receipt("D", root, fixture_sha256)
    receipt.update(
        {
            "status": "PASS" if len(entries) == len(envelopes) and matches else "FAILED",
            "backend": "raw JSONL archive + deterministic literal search",
            "records_seen": len(entries),
            "new_session_query": query,
            "recall_count": len(matched_records),
            "matched_source_keys": [item["source_event_key"] for item in matched_records],
            "matched_records": matched_records,
            "recall_budget_tokens": AUTOMATIC_MEMORY_BUDGET_TOKENS,
            "recall_tokens_used": sum(item["token_count"] for item in matched_records),
            "archive": str(archive),
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "storage": _storage_files(root),
            "core_schema_created": False,
            "claims_created": False,
            "source_text_preserved": all(item["text"] for item in entries),
        }
    )
    return receipt


def _unit_fixture(row: Mapping[str, Any], dataset_id: str = "P18-SYNTHETIC-PUBLIC") -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    for source in row["source_records"]:
        history.append(
            {
                "source_type": source["source_type"],
                "speaker_role": source["speaker_role"],
                "text": source["text"],
                "occurred_at": source["occurred_at"],
                "event_id": source["event_id"],
                "dataset_id": dataset_id,
                **{field: source[field] for field in ("source_revision", "recorded_at", "time_precision") if field in source},
            }
        )
    return {"history": history, "query": {"text": row["query_record"]["text"]}}


def run_core_baseline(
    arm: str,
    unit_path: str | Path,
    formal_config_path: str | Path,
    output_root: str | Path,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Admit and optionally execute the 240 A/B/D Core conditions.

    Each condition receives a new home and a raw source-envelope artifact.  The
    default is preflight only; actual execution is gated by the same formal
    evidence config used by P18 and never creates claims or reads gold.
    """
    admission = admit_baseline_core_unit(unit_path, formal_config_path, arm=arm)
    if not execute:
        return {
            "schema": "scope-recall.p18.hermes-baseline-core.v1",
            "status": "ADMITTED_PREFLIGHT",
            "arm": arm,
            "unit_count": admission["unit_count"],
            "unit_sha256": admission["unit_sha256"],
            "plan_sha256": admission["plan_sha256"],
            "formal_config_sha256": admission["config_sha256"],
            "network_calls": 0,
            "model_calls": 0,
            "claims_created": False,
            "gold_read": False,
        }
    root = _safe_home(output_root)
    results: list[dict[str, Any]] = []
    for row in admission["rows"]:
        ordinal = int(row["ordinal"])
        unit_root = root / f"unit-{ordinal:03d}"
        unit_root.mkdir(parents=True, exist_ok=False)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as handle:
            fixture_path = Path(handle.name)
            handle.write(json.dumps(_unit_fixture(row, admission["dataset_id"]), ensure_ascii=False, sort_keys=True) + "\n")
        try:
            if arm == "A":
                result = run_native_memory(unit_root, fixture_path=fixture_path)
            elif arm == "B":
                result = run_exact_578b_provider(unit_root, fixture_path=fixture_path)
            else:
                result = run_archive_search(unit_root, fixture_path=fixture_path)
        finally:
            fixture_path.unlink(missing_ok=True)
        source_envelope = unit_root / "source-envelope.json"
        source_envelope.write_text(
            json.dumps({"unit_id": row["unit_id"], "source_records": row["source_records"]}, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result.update({"unit_id": row["unit_id"], "ordinal": ordinal, "source_envelope_sha256": _sha256(source_envelope)})
        (unit_root / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append(result)
    statuses: dict[str, int] = {}
    for result in results:
        status = str(result.get("status", "FAILED"))
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "schema": "scope-recall.p18.hermes-baseline-core.v1",
        "status": "PASS" if statuses == {"PASS": CORE_CONDITIONS_PER_ARM} else "PARTIAL",
        "arm": arm,
        "unit_count": len(results),
        "unit_sha256": admission["unit_sha256"],
        "plan_sha256": admission["plan_sha256"],
        "formal_config_sha256": admission["config_sha256"],
        "output_root": str(root),
        "unit_status_counts": statuses,
        "network_calls": 0,
        "model_calls": 0,
        "claims_created": False,
        "gold_read": False,
        "raw_source_envelopes": True,
    }


def run_arm(arm: str, home: str | Path, *, fixture_path: str | Path = PUBLIC_FIXTURE) -> dict[str, Any]:
    if arm == "A":
        return run_native_memory(home, fixture_path=fixture_path)
    if arm == "B":
        return run_exact_578b_provider(home, fixture_path=fixture_path)
    if arm == "D":
        return run_archive_search(home, fixture_path=fixture_path)
    raise HermesBaselineError("only arms A, B and D are supported by this module")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("A", "B", "D"), required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--fixture", type=Path, default=PUBLIC_FIXTURE)
    parser.add_argument("--core-unit", type=Path)
    parser.add_argument("--formal-config", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.core_unit is not None:
        if args.formal_config is None or args.output_root is None:
            parser.error("--core-unit requires --formal-config and --output-root")
        result = run_core_baseline(args.arm, args.core_unit, args.formal_config, args.output_root, execute=args.execute)
    else:
        if args.home is None:
            parser.error("--home is required for a single-arm public fixture run")
        result = run_arm(args.arm, args.home, fixture_path=args.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTOMATIC_MEMORY_BUDGET_TOKENS",
    "BASELINE_578B",
    "EXPLICIT_INSPECTION_BUDGET_TOKENS",
    "FROZEN_HERMES_COMMIT",
    "HermesBaselineError",
    "admit_baseline_core_unit",
    "run_archive_search",
    "run_arm",
    "run_core_baseline",
    "run_exact_578b_provider",
    "run_native_memory",
]
