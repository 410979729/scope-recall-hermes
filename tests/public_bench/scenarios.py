"""Measure synthetic product scenarios through real hook and MCP facades.

Only temporary, explicitly supplied homes and one synthetic shared store are
created. No host executable, model adapter or installed user configuration is
loaded. Missing product capabilities are recorded as baseline gaps.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import lab  # Bind the source checkout before importing its adapters.
from scope_recall._version import __version__
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.mcp_server import _TOOLS, CodexMCPServer
from scope_recall.adapters.hermes.installation import build_installation_manifest
from scope_recall.adapters.hermes.shared_entries import (
    attach_shared_entry,
    attach_shared_record,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.core import CoreConfig, MemoryCore


class ScenarioStore:
    """Four real client adapters attached to one synthetic owner-private audience."""

    def __init__(self, root: Path):
        self.root, self.store = root, root / "shared"
        self.root.mkdir(parents=True, exist_ok=False)
        self.now = datetime.now(UTC).isoformat()
        write_shared_payload(self.store, new_shared_payload(self.store, agent_id="TEST-scenarios"))
        home = root / "hermes-home"
        home.mkdir()
        manifest = build_installation_manifest(
            home, agent_id="TEST-scenarios", user_id="TEST-owner", agent_workspace="TEST-project"
        )
        attach_shared_entry(self.store, manifest, entry_id="hermes", display_name="Hermes", now=self.now)
        owner = next(
            row for row in read_shared_payload(self.store)["entries"][0]["audiences"] if row["kind"] == "owner_private"
        )
        self.scope = owner["capture_scope_id"]
        self.hooks, self.mcp, self.configs = {}, {}, {}
        for host in ("codex", "claude-code", "workbuddy", "dsh"):
            client = root / f"{host}-home"
            attach_shared_record(
                self.store,
                client_entry_record(
                    host=host,
                    home=client,
                    entry_id=host,
                    display_name=host,
                    attached_at=self.now,
                    allowed_scope_ids=owner["allowed_scope_ids"],
                    writable_scope_ids=owner["writable_scope_ids"],
                    capture_scope_id=self.scope,
                ),
                now=self.now,
            )
            config = load_shared_client(client, host=host)
            core = MemoryCore(CoreConfig(config.to_binding()))
            core.initialize()
            self.configs[host] = config
            self.hooks[host] = CodexHookHandler(config, core=core)
            self.mcp[host] = CodexMCPServer(config, workspace=root, core=core)

    def hook(self, host: str, prompt: str, *, session: str | None = None, event="UserPromptSubmit", **fields):
        """Submit an authentic synthetic hook envelope and return its real output and elapsed ms."""
        session = session or str(uuid4())
        value = {
            "hook_event_name": event,
            "session_id": session,
            "turn_id": str(uuid4()),
            "prompt_id": str(uuid4()),
            "cwd": str(self.root),
            "prompt": prompt,
            **fields,
        }
        started = time.perf_counter()
        result = self.hooks[host].handle_payload(value)
        return result, (time.perf_counter() - started) * 1000, session

    def recall(self, host: str, query: str, *, mode="current", session: str | None = None):
        """Call the real MCP method with host-reserved thread metadata."""
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": session or str(uuid4())}))
        started = time.perf_counter()
        result = self.mcp[host].recall(ctx, "1.1", query, mode, 10, 4096, request_id=f"TEST-{uuid4().hex}")
        return result, (time.perf_counter() - started) * 1000

    def rows(self, where="1=1", args=()):
        """Read only the task's synthetic store for source counts and provenance."""
        with sqlite3.connect(self.store / "memory.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM source_events WHERE {where}", args)]


def contains(packet: object, text: str) -> bool:
    """Check rendered result content, without depending on adapter envelope nesting."""
    return text in json.dumps(packet, ensure_ascii=False)


def baseline(scenes: ScenarioStore, resources: Path) -> dict:
    """Record the product acceptance scenarios, keeping unsupported subconditions as gaps."""
    results = {}
    started = time.perf_counter()
    capture, _, _ = scenes.hook("codex", "TEST-azalea 配色是琥珀色。")
    recalled, _ = scenes.recall("codex", "TEST-azalea 配色")
    results["fresh_install"] = {
        "raw_search": contains(recalled, "琥珀色"),
        "setup_seconds": time.perf_counter() - started,
        "complete_memory": "unavailable_no_model_routes",
        "real_host": "not_run",
    }
    matrix_path = resources / "samples" / "matrix.json"
    matrix = json.loads(matrix_path.read_text()) if matrix_path.exists() else []
    results["upgrade"] = {
        "sample_versions": len(matrix),
        "passed": sum(row["status"] == "passed" for row in matrix),
        "target": __version__,
    }
    cross, ms = scenes.recall("claude-code", "TEST-azalea 配色")
    results["cross_entry"] = {
        "raw_cross_entry": contains(cross, "琥珀色"),
        "raw_read_ms": ms,
        "fact_promotion_p95": None,
        "fact_state": "unavailable_no_model_routes",
    }
    _, _, session = scenes.hook("codex", "TEST-bridge 请推荐两种架构。")
    scenes.hook(
        "codex",
        "",
        session=session,
        event="Stop",
        last_assistant_message="方案一使用文件；方案二使用 TEST-bridge 环形队列。",
    )
    scenes.hook("codex", "好，就用方案二。", session=session)
    adopted, _ = scenes.recall("claude-code", "TEST-bridge 上次决定的架构")
    with sqlite3.connect(scenes.store / "memory.sqlite3") as connection:
        decisions = connection.execute(
            "SELECT count(*) FROM claims WHERE kind='decision' AND subject LIKE '%TEST-bridge%'"
        ).fetchone()[0]
    results["adopted_plan"] = {
        "adopted_decisions": decisions,
        "plan_recalled": contains(adopted, "环形队列"),
        "passed": decisions > 0,
        "reason": "assistant plan capture and confirmation were submitted",
    }
    # Correction and deletion safety is covered by the host and contract tiers;
    # this check records whether a persisted recall-attempt log exists.
    with sqlite3.connect(scenes.store / "memory.sqlite3") as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    persisted = [name for name in tables if "recall" in name and ("log" in name or "attempt" in name)]
    results["correction"] = {
        "correction_notification_log": persisted,
        "passed": bool(persisted),
        "correction_checks": "contract and host tiers; simultaneous notification not available",
    }
    unrelated = []
    for prompt in ("好的", "按这个写", "继续", "Thanks!", "删除了吗"):
        packet, elapsed, _ = scenes.hook("codex", prompt, session=session)
        unrelated.append({"prompt": prompt, "injected": bool(packet), "ms": elapsed})
    results["unrelated_prompts"] = {"cases": unrelated, "false_injections": sum(row["injected"] for row in unrelated)}
    results["inspect_and_edit"] = {
        "persistent_attempt_tables": persisted,
        "passed": bool(persisted),
        "mcp_recall_returns_gaps": "gaps" in json.dumps(cross),
    }
    private_text = "TEST-private <private>TEST-unshared-orchid</private> public sentence."
    scenes.hook("codex", private_text)
    leaked = any("TEST-unshared-orchid" in row["content"] for row in scenes.rows())
    results["privacy"] = {
        "inline_private_absent": not leaked,
        "passed": not leaked,
        "other_privacy_checks": "private-mode and credential cases run in contract/host tiers",
    }
    scales = {}
    for count in (350000, 1000000):
        path = resources / "performance" / f"results-{count}.json"
        if path.exists():
            report = json.loads(path.read_text())
            scales[str(count)] = {
                "capture_p95_ms": report["warm"]["capture"]["p95_ms"],
                "recall_p95_ms": report["warm"]["auto_recall"]["p95_ms"],
                "within_1000ms": report["warm"]["auto_recall"]["p95_ms"] <= 1000,
            }
    results["scale"] = {"scales": scales, "real_embedding": "not_run"}
    outage, elapsed, _ = scenes.hook("codex", "TEST-azalea 配色是什么？")
    raw_outage, _ = scenes.recall("workbuddy", "TEST-azalea 配色")
    results["degraded_services"] = {
        "unconfigured_model_hook_returned": isinstance(outage, dict),
        "hook_ms": elapsed,
        "raw_search_available": contains(raw_outage, "琥珀色"),
        "provider_recovery": "not_run_without_model_routes",
    }
    scenes.hook(
        "codex",
        "",
        session=session,
        event="Stop",
        last_assistant_message="TEST-handoff 已检查布局；下一步检查配色；等待主人选择尺寸。",
    )
    handoff, elapsed, _ = scenes.hook("claude-code", "", event="SessionStart")
    results["handoff"] = {
        "handoff_tool": any("handoff" in name for name, _, _ in _TOOLS),
        "session_start_ms": elapsed,
        "automatic_handoff": contains(handoff, "TEST-handoff"),
    }
    return results


def main():
    """Run synthetic hooks/MCP calls and retain the current-version baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    scenes = ScenarioStore(args.output / f"store-{time.time_ns()}")
    results = baseline(scenes, args.resources)
    report = {
        "schema": "scenarios/1",
        "version": __version__,
        "model_calls": 0,
        "real_host_processes": 0,
        "construction_seconds": time.perf_counter() - started,
        "scenarios": results,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
