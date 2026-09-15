"""Prepare the isolated G0 CLI/A2A Hermes TEST home without model traffic.

This is a TEST-only host-entry fixture.  It creates a fresh v6 home, an
explicit cross-entry conversation mapping, and complete runtime configuration.
It never reads or writes the shared P11 ledger and never contacts an upstream.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPO_ROOT.parents[1] / "TEST-Hermes-runtime-v1"
HERMES_ROOT = TEST_ROOT / "hermes-source-79445"
HERMES_PYTHON = TEST_ROOT / "venv" / "Scripts" / "python.exe"
STATE = REPO_ROOT.parents[1] / "TEST-P11-Hermes-v6"
HERMES_HOME = STATE / "hermes-home"
CORE_DIR = HERMES_HOME / "scope-recall"
RUNTIME_CONFIG = CORE_DIR / "runtime-config.json"
HERMES_CONFIG = HERMES_HOME / "config.yaml"
RECEIPT = STATE / "archive" / "prepare-cli-a2a-v6.json"

AGENT_ID = "default"
WORKSPACE = "hermes"
CLI_USER = "TEST-operator"
A2A_USER = "TEST-user"
CONTEXT = "TEST-context-v6-1"
CLI_ZERO_PORT = 29992
CLI_ZERO_MODEL = "test-zero-upstream"
CLI_ZERO_TOKEN_ENV = "SCOPE_RECALL_TEST_G0_LOCAL_TOKEN"


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def _conversation_scope() -> str:
    # This is the logical conversation scope shared by the two exact host
    # entries.  Its content is intentionally not tied to A2A's origin.
    return "|".join(
        (
            _scope_component("audience", "conversation"),
            _scope_component("platform", "cli"),
            _scope_component("key", CONTEXT),
        )
    )


def _budget() -> dict:
    return {
        "batch": "G0_CLI_A2A_V6",
        "cap_micro_usd": 0,
        "total_input_cap": 0,
        "total_output_cap": 0,
        "total_call_cap": 0,
        "batch_call_cap": 0,
        "max_request_bytes": 786432,
        "default_reserve_input": 32768,
        "default_reserve_output": 4096,
        "model_reserve_output": {CLI_ZERO_MODEL: 4096, "mimo-v2.5": 131072},
        "model_token_caps": {
            CLI_ZERO_MODEL: {"input": 0, "output": 0},
            "mimo-v2.5": {"input": 0, "output": 0},
        },
        "pricing": {
            CLI_ZERO_MODEL: {"input_usd_per_million": "0", "output_usd_per_million": "0"},
            "mimo-v2.5": {"input_usd_per_million": "0", "output_usd_per_million": "0"},
        },
        "approved_models": [CLI_ZERO_MODEL, "mimo-v2.5"],
    }


def _audiences(conversation_scope: str) -> list[dict]:
    return [
        {
            "platform": "cli",
            "chat_type": "cli",
            "chat_id": "local",
            "thread_id": "main",
            "agent_workspace": WORKSPACE,
            "allowed_scope_ids": [conversation_scope],
            "capture_scope_id": conversation_scope,
            "kind": "conversation",
        },
        {
            "platform": "a2a",
            "chat_type": "dm",
            "chat_id": CONTEXT,
            "thread_id": "",
            "agent_workspace": WORKSPACE,
            "allowed_scope_ids": [conversation_scope],
            "capture_scope_id": conversation_scope,
            "kind": "conversation",
        },
    ]


def _config(manifest: dict) -> dict:
    return {
        "model": {
            "default": CLI_ZERO_MODEL,
            "provider": "g0-local-zero",
            "max_tokens": 128,
            "context_length": 131072,
            "streaming": False,
        },
        "custom_providers": [
            {
                "name": "g0-local-zero",
                "base_url": f"http://127.0.0.1:{CLI_ZERO_PORT}/v1",
                "key_env": CLI_ZERO_TOKEN_ENV,
                "api_mode": "chat_completions",
                "model": CLI_ZERO_MODEL,
                "extra_body": {"thinking": {"type": "disabled"}},
                "models": {CLI_ZERO_MODEL: {"context_length": 131072}},
            }
        ],
        "agent": {
            "max_turns": 1,
            "api_max_retries": 0,
            "verbose": False,
            "system_prompt": "TEST only. Never disclose credentials or leave this TEST home.",
        },
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "nudge_interval": 0, "provider": "scope_recall"},
        "auxiliary": {"title_generation": {"enabled": False}},
        "plugins": {"enabled": ["platforms/a2a", "scope_recall"]},
        "session_reset": {"mode": "idle", "idle_minutes": 1, "notify": False},
        "platform_toolsets": {"cli": [], "a2a": []},
        "terminal": {"backend": "local", "cwd": str(HERMES_HOME)},
        "compression": {"enabled": False},
        "fallback_model": [],
    }


def _runtime_config(manifest: dict) -> dict:
    scope_ids = sorted(manifest["scope_ids"])
    return {
        "binding": {
            "agent_id": manifest["agent_id"],
            "installation_id": manifest["installation_id"],
            "data_directory": str(CORE_DIR.resolve()),
            "scope_ids": scope_ids,
            "test_mode": True,
        },
        "session_id": "TEST-g0-cli-session",
        "allowed_scope_ids": scope_ids,
        "actor_origin": "human_direct",
        "project_id": "TEST-P11-Hermes-v6",
        "branch_id": "TEST-main",
        "request_seconds": 45.0,
        "drain_seconds": 45.0,
        "max_items": 8,
        "lease_seconds": 60.0,
        "vector": {
            "backend": "lancedb",
            "storage_dir": str((CORE_DIR / "vectors" / _space_id()).resolve()),
            "table_name": "source_vectors",
            "dimensions": 3072,
            "metric": "cosine",
            "test_injection_override": False,
        },
        "vector_threshold": 0.653189984350642,
        "auxiliary": {
            "external_embedding": False,
            "external_consolidation": False,
            "installation_dir": str(CORE_DIR.resolve()),
            "ledger_path": str((CORE_DIR / "auxiliary-budget.sqlite3").resolve()),
            "budget": _budget(),
            "consolidation": {
                "model": "mimo-v2.5",
                "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
                "credential_env": "SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY",
                "output_limit_field": "max_completion_tokens",
                "max_output_tokens": 4096,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "stream": False,
                "n": 1,
            },
            "consolidation_reserve_input": 32768,
        },
        "manifest_audiences": manifest["audiences"],
    }


def _space_id() -> str:
    # Keep this calculation source-independent so the fixture can be prepared
    # using the dedicated Hermes venv and the installed Scope Recall package.
    from scope_recall.core.recall_policy import SPACE_ID

    return SPACE_ID


def main() -> int:
    if not HERMES_PYTHON.is_file() or not HERMES_ROOT.is_dir():
        raise RuntimeError("frozen Hermes TEST runtime is missing")
    if STATE.exists():
        raise RuntimeError(f"refusing to overwrite existing v6 state: {STATE}")
    STATE.mkdir(parents=True)
    HERMES_HOME.mkdir()
    CORE_DIR.mkdir()

    from scope_recall.adapters.hermes.installation import (
        build_installation_manifest,
        install_hermes_scope_recall,
        load_installation_manifest,
        manifest_payload,
    )

    provisional = build_installation_manifest(
        HERMES_HOME,
        agent_id=AGENT_ID,
        platform="cli",
        user_id=CLI_USER,
        agent_workspace=WORKSPACE,
        conversation_key=CONTEXT,
        test_mode=True,
    )
    conversation_scope = _conversation_scope()
    # Install the trusted CLI owner plus explicit cross-entry mappings.  The
    # A2A row does not include owner_private and is therefore not an operator.
    install_hermes_scope_recall(
        HERMES_HOME,
        agent_id=AGENT_ID,
        platform="cli",
        user_id=CLI_USER,
        agent_workspace=WORKSPACE,
        conversation_key=CONTEXT,
        audiences=_audiences(conversation_scope),
        test_mode=True,
    )
    manifest = manifest_payload(load_installation_manifest(HERMES_HOME))
    RUNTIME_CONFIG.write_text(json.dumps(_runtime_config(manifest), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    HERMES_CONFIG.write_text(json.dumps(_config(manifest), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    plugin_dir = HERMES_HOME / "plugins" / "scope_recall"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "distribution" / "hermes" / "__init__.py", plugin_dir / "__init__.py")
    shutil.copy2(REPO_ROOT / "distribution" / "hermes" / "plugin.yaml", plugin_dir / "plugin.yaml")

    receipt = {
        "prepared": True,
        "state": str(STATE.resolve()),
        "hermes_home": str(HERMES_HOME.resolve()),
        "core_db": str((CORE_DIR / "memory.sqlite3").resolve()),
        "host": {
            "root": str(HERMES_ROOT.resolve()),
            "python": str(HERMES_PYTHON.resolve()),
            "version": str(tomllib.loads((HERMES_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]),
            "head": "79445a496c86a19332ad786494b8384d2167e2d0",
            "python_sha256": hashlib.sha256(HERMES_PYTHON.read_bytes()).hexdigest(),
        },
        "installation": manifest,
        "entry_mapping": {
            "cli": {"platform": "cli", "chat_type": "cli", "chat_id": "local", "thread_id": "main", "operator_origin": "human_direct", "scope": conversation_scope},
            "a2a": {"platform": "a2a", "chat_type": "dm", "chat_id": CONTEXT, "thread_id": "", "operator_origin": "origin_unknown", "scope": conversation_scope, "owner_private_exposed": False},
        },
        "cli_command": f"hermes chat -q --query-file <TEST-public-input> -Q --source cli --provider g0-local-zero",
        "zero_upstream": {"host": "127.0.0.1", "port": CLI_ZERO_PORT, "model": CLI_ZERO_MODEL, "external_api": False},
        "dependencies": {"hermes_httpx": "0.28.1", "lancedb": "0.30.2", "pyarrow": "24.0.0", "mcp": "absent"},
        "config_integrity": {"embedding_dimensions": 3072, "embedding_space": _space_id(), "vector_threshold": 0.653189984350642, "external_embedding": False, "external_consolidation": False, "global_budget_mutated": False},
        "credential_values_written": False,
        "network_calls": 0,
        "production_rows_read": 0,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
