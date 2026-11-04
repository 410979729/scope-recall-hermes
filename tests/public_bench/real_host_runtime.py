"""Use the shipped configuration, budget and supervisor boundaries on synthetic stores."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from real_host_io import write_json
from scenario_store import ScenarioStore


def route_template(chat_endpoint: str, chat_model: str):
    """Return environment references, explicit recall floor and bounded metered accounting.

    The chat endpoint and model come from the operator: a deployment's address never lives in the checkout.
    A plain-HTTP endpoint is allowed explicitly. The local chat server is unbilled;
    zero chat rates record that choice, while Gemini input is metered separately.
    Explicit route descriptors have their own space identity and require a floor.
    """
    from scope_recall.core.recall_policy import BUILTIN_VECTOR_THRESHOLD, EMBEDDING_SPACE

    models = [chat_model, EMBEDDING_SPACE["model"]]
    return dict(
        vector_threshold=BUILTIN_VECTOR_THRESHOLD,
        vector=dict(
            backend="sqlite",
            storage_dir=str(Path(sys.prefix).absolute() / "TEST-unbound-vectors"),
            table_name="scope_recall",
            dimensions=3072,
            metric="cosine",
        ),
        auxiliary=dict(
            external_embedding=True,
            external_consolidation=True,
            consolidation=dict(
                kind="openai",
                model=chat_model,
                endpoint=chat_endpoint,
                credential_env="TEST_SR_CHAT_KEY",
                allow_insecure_endpoint=chat_endpoint.startswith("http://"),
                output_limit_field="max_tokens",
                max_output_tokens=8192,
                temperature=0,
                thinking={"type": "disabled"},
            ),
            embedding=dict(
                credential_env="TEST_SR_GEMINI_KEY",
                model=EMBEDDING_SPACE["model"],
                endpoint=EMBEDDING_SPACE["endpoint"],
                dimensions=3072,
                dialect="gemini",
            ),
            budget=dict(
                batch="TEST-local-chat-unbilled",
                cap_micro_usd=250_000,
                total_call_cap=1500,
                total_input_cap=8_000_000,
                total_output_cap=1_000_000,
                approved_models=models,
                pricing={
                    chat_model: dict(input_usd_per_million=0, output_usd_per_million=0),
                    EMBEDDING_SPACE["model"]: dict(input_usd_per_million="0.20", output_usd_per_million=0),
                },
            ),
        ),
    )


def load_routes(path):
    """Validate a supplied recipe using setup's own schema and freeze the requested routes."""
    from scope_recall.maintenance.setup_configuration import read_models

    value = read_models(path)
    if value is None:
        raise ValueError("--routes is required for real models")
    chat, embedding = value["auxiliary"]["consolidation"], value["auxiliary"]["embedding"]
    if not chat.get("model") or not chat.get("endpoint"):
        raise ValueError("chat route names no model or endpoint")
    if embedding["model"] != "gemini-embedding-2":
        raise ValueError("embedding route differs from the requested M6 model")
    return value


def utc_now():
    """Match production's lexically ordered UTC timestamps using the actual wall clock."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def library(run, name, *, models=False):
    """Create one isolated shared fixture and retain ordinary scheduling defaults."""
    from scope_recall.adapters.hermes.shared_entries import read_shared_payload
    from scope_recall.maintenance.setup_configuration import runtime_payload
    from scope_recall.maintenance.shared import _ledger_made, store_binding

    scenes = ScenarioStore(run.root / name)
    run.stores.append(scenes)
    if run.args.dry_run:
        return scenes
    if not run.args.dry_run:
        # Existing cores retain the clock object. Replace its source, not the
        # object, so no component can keep a stale fixture timestamp.
        scenes.clock.utc_now = utc_now
    recipe = run.routes if models else None
    for config in scenes.configs.values():
        value = runtime_payload(config.to_binding(), models=recipe, worker="manual")
        value["resident_recall_minutes"] = 0
        _ledger_made(value)
        write_json(config.runtime_config_path, value)
    payload = read_shared_payload(scenes.store)
    binding = store_binding(payload, scenes.store, frozenset(payload["scope_ids"]))
    value = runtime_payload(binding, models=recipe, worker="on-demand")
    value["resident_recall_minutes"] = 0
    _ledger_made(value)
    write_json(scenes.store / "runtime-config.json", value)
    return scenes


def supervisor(run, scenes):
    """Start the production finite supervisor, preserving its ordinary cadence and retries."""
    config = scenes.store / "runtime-config.json"
    process = run.children.start(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "scope_recall.runtime.worker_watchdog",
            "--config",
            str(config),
            "--python",
            sys.executable,
        ]
    )
    return process


def queue(scenes):
    """Retain all work channels, errors, age and outstanding IDs without mutating scheduling."""
    rows = scenes.rows("SELECT work_id,work_type,state,available_at,last_error_code FROM work_items")
    pending = [row for row in rows if row["state"] in {"pending", "leased", "failed"}]
    oldest = min((row["available_at"] for row in pending), default=None)
    return dict(
        depth=len(pending),
        oldest=oldest,
        oldest_age_seconds=max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds())
        if oldest
        else 0,
        rows=rows,
    )


def active(scenes, marker):
    """Inspect authoritative active versions with evidence; text in raw sources cannot pass."""
    return scenes.rows(
        "SELECT c.claim_id,c.subject,v.payload_json,v.state FROM claims c JOIN claim_versions v "
        "ON c.claim_id=v.claim_id AND c.current_revision=v.revision WHERE v.state='active' "
        "AND (c.subject LIKE ? OR v.payload_json LIKE ?)",
        ("%" + marker + "%", "%" + marker + "%"),
    )


def wait_until(predicate, seconds, *, interval=5):
    """Observe real wall time; never advance a business clock or edit retry rows."""
    started = time.monotonic()
    while True:
        value = predicate()
        if value or time.monotonic() - started >= seconds:
            return value, time.monotonic() - started
        time.sleep(min(interval, max(0, seconds - (time.monotonic() - started))))


def doctor(scenes, host="codex"):
    """Run the actual doctor against this synthetic home and return its full diagnosis."""
    from scope_recall.maintenance.doctor import run_doctor

    return run_doctor(host=host, instance_root=scenes.root / (host + "-home"), project_root=scenes.root).to_dict()


def homes(scenes):
    """Return the two disposable entry homes used by the host harness."""
    return {host: scenes.root / (host + "-home") for host in ("claude-code", "codex")}
