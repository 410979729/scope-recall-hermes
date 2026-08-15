"""P0-A: read-only follower ToolService is fail-closed by default.

Unknown and action-dependent write surfaces must not mutate by omission.
Only proven read-only search/context/profile/fact (and other audited reads)
remain available. Receipts stay sanitized.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from plugins.memory import load_memory_provider

READ_ONLY_STATUS = "active_read_only"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _initialize(provider, hermes_home: Path, session: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="lease-user",
        chat_id="lease-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


@contextlib.contextmanager
def _external_lease_holder(storage_dir: Path, *, role: str = "external-process"):
    storage_dir.mkdir(parents=True, exist_ok=True)
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role={role!r})
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        print("RELEASED", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        yield child
    finally:
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.close()
                assert child.stdout.readline().strip() == "RELEASED"
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)


def _busy_payload(raw: str) -> dict:
    payload = json.loads(raw)
    assert "truth_writer_busy" in str(payload.get("error") or "")
    serialized = json.dumps(payload)
    assert "pid" not in serialized.lower()
    assert "hostname" not in serialized.lower()
    assert "memory.sqlite3" not in serialized
    return payload


def test_readonly_follower_default_denies_writes_and_unknown_tools(tmp_path):
    _write_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "maintenance_tools_enabled": True,
            "temporal_queries": {"enabled": True},
            "reflection": {"enabled": True, "write_candidates": True},
            "experience": {"enabled": True},
        },
    )
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "seed-tools")
    stored = json.loads(
        seeder.handle_tool_call(
            "scope_recall_store",
            {
                "content": "The lease follower still recalls the Aurora storage runbook.",
                "target": "ops",
            },
        )
    )
    assert stored.get("id")
    seeder.shutdown()

    reader = _provider()
    try:
        with _external_lease_holder(storage, role="external-writer"):
            _initialize(reader, tmp_path, "reader-tools")
            assert reader.runtime_status == READ_ONLY_STATUS

            search = json.loads(
                reader.handle_tool_call(
                    "scope_recall_search",
                    {"query": "Aurora storage runbook"},
                )
            )
            assert search.get("count", 0) >= 1
            context = json.loads(
                reader.handle_tool_call(
                    "scope_recall_context",
                    {"query": "Aurora storage runbook"},
                )
            )
            assert isinstance(context, dict)
            profile = json.loads(
                reader.handle_tool_call("scope_recall_profile", {})
            )
            assert isinstance(profile, dict)
            fact = json.loads(
                reader.handle_tool_call(
                    "scope_recall_fact",
                    {
                        "action": "current",
                        "subject": "Aurora",
                        "predicate": "uses",
                    },
                )
            )
            assert "error" not in fact or "truth_writer_busy" not in str(
                fact.get("error") or ""
            )

            denied = [
                ("scope_recall_govern", {"dry_run": True}),
                ("scope_recall_forgetting_run", {"apply": False}),
                (
                    "scope_recall_reflect",
                    {"query": "Aurora storage", "propose_memory": True},
                ),
                (
                    "scope_recall_experience_preflight",
                    {
                        "query": "Aurora storage runbook reuse",
                        "record_run": True,
                    },
                ),
                ("scope_recall_experience_promote", {"limit": 1}),
                (
                    "scope_recall_store",
                    {"content": "must not store from follower", "target": "ops"},
                ),
                (
                    "scope_recall_memory",
                    {"action": "update", "id": stored["id"], "content": "nope"},
                ),
                (
                    "scope_recall_playbook_create",
                    {"payload": {"title": "blocked playbook"}},
                ),
                ("scope_recall_repair", {}),
                ("scope_recall_future_mutate", {"payload": "unknown"}),
            ]
            for tool_name, args in denied:
                _busy_payload(reader.handle_tool_call(tool_name, args))

            inspect = json.loads(
                reader.handle_tool_call(
                    "scope_recall_memory",
                    {"action": "inspect", "id": stored["id"]},
                )
            )
            assert inspect.get("id") == stored["id"] or inspect.get("memory")
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


def test_readonly_busy_error_does_not_echo_adversarial_tool_name(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    seeder = _provider()
    _initialize(seeder, tmp_path, "seed-adversarial-tool")
    seeder.shutdown()
    adversarial_tool = (
        r"C:\Users\Administrator\token-"
        + "ghp_"
        + "abcdefghijklmnopqrstuvwxyz012345"
        + r"\scope_recall_store"
    )
    reader = _provider()
    try:
        with _external_lease_holder(storage, role="provider"):
            _initialize(reader, tmp_path, "reader-adversarial-tool")
            raw = reader.handle_tool_call(
                adversarial_tool,
                {"content": "must not store", "target": "ops"},
            )
            payload = _busy_payload(raw)
            serialized = json.dumps(payload)
            assert "Administrator" not in serialized
            assert "ghp_" not in serialized
            assert adversarial_tool not in serialized
            assert "C:\\Users" not in serialized
            assert r"C:\Users" not in serialized
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass
