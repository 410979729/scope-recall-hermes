#!/usr/bin/env python3
"""Prepare and run the bounded P12 Codex automatic-recall validation.

The prepare path is deliberately side-effect limited to a new TEST-P12-AUTO-v5
directory.  The run path temporarily points the existing v3 hook config at
that fresh database, submits two ephemeral public TEST turns, and observes
the automatic Stop/SessionStart worker through read-only SQLite polling.

This controller never calls the worker entry point, never drains or embeds
manually, never writes the shared budget ledger, and never changes hook files.
It records only bounded probe metadata, hook context packets, hashes, IDs, and
the public probe's sanitized final-answer metadata.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scope_recall.adapters.codex.config import load_codex_config
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.runtime.instance import RuntimeInstanceConfig


SCHEMA_VERSION = "p12-codex-auto-driver.v1"
SOURCE_COMMIT = "fc8d4810b5c7d8ba0c9f8314fac47cc067cf1aa0"
EXPECTED_WHEEL_SHA256 = "50f99390033d89fe2eb8b566a213b05d59a0b579bc38c0fe63355bc87e09d56b"

V3_ROOT = Path(r"F:\SCOPERECALL更新项目\TEST-P12-instance-v3")
V3_CONFIG = V3_ROOT / "codex-installation.json"
V3_RUNTIME = V3_ROOT / "data" / "runtime-config.json"
AUTO_ROOT = Path(r"F:\SCOPERECALL更新项目\TEST-P12-AUTO-v5")
AUTO_DATA = AUTO_ROOT / "data"
AUTO_RUNTIME = AUTO_DATA / "runtime-config.json"
AUTO_RECEIPT = AUTO_ROOT / "prepare-receipt.json"
LEDGER = Path(
    r"F:\SCOPERECALL更新项目\worktrees\scope-recall-v1.1\.execution\TEST-MODEL-BUDGET-V1\call-budget.sqlite3"
)
CANDIDATE_RECEIPT_DEFAULT = ROOT / ".execution" / "TEST-CANDIDATE-fc8d481" / "build-receipt.json"
PUBLIC_PROBE = ROOT / ".execution" / "TEST-CURSOR-APP-PROTOCOL-v1" / "appserver_probe.py"
CODEX_EXE = Path(r"C:\Users\w4109\AppData\Local\OpenAI\Codex\bin\8e5b6932251c2c1c\codex.exe")
TEST_CWD = Path(r"C:\Users\w4109\Documents\Codex\2026-09-06\test-scope-recall-p12-v2")

AUTO_AGENT_ID = "TEST-P12-AUTO-v5"
PRIMARY_MODEL = "gpt-5.6-luna"
PRIMARY_EFFORT = "low"
EMBEDDING_MODEL = "gemini-embedding-2"
CONSOLIDATION_MODEL = "deepseek-v4-flash"
EMBEDDING_KEY_ENV = "SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"
CONSOLIDATION_KEY_ENV = "SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY"
ALLOCATION_BATCH = "P12_AUTO_V4"
TOTAL_CALL_CAP = 176
BATCH_CALL_CAP = 24
MAX_WAIT_SECONDS = 120.0

SOURCE_TEXT = (
    "请记住这条公开 TEST 事实：TEST_SCOPE_RECALL 测试灯塔的颜色是青绿色，"
    "代号是 TEST-P12-AUTO-v5。请只回复已记录，不要改变事实。"
)
QUERY_TEXT = (
    "请回答 TEST_SCOPE_RECALL 测试灯塔是什么颜色？请只依据你收到的 TEST 记忆，"
    "只输出颜色名称，不加标点、解释或其他文字。"
)
SOURCE_ANSWER = "青绿色"
WAKEUP_TEXT = "这是一个与事实无关的 synthetic TEST 唤醒提示。请回复 TEST wakeup。"
G2_DRIVER = ROOT / ".execution" / "TEST-CURSOR-APP-PROTOCOL-v1" / "run_g2_echo_fix.py"
P11_STARTER = ROOT / "probes" / "hermes" / "p11_start_a2a_test.py"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def write_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise RuntimeError(f"output_exists:{path.name}")
    path.write_bytes(json_bytes(payload))


def write_text_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def assert_test_path(path: Path, *, name: str) -> None:
    resolved = path.resolve()
    normalized = os.path.normcase(str(resolved)).replace("\\", "/")
    if not resolved.is_absolute() or normalized.startswith("f:/agents"):
        raise RuntimeError(f"unsafe_{name}")
    if "test" not in normalized:
        raise RuntimeError(f"non_test_{name}")


def validate_candidate(receipt_path: Path) -> dict[str, Any]:
    if not receipt_path.is_file():
        raise RuntimeError("candidate_receipt_missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise RuntimeError("candidate_receipt_invalid")
    if receipt.get("source_commit") != SOURCE_COMMIT:
        raise RuntimeError("candidate_commit_mismatch")
    if receipt.get("wheel_sha256") != EXPECTED_WHEEL_SHA256:
        raise RuntimeError("candidate_wheel_mismatch")
    if receipt.get("mismatches") not in ([], None):
        raise RuntimeError("candidate_receipt_mismatches")
    spec = importlib.util.spec_from_file_location("p12_verified_g2_driver", G2_DRIVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate_verifier_import_failed")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    actual = verifier.verify_candidate(receipt_path)
    if not isinstance(actual, dict) or actual.get("mismatches"):
        raise RuntimeError("candidate_installed_files_mismatch")
    return {
        "receiptSha256": sha256_file(receipt_path),
        "sourceCommit": receipt["source_commit"],
        "wheelSha256": receipt["wheel_sha256"],
        "wheel": receipt.get("wheel"),
        "packageFilesDeclared": len(receipt["package_files"]),
        "packageFilesChecked": actual.get("package_files_checked"),
        "installedPython": actual.get("python"),
        "installedModuleRoot": actual.get("module_root"),
        "installedFilesMatch": actual.get("module_file_matches_expected") and not actual.get("mismatches"),
    }


def load_g2_driver() -> Any:
    spec = importlib.util.spec_from_file_location("p12_g2_budget_driver", G2_DRIVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("budget_driver_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BATCH = ALLOCATION_BATCH
    module.MAX_ATTEMPTS = 3
    return module


def load_p11_key_loader() -> Any:
    spec = importlib.util.spec_from_file_location("p12_p11_key_loader", P11_STARTER)
    if spec is None or spec.loader is None:
        raise RuntimeError("consolidation_key_loader_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_test_credentials() -> Any:
    import contextlib

    @contextlib.contextmanager
    def manager():
        g2 = load_g2_driver()
        p11 = load_p11_key_loader()
        embedding_key = g2.credential_for_owned_child()
        consolidation_key = p11._load_authorized_test_key()
        previous = {
            EMBEDDING_KEY_ENV: os.environ.get(EMBEDDING_KEY_ENV),
            CONSOLIDATION_KEY_ENV: os.environ.get(CONSOLIDATION_KEY_ENV),
        }
        os.environ[EMBEDDING_KEY_ENV] = embedding_key
        os.environ[CONSOLIDATION_KEY_ENV] = consolidation_key
        try:
            yield
        finally:
            embedding_key = ""
            consolidation_key = ""
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    return manager()


def operation_ids() -> dict[str, str]:
    """Stable, predeclared IDs for this one fresh fixture target."""
    nonce = sha256_text(f"{V3_CONFIG.resolve()}|{AUTO_ROOT.resolve()}|{SOURCE_COMMIT}")[:16]
    return {
        "source": f"{ALLOCATION_BATCH}-source-{nonce}",
        "wakeup": f"{ALLOCATION_BATCH}-wakeup-{nonce}",
        "query": f"{ALLOCATION_BATCH}-query-{nonce}",
    }


def build_auto_installation_payload(original: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(original))
    audience = {
        "owner_private": f"audience:owner_private:{AUTO_AGENT_ID}",
        "project": f"audience:project:{AUTO_AGENT_ID}",
        "shared": f"audience:shared:{AUTO_AGENT_ID}",
    }
    payload["agent_id"] = AUTO_AGENT_ID
    payload["data_directory"] = str(AUTO_DATA)
    payload["scope_ids"] = sorted(audience.values())
    payload["audience_scopes"] = audience
    project_roots = payload.get("project_roots")
    if not isinstance(project_roots, dict) or len(project_roots) != 1:
        raise RuntimeError("v3_project_roots_unexpected")
    payload["project_roots"] = {next(iter(project_roots)): audience["project"]}
    payload["test_mode"] = True
    return payload


def build_runtime_payload(binding: Mapping[str, Any]) -> dict[str, Any]:
    scope_ids = sorted(str(item) for item in binding["scope_ids"])
    budget = {
        "batch": ALLOCATION_BATCH,
        "cap_micro_usd": 20_000_000,
        "total_input_cap": 64_000_000,
        "total_output_cap": 8_000_000,
        "total_call_cap": TOTAL_CALL_CAP,
        "batch_call_cap": BATCH_CALL_CAP,
        "max_request_bytes": 786_432,
        "default_reserve_input": 32_768,
        "default_reserve_output": 4_096,
        "model_reserve_output": {
            EMBEDDING_MODEL: 4_096,
            CONSOLIDATION_MODEL: 131_072,
        },
        "pricing": {
            EMBEDDING_MODEL: {
                "input_usd_per_million": "0.20",
                "output_usd_per_million": "0",
            },
            CONSOLIDATION_MODEL: {
                "input_usd_per_million": "0.14",
                "output_usd_per_million": "0.28",
            },
        },
        "approved_models": [EMBEDDING_MODEL, CONSOLIDATION_MODEL],
    }
    return {
        "binding": {
            "agent_id": binding["agent_id"],
            "installation_id": binding["installation_id"],
            "data_directory": str(AUTO_DATA),
            "scope_ids": scope_ids,
            "test_mode": True,
        },
        "session_id": "TEST-P12-AUTO-v5-bootstrap",
        "allowed_scope_ids": scope_ids,
        "actor_origin": "human_direct",
        "owner_id": AUTO_AGENT_ID,
        "request_seconds": 45.0,
        "drain_seconds": 45.0,
        "auto_recall_seconds": 5.0,  # contract validation maximum (instance.py:147-149)
        "hook_processing_seconds": 6.0,
        "max_items": 8,
        "lease_seconds": 60.0,
        "auxiliary": {
            "external_embedding": True,
            "external_consolidation": True,
            "installation_dir": str(AUTO_DATA),
            "ledger_path": str(LEDGER),
            "budget": budget,
            "embedding": {"credential_env": EMBEDDING_KEY_ENV},
            "consolidation": {
                "model": CONSOLIDATION_MODEL,
                "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
                "credential_env": CONSOLIDATION_KEY_ENV,
                "headers": {"x-opencode-session": "scope-recall-test-p12-auto-v7"},
                "output_limit_field": "max_completion_tokens",
                "max_output_tokens": 4096,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "stream": False,
                "n": 1,
            },
            "consolidation_reserve_input": 32_768,
        },
        "vector": {
            "backend": "lancedb",
            "storage_dir": str(AUTO_DATA / "vectors" / "93ba90c7d52b3574462d6751e2e077a411f1095727862abb4cc80d3780d2e30c"),
            "table_name": "TEST_P12_AUTO_V4_GEM2",
            "dimensions": 3072,
            "metric": "cosine",
            "test_injection_override": False,
        },
        "vector_threshold": 0.653189984350642,
    }


def build_prepare_receipt(*, candidate: Mapping[str, Any], original_sha: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "status": "PREPARED_ONLY",
        "sourceCommit": SOURCE_COMMIT,
        "candidate": dict(candidate),
        "paths": {
            "existingV3Config": str(V3_CONFIG),
            "existingV3Runtime": str(V3_RUNTIME),
            "newDataDirectory": str(AUTO_DATA),
            "newRuntimeConfig": str(AUTO_RUNTIME),
            "sharedLedger": str(LEDGER),
            "publicProbe": str(PUBLIC_PROBE),
            "codexExecutable": str(CODEX_EXE),
            "testCwd": str(TEST_CWD),
        },
        "originalV3ConfigSha256": original_sha,
        "binding": {
            "installationId": binding["installation_id"],
            "agentId": binding["agent_id"],
            "scopeIds": sorted(binding["scope_ids"]),
        },
        "runtime": {
            "externalEmbedding": True,
            "externalConsolidation": True,
            "requestSeconds": 45.0,
            "drainSeconds": 45.0,
            "autoRecallSeconds": 4.0,
            "hookProcessingSeconds": 5.0,
            "leaseSeconds": 60.0,
            "budgetBatch": ALLOCATION_BATCH,
            "totalCallCap": TOTAL_CALL_CAP,
            "batchCallCap": BATCH_CALL_CAP,
            "primaryModel": PRIMARY_MODEL,
            "consolidationModel": CONSOLIDATION_MODEL,
            "embeddingCredentialEnv": EMBEDDING_KEY_ENV,
            "consolidationCredentialEnv": CONSOLIDATION_KEY_ENV,
        },
        "prompts": {
            "sourceSha256": sha256_text(SOURCE_TEXT),
            "sourceLength": len(SOURCE_TEXT),
            "querySha256": sha256_text(QUERY_TEXT),
            "queryLength": len(QUERY_TEXT),
            "queryContainsSourceAnswer": SOURCE_ANSWER in QUERY_TEXT,
        },
        "operationIds": operation_ids(),
        "constraints": {
            "primaryTurnsMaximum": 3,
            "primaryTurnsPlanned": 2,
            "auxiliaryMaximum": 24,
            "manualDrain": False,
            "manualEmbedding": False,
            "manualWorkerEntry": False,
            "sharedLedgerMutated": False,
            "hooksChanged": False,
            "sourceDbWritesByController": False,
        },
    }


def prepare(candidate_path: Path) -> dict[str, Any]:
    for path, name in ((V3_CONFIG, "v3_config"), (V3_RUNTIME, "v3_runtime"), (AUTO_ROOT, "auto_root"), (LEDGER, "ledger"), (TEST_CWD, "test_cwd")):
        assert_test_path(path, name=name)
    if not V3_CONFIG.is_file() or not V3_RUNTIME.is_file():
        raise RuntimeError("v3_baseline_missing")
    if not LEDGER.is_file():
        raise RuntimeError("shared_ledger_missing")
    if not TEST_CWD.is_dir() or not PUBLIC_PROBE.is_file():
        raise RuntimeError("test_prerequisite_missing")
    candidate = validate_candidate(candidate_path)
    original_bytes = V3_CONFIG.read_bytes()
    original_sha = sha256_bytes(original_bytes)
    original = json.loads(original_bytes.decode("utf-8"))
    if not isinstance(original, dict):
        raise RuntimeError("v3_config_invalid")
    baseline_config = load_codex_config(V3_CONFIG)
    auto_payload = build_auto_installation_payload(original)
    if AUTO_ROOT.exists() and any(AUTO_ROOT.iterdir()):
        children = {item.name for item in AUTO_ROOT.iterdir()}
        reusable_partial = (
            children == {"data", "binding-payload.json"}
            and (AUTO_ROOT / "data").is_dir()
            and not any((AUTO_ROOT / "data").iterdir())
            and (AUTO_ROOT / "binding-payload.json").read_bytes() == json_bytes(auto_payload)
        )
        if not reusable_partial:
            raise RuntimeError("auto_target_must_be_fresh")
    AUTO_ROOT.mkdir(parents=True, exist_ok=True)
    AUTO_DATA.mkdir(parents=True, exist_ok=True)
    binding_path = AUTO_ROOT / "binding-payload.json"
    write_json(binding_path, auto_payload)
    try:
        # The installation_id is intentionally the v3 config-path hash.  The
        # binding is therefore validated through the existing v3 path while
        # its data directory is temporarily redirected to the fresh target.
        V3_CONFIG.write_bytes(json_bytes(auto_payload))
        temporary_config = replace(
            baseline_config,
            agent_id=AUTO_AGENT_ID,
            data_directory=AUTO_DATA,
            scope_ids=frozenset(auto_payload["scope_ids"]),
            audience_scopes=type(baseline_config.audience_scopes)(
                auto_payload["audience_scopes"]
            ),
            project_roots=type(baseline_config.project_roots)(
                {next(iter(baseline_config.project_roots)): auto_payload["audience_scopes"]["project"]}
            ),
        )
        MemoryCore(CoreConfig(temporary_config.to_binding())).initialize()
        verified = load_codex_config(V3_CONFIG)
        if verified.agent_id != AUTO_AGENT_ID or verified.data_directory != AUTO_DATA.resolve():
            raise RuntimeError("temporary_binding_verification_failed")
    finally:
        V3_CONFIG.write_bytes(original_bytes)
    runtime_payload = build_runtime_payload(auto_payload)
    write_json(AUTO_RUNTIME, runtime_payload)
    parsed = RuntimeInstanceConfig.from_mapping(json.loads(AUTO_RUNTIME.read_text(encoding="utf-8")))
    if parsed.binding.agent_id != AUTO_AGENT_ID or parsed.auxiliary is None:
        raise RuntimeError("new_runtime_binding_invalid")
    if parsed.auxiliary.budget.batch != ALLOCATION_BATCH or parsed.auxiliary.budget.total_call_cap != TOTAL_CALL_CAP:
        raise RuntimeError("budget_binding_invalid")
    (AUTO_ROOT / "source-prompt.txt").write_text(SOURCE_TEXT, encoding="utf-8")
    (AUTO_ROOT / "query-prompt.txt").write_text(QUERY_TEXT, encoding="utf-8")
    receipt = build_prepare_receipt(candidate=candidate, original_sha=original_sha, binding=auto_payload)
    write_json(AUTO_RECEIPT, receipt)
    if V3_CONFIG.read_bytes() != original_bytes:
        raise RuntimeError("v3_config_changed_during_prepare")
    return receipt


REQUIRED_TABLES = {"instance_meta", "instance_scopes", "source_events", "work_items", "claims"}


def readonly_snapshot() -> dict[str, Any]:
    db_path = AUTO_DATA / "memory.sqlite3"
    if not db_path.is_file():
        raise RuntimeError("auto_database_missing")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        source_count = int(db.execute("SELECT COUNT(*) FROM source_events").fetchone()[0])
        work_count = int(db.execute("SELECT COUNT(*) FROM work_items").fetchone()[0])
        claims_count = int(db.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        return {
            "schemaVersion": schema_version,
            "requiredTablesPresent": REQUIRED_TABLES <= tables,
            "tableCount": len(tables),
            "sourceEvents": source_count,
            "workItems": work_count,
            "claims": claims_count,
        }


def source_rows(prompt_hash: str) -> list[dict[str, Any]]:
    db_path = AUTO_DATA / "memory.sqlite3"
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT event_id,source_revision,content_sha256,capture_state,scope_id,session_id "
            "FROM source_events WHERE content_sha256=? ORDER BY event_id,source_revision",
            (prompt_hash,),
        ).fetchall()
    return [dict(row) for row in rows]


def work_rows(ref: str, revision: int) -> list[dict[str, Any]]:
    db_path = AUTO_DATA / "memory.sqlite3"
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT work_id,work_type,state,attempt,last_error_code FROM work_items "
            "WHERE subject_ref=? AND subject_revision=? ORDER BY work_type",
            (ref, revision),
        ).fetchall()
    return [dict(row) for row in rows]


class SourceWorkerTimeout(RuntimeError):
    def __init__(self, last: dict[str, Any]) -> None:
        super().__init__("automatic_source_worker_timeout")
        self.last = last


class SourceWorkerFailed(RuntimeError):
    def __init__(self, last: dict[str, Any]) -> None:
        super().__init__("automatic_source_worker_failed")
        self.last = last


def wait_for_source_ready(prompt_hash: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"sourceRows": [], "workRows": []}
    while time.monotonic() < deadline:
        rows = source_rows(prompt_hash)
        work = [item for row in rows for item in work_rows(row["event_id"], int(row["source_revision"]))]
        last = {"sourceRows": rows, "workRows": work}
        if any(item.get("state") == "failed" for item in work):
            raise SourceWorkerFailed(last)
        if len(rows) == 1 and rows[0]["capture_state"] == "complete":
            by_type = {item["work_type"]: item for item in work}
            if set(by_type) >= {"embed", "consolidate"} and all(item["state"] == "done" for item in by_type.values()):
                return last
        time.sleep(0.5)
    raise SourceWorkerTimeout(last)


def source_can_use_wakeup(last: Mapping[str, Any]) -> bool:
    rows = last.get("workRows")
    return (
        isinstance(rows, list)
        and bool(rows)
        and any(
            isinstance(item, Mapping)
            and item.get("state") == "pending"
            and item.get("attempt") == 0
            for item in rows
        )
    )


def read_codex_submission_rows(operation_ids_to_read: Mapping[str, str]) -> list[dict[str, Any]]:
    if not LEDGER.is_file():
        return []
    uri = f"file:{LEDGER.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT operation_id,batch,model,request_sha256,request_bytes,reserved_input,"
            "reserved_output,actual_input,actual_output,status,dispatch_status,usage_quality,"
            "reservation_origin,audit_source,historical_not_pre_dispatch,monetary_status,"
            "started_ns,finished_ns FROM codex_submissions WHERE operation_id IN (?,?,?) ORDER BY operation_id",
            tuple(operation_ids_to_read.values()),
        ).fetchall()
    return [dict(row) for row in rows]


def load_probe_module() -> Any:
    spec = importlib.util.spec_from_file_location("p12_public_appserver_probe", PUBLIC_PROBE)
    if spec is None or spec.loader is None:
        raise RuntimeError("public_probe_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_public_turn(probe: Any, prompt_path: Path) -> dict[str, Any]:
    args = SimpleNamespace(
        codex_exe=str(CODEX_EXE),
        cwd=str(TEST_CWD),
        fixture=None,
        output=None,
        server_args=["app-server"],
        send=True,
        prompt_file=str(prompt_path),
        thread_id=None,
        capture_public_test_context_text=True,
        capture_public_test_final_text=True,
    )
    report = probe.run_probe(args)
    if not isinstance(report, dict):
        raise RuntimeError("public_probe_report_invalid")
    safe = deepcopy(report)
    io = safe.get("io")
    if isinstance(io, dict):
        io["stderr"] = {"omitted": True}
    return safe


def _usage_mapping(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    aliases = {
        "inputTokens": "input_tokens", "input_tokens": "input_tokens",
        "outputTokens": "output_tokens", "output_tokens": "output_tokens",
    }
    result: dict[str, int] = {}
    for source, target in aliases.items():
        item = value.get(source)
        if type(item) is int and item >= 0:
            result[target] = item
    if set(result) != {"input_tokens", "output_tokens"}:
        return None
    total = value.get("totalTokens", value.get("total_tokens"))
    if total is not None and (type(total) is not int or total != result["input_tokens"] + result["output_tokens"]):
        return None
    return result


def current_turn_usage(report: Mapping[str, Any], *, baseline: Mapping[str, int] | None = None) -> tuple[dict[str, int] | None, list[dict[str, Any]]]:
    """Reconcile one turn from cumulative usage, with an explicit baseline.

    ``last`` describes only the latest internal request.  It is accepted for
    one fresh-thread event with a verified zero baseline, while multi-request
    turns and resumed threads require cumulative ``total`` snapshots.
    """
    send = report.get("send")
    if not isinstance(send, Mapping):
        candidate = report.get("query") if isinstance(report.get("query"), Mapping) else report
        if isinstance(candidate, Mapping) and isinstance(candidate.get("turnNotifications"), Mapping):
            send = candidate
    if not isinstance(send, Mapping):
        return None, []
    thread_id = send.get("threadId")
    turn_id = send.get("turnId")
    notifications = send.get("turnNotifications")
    if not isinstance(notifications, Mapping):
        return None, []
    events = notifications.get("usageEvents")
    matching = [
        dict(event)
        for event in events
        if isinstance(event, Mapping)
        and event.get("threadId") == thread_id
        and event.get("turnId") == turn_id
    ] if isinstance(events, list) else []
    if not matching:
        return None, []
    parsed: list[tuple[dict[str, Any], dict[str, int] | None, dict[str, int] | None]] = []
    for event in matching:
        token_usage = event.get("tokenUsage")
        last = _usage_mapping(token_usage.get("last")) if isinstance(token_usage, Mapping) else None
        total = _usage_mapping(token_usage.get("total")) if isinstance(token_usage, Mapping) else None
        if last is None and total is None:
            continue
        parsed.append((event, last, total))
    if not parsed:
        return None, matching
    if baseline is not None and any(type(baseline.get(key)) is not int or baseline[key] < 0 for key in ("input_tokens", "output_tokens")):
        return None, matching
    updated = [item[0].get("updated") for item in parsed]
    if all(type(value) is int for value in updated) or all(isinstance(value, str) for value in updated):
        ordered = sorted(parsed, key=lambda item: (item[0].get("updated"), matching.index(item[0])))
    elif all(value is None for value in updated):
        ordered = parsed
    else:
        return None, matching
    if baseline is None or not all(item[2] is not None for item in ordered):
        return None, matching
    previous = {"input_tokens": baseline["input_tokens"], "output_tokens": baseline["output_tokens"]}
    final: dict[str, int] | None = None
    for _, _, cumulative in ordered:
        assert cumulative is not None
        if any(cumulative[key] < previous[key] for key in previous):
            return None, matching
        previous = {key: cumulative[key] for key in previous}
        final = cumulative
    if final is None:
        return None, matching
    return {
        "input_tokens": final["input_tokens"] - baseline["input_tokens"],
        "output_tokens": final["output_tokens"] - baseline["output_tokens"],
    }, matching


def dispatch_primary(
    *, g2: Any, probe: Any, operation_id: str, prompt_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt_bytes = prompt_path.read_text(encoding="utf-8").rstrip("\r\n").encode("utf-8")
    reservation = g2.reserve(operation_id, prompt_bytes)
    if reservation.get("idempotent"):
        raise RuntimeError("operation_id_already_exists_no_retry")
    dispatched = False
    report: dict[str, Any] | None = None
    finish_result: dict[str, Any] | None = None
    try:
        with with_test_credentials():
            report = run_public_turn(probe, prompt_path)
        send = report.get("send") if isinstance(report, Mapping) else None
        provenance = report.get("rpcProvenance") if isinstance(report, Mapping) else None
        dispatched = bool(
            isinstance(send, Mapping)
            and not send.get("blocked")
            and isinstance(provenance, Mapping)
            and provenance.get("turnRpcsDispatched") == 1
        )
        usage, usage_events = current_turn_usage(report, baseline={"input_tokens": 0, "output_tokens": 0})
        finish_result = g2.finish(
            operation_id,
            status="completed" if dispatched else "not_dispatched",
            dispatched=dispatched,
            usage=usage,
        )
        return (
            summarize_turn(report),
            {
                "operationId": operation_id,
                "reservation": reservation,
                "finish": finish_result,
                "dispatched": dispatched,
                "usage": usage,
                "usageEvents": usage_events,
            },
        )
    except Exception:
        # A reserved operation is always finalized.  If the event stream did
        # not expose a reliable last breakdown, g2.finish intentionally keeps
        # the reservation as an unknown upper bound.
        usage = None
        if report is not None:
            usage, _ = current_turn_usage(report, baseline={"input_tokens": 0, "output_tokens": 0})
        g2.finish(
            operation_id,
            status="completed" if dispatched else "not_dispatched",
            dispatched=dispatched,
            usage=usage,
        )
        raise


def context_packets(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = report.get("userPromptSubmitEvidence")
    if not isinstance(evidence, Mapping):
        return []
    runtime = evidence.get("runtime")
    if not isinstance(runtime, Mapping):
        return []
    entries = runtime.get("contextOutputEntries")
    return [dict(item) for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []


def summarize_turn(report: Mapping[str, Any]) -> dict[str, Any]:
    send = report.get("send")
    if not isinstance(send, Mapping):
        raise RuntimeError("turn_send_report_missing")
    notifications = send.get("turnNotifications")
    notifications = dict(notifications) if isinstance(notifications, Mapping) else {}
    return {
        "threadId": send.get("threadId"),
        "turnId": send.get("turnId"),
        "promptSha256": send.get("promptSha256"),
        "turnNotifications": notifications,
        "userPromptSubmitEvidence": report.get("userPromptSubmitEvidence"),
        "contextPackets": context_packets(report),
        "errors": report.get("errors") if isinstance(report.get("errors"), list) else [],
        "rpcProvenance": report.get("rpcProvenance"),
    }


def switch_config(auto_config_bytes: bytes) -> None:
    V3_CONFIG.write_bytes(auto_config_bytes)


def run(candidate_path: Path, wait_seconds: float) -> dict[str, Any]:
    assert_test_path(AUTO_ROOT, name="auto_root")
    candidate = validate_candidate(candidate_path)
    if not AUTO_RECEIPT.is_file():
        raise RuntimeError("prepare_receipt_missing")
    prepared = json.loads(AUTO_RECEIPT.read_text(encoding="utf-8"))
    if prepared.get("status") != "PREPARED_ONLY":
        raise RuntimeError("prepare_receipt_not_fresh")
    if prepared.get("sourceCommit") != SOURCE_COMMIT:
        raise RuntimeError("prepared_commit_mismatch")
    prepared_candidate = prepared.get("candidate")
    if not isinstance(prepared_candidate, Mapping) or prepared_candidate.get("receiptSha256") != candidate.get("receiptSha256"):
        raise RuntimeError("prepared_candidate_mismatch")
    for output in (AUTO_ROOT / "run-receipt.json", AUTO_ROOT / "run-failure.json"):
        if output.exists():
            raise RuntimeError("run_receipt_already_exists_no_retry")
    if prepared.get("originalV3ConfigSha256") != sha256_file(V3_CONFIG):
        raise RuntimeError("v3_baseline_hash_changed")
    if SOURCE_ANSWER in QUERY_TEXT:
        raise RuntimeError("query_contains_source_answer")
    if len(set(operation_ids().values())) != 3:
        raise RuntimeError("operation_ids_not_unique")
    before_config = V3_CONFIG.read_bytes()
    before_runtime = V3_RUNTIME.read_bytes()
    before_db = sha256_file(V3_ROOT / "data" / "memory.sqlite3")
    auto_install = json.loads((AUTO_ROOT / "binding-payload.json").read_text(encoding="utf-8"))
    auto_runtime = json.loads(AUTO_RUNTIME.read_text(encoding="utf-8"))
    auto_config_bytes = json_bytes(auto_install)
    if auto_runtime.get("auxiliary", {}).get("budget", {}).get("total_call_cap") != TOTAL_CALL_CAP:
        raise RuntimeError("prepared_total_call_cap_mismatch")
    if auto_runtime.get("auxiliary", {}).get("budget", {}).get("batch_call_cap") != BATCH_CALL_CAP:
        raise RuntimeError("prepared_batch_call_cap_mismatch")
    snapshot_before = readonly_snapshot()
    if (
        not snapshot_before.get("requiredTablesPresent")
        or snapshot_before.get("sourceEvents") != 0
        or snapshot_before.get("workItems") != 0
        or snapshot_before.get("claims") != 0
    ):
        raise RuntimeError("auto_database_not_fresh")
    prompt_source = AUTO_ROOT / "source-prompt.txt"
    prompt_query = AUTO_ROOT / "query-prompt.txt"
    if (
        prompt_source.read_text(encoding="utf-8").rstrip("\r\n") != SOURCE_TEXT
        or prompt_query.read_text(encoding="utf-8").rstrip("\r\n") != QUERY_TEXT
    ):
        raise RuntimeError("prompt_fixture_changed")
    probe = load_probe_module()
    g2 = load_g2_driver()
    source_report: dict[str, Any] | None = None
    query_report: dict[str, Any] | None = None
    wakeup_summary: dict[str, Any] | None = None
    source_ready: dict[str, Any] | None = None
    source_summary: dict[str, Any] | None = None
    query_summary: dict[str, Any] | None = None
    budget_records: list[dict[str, Any]] = []
    try:
        switch_config(auto_config_bytes)
        source_summary, source_budget = dispatch_primary(
            g2=g2, probe=probe, operation_id=operation_ids()["source"], prompt_path=prompt_source
        )
        budget_records.append(source_budget)
        source_report = {"send": {"threadId": source_summary["threadId"], "turnId": source_summary["turnId"]}}
        if source_summary["promptSha256"] != sha256_text(SOURCE_TEXT):
            raise RuntimeError("source_prompt_hash_mismatch")
        try:
            source_ready = wait_for_source_ready(sha256_text(SOURCE_TEXT), wait_seconds)
        except SourceWorkerFailed as failed:
            source_ready = failed.last
            raise
        except SourceWorkerTimeout as timeout:
            source_ready = timeout.last
            if not source_can_use_wakeup(timeout.last):
                raise
            wakeup_path = AUTO_ROOT / "wakeup-prompt.txt"
            write_text_exclusive(wakeup_path, WAKEUP_TEXT)
            wakeup_summary, wakeup_budget = dispatch_primary(
                g2=g2, probe=probe, operation_id=operation_ids()["wakeup"], prompt_path=wakeup_path
            )
            budget_records.append(wakeup_budget)
            source_ready = wait_for_source_ready(sha256_text(SOURCE_TEXT), wait_seconds)
        query_summary, query_budget = dispatch_primary(
            g2=g2, probe=probe, operation_id=operation_ids()["query"], prompt_path=prompt_query
        )
        budget_records.append(query_budget)
        query_report = {"send": {"threadId": query_summary["threadId"], "turnId": query_summary["turnId"]}}
        if query_summary["promptSha256"] != sha256_text(QUERY_TEXT):
            raise RuntimeError("query_prompt_hash_mismatch")
        if source_summary["threadId"] is None or query_summary["threadId"] is None:
            raise RuntimeError("thread_id_missing")
        if source_summary["threadId"] == query_summary["threadId"]:
            raise RuntimeError("threads_not_distinct")
        packets = query_summary["contextPackets"]
        packet_text = "\n".join(str(item.get("text", "")) for item in packets)
        if len(packets) != 1 or SOURCE_ANSWER not in packet_text:
            raise RuntimeError("native_context_packet_missing_source_fact")
        answer_items = query_summary["turnNotifications"].get("publicItems") or []
        final_answers = [
            item for item in answer_items
            if isinstance(item, Mapping)
            and item.get("type") == "agentMessage"
            and item.get("phase") in ("final_answer", None)
            and isinstance(item.get("text"), str)
        ]
        if len(final_answers) != 1 or sha256_text(final_answers[0]["text"]) != sha256_text(SOURCE_ANSWER):
            raise RuntimeError("final_answer_not_exact_allowed_test_answer")
        usage_events = [record.get("usageEvents", []) for record in budget_records]
        final = {
            "status": "COMPLETED_AUTO_NATIVE_RECALL",
            "schema": SCHEMA_VERSION,
            "sourceCommit": SOURCE_COMMIT,
            "candidate": candidate,
            "operationIds": operation_ids(),
            "turns": {
                "primaryTurnsUsed": 2 + int(wakeup_summary is not None),
                "primaryTurnsMaximum": 3,
                "source": source_summary,
                "query": query_summary,
                "wakeup": wakeup_summary,
            },
            "sourceRefs": source_ready,
            "answer": {
                "publicItems": answer_items,
                "allowedAnswerSha256": sha256_text(SOURCE_ANSWER),
                "answerBodyRetained": True,
                "syntheticOnly": True,
            },
            "usage": {
                "events": usage_events,
                "accountingField": "tokenUsage.total delta from trusted thread baseline",
                "totalRetainedSeparately": True,
                "unknownReservationsRetained": True,
                "codexSubmissionLedgerMutated": True,
                "auxiliaryLedgerMutatedByController": False,
                "submissions": budget_records,
            },
            "verification": {
                "sourceCaptureAndAutomaticWork": True,
                "newThreadQuery": True,
                "nativeContextPacketObserved": True,
                "manualDrain": False,
                "manualEmbedding": False,
                "sourceDbWritesByController": False,
                "sharedBudgetGuardsUsed": True,
            },
        }
        write_json(AUTO_ROOT / "run-receipt.json", final, exclusive=True)
        return final
    except Exception as exc:
        existing_budget_rows = read_codex_submission_rows(operation_ids())
        failure = {
            "status": "FAILED_AUTO_NATIVE_RECALL",
            "schema": SCHEMA_VERSION,
            "sourceCommit": SOURCE_COMMIT,
            "candidate": candidate,
            "operationIds": operation_ids(),
            "errorCode": type(exc).__name__ + ":" + str(exc),
            "sourceTurn": source_summary,
            "queryTurn": query_summary,
            "wakeupTurn": wakeup_summary,
            "sourceRefs": source_ready,
            "budgetRows": existing_budget_rows,
            "manualDrain": False,
            "manualEmbedding": False,
            "codexSubmissionLedgerMutated": bool(existing_budget_rows),
            "auxiliaryLedgerMutatedByController": False,
        }
        write_json(AUTO_ROOT / "run-failure.json", failure, exclusive=True)
        raise
    finally:
        V3_CONFIG.write_bytes(before_config)
        if V3_CONFIG.read_bytes() != before_config:
            raise RuntimeError("v3_config_restore_mismatch")
        if V3_RUNTIME.read_bytes() != before_runtime:
            raise RuntimeError("v3_runtime_changed")
        if sha256_file(V3_ROOT / "data" / "memory.sqlite3") != before_db:
            raise RuntimeError("v3_database_changed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P12 bounded Codex automatic recall TEST driver")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--candidate-receipt", type=Path, default=CANDIDATE_RECEIPT_DEFAULT)
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    candidate_path = args.candidate_receipt.resolve()
    try:
        if args.wait_seconds <= 0 or args.wait_seconds > MAX_WAIT_SECONDS:
            raise RuntimeError("wait_seconds_out_of_bounds")
        if args.prepare_only:
            receipt = prepare(candidate_path)
        else:
            receipt = run(candidate_path, args.wait_seconds)
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "errorCode": type(exc).__name__ + ":" + str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
