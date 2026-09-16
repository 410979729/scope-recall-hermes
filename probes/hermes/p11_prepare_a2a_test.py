"""Prepare the isolated P11/P18 Hermes A2A TEST home.

Preparation is intentionally offline: it creates only the bounded TEST home,
Core SQLite database, manifests, configuration, and a redacted receipt.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scope_recall.adapters.hermes.installation import build_installation_manifest, install_hermes_scope_recall, load_installation_manifest, manifest_payload
from scope_recall.runtime.model_budget import read_auxiliary_budget_status
from probes.hermes.p11_a2a_testkit import (
    A2A_PORT, ARCHIVE, BATCH_NAME, BUDGET_CONFIG, CORE_DIR, HERMES_CONFIG, HERMES_HOME, HERMES_PYTHON,
    HERMES_ROOT, HOST_HEAD, HOST_VERSION, LEDGER, LOCAL_BRIDGE_TOKEN_ENV,
    MAIN_BRIDGE_PORT, MAIN_MODEL, MAX_MODEL_POSTS, REPO_ROOT as KIT_REPO_ROOT, RUNTIME_CONFIG,
    STATE, TEST_AGENT_ID, TEST_CONTEXT, TEST_USER_ID, UPSTREAM_ENDPOINT,
    UPSTREAM_KEY_ENV, assert_test_path, budget_mapping, digest_bytes, host_python_sha256, port_status, scrub,
    install_batch_guard, shared_ledger_status,
    write_json,
)


def _json_config(*, hermes_home: Path | None = None) -> dict:
    return {
        "model": {
            "default": MAIN_MODEL,
            "provider": "p11-deepseek-flash",
            "max_tokens": 4096,
            "context_length": 131072,
            "streaming": False,
        },
        "custom_providers": [{
            "name": "p11-deepseek-flash",
            "base_url": f"http://127.0.0.1:{MAIN_BRIDGE_PORT}/v1",
            "key_env": LOCAL_BRIDGE_TOKEN_ENV,
            "api_mode": "chat_completions",
            "model": MAIN_MODEL,
            "extra_body": {"thinking": {"type": "disabled"}},
            "models": {MAIN_MODEL: {"context_length": 131072}},
        }],
        "agent": {"max_turns": 3, "api_max_retries": 0, "verbose": False,
                  "system_prompt": "TEST only. Do not disclose credentials or leave the TEST home."},
        "memory": {"memory_enabled": False, "user_profile_enabled": False,
                   "nudge_interval": 0, "provider": "scope_recall"},
        "auxiliary": {"title_generation": {"enabled": False}},
        "plugins": {"enabled": ["platforms/a2a", "scope_recall"]},
        "session_reset": {"mode": "idle", "idle_minutes": 1, "notify": False},
        "gateway": {"platforms": {"a2a": {"enabled": True,
                                               "extra": {"host": "127.0.0.1", "port": A2A_PORT}}}},
        "platform_toolsets": {"a2a": []},
        "terminal": {"backend": "local", "cwd": str(hermes_home or HERMES_HOME)},
        "compression": {"enabled": False},
        "fallback_model": [],
    }


def _runtime_config(manifest, *, core_dir: Path | None = None, ledger: Path | None = None) -> dict:
    payload = manifest_payload(manifest)
    return {
        "binding": {
            "agent_id": manifest.agent_id,
            "installation_id": manifest.installation_id,
            "data_directory": str(manifest.data_directory),
            "scope_ids": sorted(manifest.scope_ids),
            "test_mode": True,
        },
        "session_id": "TEST-a2a-session",
        "allowed_scope_ids": sorted(manifest.scope_ids),
        "actor_origin": "human_direct",
        "request_seconds": 45.0,
        "drain_seconds": 45.0,
        "max_items": 8,
        "lease_seconds": 60.0,
        "auxiliary": {
            "external_embedding": False,
            "external_consolidation": False,
            "installation_dir": str(core_dir or CORE_DIR),
            "ledger_path": str(ledger or LEDGER),
            "budget": budget_mapping(),
            "consolidation": {
                "model": "mimo-v2.5",
                "endpoint": UPSTREAM_ENDPOINT,
                "credential_env": UPSTREAM_KEY_ENV,
                "output_limit_field": "max_completion_tokens",
                "max_output_tokens": 4096,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "stream": False,
                "n": 1,
            },
            "consolidation_reserve_input": 32768,
        },
        "manifest_audiences": payload["audiences"],
    }


def _plugin_files() -> tuple[str, str]:
    init = (REPO_ROOT / "distribution" / "hermes" / "__init__.py").read_text(encoding="utf-8")
    metadata = (REPO_ROOT / "distribution" / "hermes" / "plugin.yaml").read_text(encoding="utf-8")
    return init, metadata


@dataclass(frozen=True)
class PrepareLayout:
    """Isolated prepare paths. Tests must use a fixture layout, not the live P11 home."""

    state: Path
    hermes_home: Path
    core_dir: Path
    archive: Path
    ledger: Path
    budget_config: Path
    runtime_config: Path
    hermes_config: Path
    p15_sentinel: Path
    kind: str = "synthetic_isolated_prepare_fixture"


def _ledger_counts(ledger: Path) -> dict[str, int | bool]:
    if not ledger.is_file():
        return {"ledger_exists": False, "global_requests": 0, "batch_requests": 0}
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        global_count = int(db.execute("SELECT count(*) FROM requests").fetchone()[0])
        batch_count = int(db.execute("SELECT count(*) FROM requests WHERE batch=?", (BATCH_NAME,)).fetchone()[0])
    return {"ledger_exists": True, "global_requests": global_count, "batch_requests": batch_count}


def _digest_if_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return digest_bytes(path.read_bytes())


def run_prepare(layout: PrepareLayout) -> tuple[int, dict]:
    """Execute the offline prepare path against an explicit layout.

    Receipt fields are observed from the layout. This is not a historical P11
    run report.
    """
    before = _ledger_counts(layout.ledger)
    p15_before = _digest_if_file(layout.p15_sentinel)
    if not before["ledger_exists"]:
        receipt = {"prepared": False, "reason": "ledger_missing", "layout_kind": layout.kind, "ledger": before}
        return 2, receipt
    if int(before["batch_requests"]) >= MAX_MODEL_POSTS:
        receipt = {
            "prepared": False,
            "reason": "P11 batch cap already reached",
            "layout_kind": layout.kind,
            "ledger": before,
        }
        return 2, receipt
    if layout.state.exists():
        receipt = {
            "prepared": False,
            "reason": "TEST state already exists",
            "layout_kind": layout.kind,
            "state": str(layout.state),
        }
        return 2, receipt

    for path in (layout.state, layout.hermes_home, layout.core_dir, layout.archive):
        resolved = path.resolve()
        if resolved != layout.state.resolve() and layout.state.resolve() not in resolved.parents:
            raise RuntimeError(f"TEST path escaped: {resolved}")
        path.mkdir(parents=True, exist_ok=True)
    provisional = build_installation_manifest(
        layout.hermes_home, agent_id=TEST_AGENT_ID, platform="a2a", user_id=TEST_USER_ID,
        agent_workspace="hermes", conversation_key=TEST_CONTEXT, test_mode=True,
    )
    audiences = _test_audiences(provisional.audience_scopes)
    install_hermes_scope_recall(
        layout.hermes_home,
        agent_id=TEST_AGENT_ID,
        platform="a2a",
        user_id=TEST_USER_ID,
        agent_workspace="hermes",
        conversation_key=TEST_CONTEXT,
        audiences=audiences,
        test_mode=True,
    )
    manifest = load_installation_manifest(layout.hermes_home)
    write_json(layout.budget_config, budget_mapping())
    layout.runtime_config.write_text(
        json.dumps(_runtime_config(manifest, core_dir=layout.core_dir, ledger=layout.ledger), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    layout.hermes_config.write_text(
        json.dumps(_json_config(hermes_home=layout.hermes_home), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    plugin_dir = layout.hermes_home / "plugins" / "scope_recall"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    init, metadata = _plugin_files()
    (plugin_dir / "__init__.py").write_text(init, encoding="utf-8")
    (plugin_dir / "plugin.yaml").write_text(metadata, encoding="utf-8")
    after = _ledger_counts(layout.ledger)
    p15_after = _digest_if_file(layout.p15_sentinel)
    observed_posts = int(after["batch_requests"]) - int(before["batch_requests"])
    receipt = {
        "prepared": True,
        "layout_kind": layout.kind,
        "state": str(layout.state),
        "hermes_home": str(layout.hermes_home),
        "core_db": str(manifest.data_directory / "memory.sqlite3"),
        "ledger": str(layout.ledger),
        "observed": {
            "ledger_global_before": int(before["global_requests"]),
            "ledger_global_after": int(after["global_requests"]),
            "ledger_batch_before": int(before["batch_requests"]),
            "ledger_batch_after": int(after["batch_requests"]),
            "model_posts_in_this_prep": observed_posts,
            "p15_sentinel_digest_before": p15_before,
            "p15_sentinel_digest_after": p15_after,
            "p15_unchanged": p15_before is not None and p15_before == p15_after,
        },
        "routes": {"main": MAIN_MODEL, "auxiliary": "mimo-v2.5", "fallback": []},
        "credential_values_written": False,
    }
    write_json(layout.archive / "prepare.json", scrub(receipt))
    return 0, receipt


def _test_audiences(audience_scopes: dict) -> list[dict]:
    """Match the unthreaded A2A source emitted by Frozen H's adapter.

    A2A's ``MessageEvent`` has no thread_id; the empty string is deliberate and
    must not be replaced by the CLI-only ``main`` default.

    Supplying ``audiences`` replaces the convenience grants wholesale, so the
    owner_private mapping the installer always emits has to be restated here or
    the manifest is rejected. It is copied field for field from the generated
    one (``adapters/hermes/installation.py`` owner mapping), so this declares
    the same grant rather than a new one.
    """
    conversation_scope = audience_scopes["conversation"]
    owner_scope = audience_scopes["owner_private"]
    return [{
        "platform": "a2a", "user_id": TEST_USER_ID, "chat_type": "private", "chat_id": TEST_USER_ID,
        "thread_id": "main", "gateway_session_key": "", "agent_workspace": "hermes",
        "allowed_scope_ids": [owner_scope], "writable_scope_ids": [owner_scope],
        "capture_scope_id": owner_scope,
        "kind": "owner_private",
    }, {
        "platform": "a2a", "user_id": TEST_USER_ID, "chat_type": "dm", "chat_id": TEST_CONTEXT,
        # A2A carries neither a thread nor a gateway session. Both stay empty by
        # the same reasoning; ``_EMPTY_ROUTE_FIELDS`` admits exactly these.
        "thread_id": "", "gateway_session_key": "", "agent_workspace": "hermes",
        # v3 requires the write grant to be declared rather than inferred from
        # ``capture_scope_id``. Capture already writes to the conversation scope,
        # so naming it here states the existing grant; it widens nothing.
        "allowed_scope_ids": [conversation_scope], "writable_scope_ids": [conversation_scope],
        "capture_scope_id": conversation_scope,
        "kind": "conversation",
    }]


def main() -> int:
    if KIT_REPO_ROOT != REPO_ROOT:
        raise RuntimeError("repository root mismatch")
    status = port_status()
    if not all(status.values()):
        print(json.dumps({"prepared": False, "reason": "port_occupied", "ports": status}, sort_keys=True))
        return 2
    ledger_status = shared_ledger_status()
    if not ledger_status["ledger_exists"]:
        raise RuntimeError("frozen shared ledger missing")
    install_batch_guard()
    for path in (STATE, HERMES_HOME, CORE_DIR, ARCHIVE):
        assert_test_path(path)
    layout = PrepareLayout(
        state=STATE,
        hermes_home=HERMES_HOME,
        core_dir=CORE_DIR,
        archive=ARCHIVE,
        ledger=LEDGER,
        budget_config=BUDGET_CONFIG,
        runtime_config=RUNTIME_CONFIG,
        hermes_config=HERMES_CONFIG,
        p15_sentinel=STATE.parent / "p15-readonly-sentinel.json",
        kind="live_p11_prepare_home",
    )
    code, receipt = run_prepare(layout)
    if receipt.get("prepared"):
        wrapper = (HERMES_HOME / "plugins" / "scope_recall" / "__init__.py").read_bytes()
        source = (HERMES_ROOT / "plugins" / "platforms" / "a2a" / "adapter.py").read_bytes()
        observed = receipt.get("observed") if isinstance(receipt.get("observed"), dict) else {}
        receipt.update({
            "ledger_is_frozen_shared": True,
            "shared_ledger_baseline": {**read_auxiliary_budget_status(LEDGER), **ledger_status},
            "host": {"root": str(HERMES_ROOT), "python": str(HERMES_PYTHON), "head": HOST_HEAD, "version": HOST_VERSION,
                     "python_sha256": host_python_sha256(),
                     "source_sha256": hashlib.sha256(source).hexdigest(),
                     "wrapper_sha256": hashlib.sha256(wrapper).hexdigest()},
            "ports": {"a2a": A2A_PORT, "main_bridge": MAIN_BRIDGE_PORT},
            "model_posts_upper_bound": MAX_MODEL_POSTS,
            "model_posts_used": int(observed.get("ledger_batch_after", 0)),
            "credential_env_names": [LOCAL_BRIDGE_TOKEN_ENV, UPSTREAM_KEY_ENV],
        })
        write_json(ARCHIVE / "prepare.json", scrub(receipt))
    print(json.dumps(scrub(receipt), ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
