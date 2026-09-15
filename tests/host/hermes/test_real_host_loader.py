"""Real Hermes v0.21.0 loader/dispatch probe in an isolated HOME."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _frozen_hermes_fixture() -> tuple[Path, Path, dict]:
    """Resolve only an explicitly supplied, manifest-backed TEST fixture."""
    root_value = os.environ.get("SCOPE_RECALL_TEST_HERMES_ROOT")
    manifest_value = os.environ.get("SCOPE_RECALL_TEST_HERMES_MANIFEST")
    if not root_value or not manifest_value:
        pytest.skip(
            "set SCOPE_RECALL_TEST_HERMES_ROOT and "
            "SCOPE_RECALL_TEST_HERMES_MANIFEST to run the frozen Hermes loader probe"
        )
    root = Path(root_value)
    manifest_path = Path(manifest_value)
    if root.is_symlink() or manifest_path.is_symlink():
        pytest.fail("TEST Hermes source and manifest must not be symlinks")
    if not root.is_dir() or not manifest_path.is_file():
        pytest.fail("configured Hermes TEST source/manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("environment") != "TEST-Hermes-runtime-v1":
        pytest.fail("manifest is not the frozen TEST-Hermes-runtime-v1 environment")
    if manifest.get("source_commit") != "79445a496c86a19332ad786494b8384d2167e2d0":
        pytest.fail("manifest does not identify frozen Hermes source 79445")
    declared_source = Path(str(manifest.get("frozen_source", ""))).resolve()
    if declared_source != root.resolve():
        pytest.fail("manifest frozen_source does not match configured TEST source")
    archive = manifest.get("archive_contents", {})
    if archive.get("inside_test_root") is not True or archive.get("git_directory_copied") is not False:
        pytest.fail("manifest does not prove an isolated TEST source fixture")
    if archive.get("production_config_copied") is not False:
        pytest.fail("manifest allows production configuration in TEST source")
    checksums = manifest.get("sha256", {})
    for relative in (
        "gateway/run.py",
        "plugins/platforms/a2a/adapter.py",
        "plugins/platforms/a2a/protocol.py",
    ):
        expected = checksums.get(relative)
        path = root / relative
        if not isinstance(expected, str) or not path.is_file():
            pytest.fail(f"manifest lacks required source hash: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        if actual != expected.upper():
            pytest.fail(f"frozen source hash mismatch: {relative}")
    for relative in (
        "agent/agent_init.py",
        "hermes_cli/plugins.py",
        "plugins/memory/__init__.py",
    ):
        if not (root / relative).is_file():
            pytest.fail(f"frozen loader source is missing: {relative}")
    return root, manifest_path, manifest


def test_real_hermes_memory_loader_and_hooks(tmp_path):
    hermes_root, manifest_path, manifest = _frozen_hermes_fixture()
    worktree = Path(__file__).parents[3]
    home = tmp_path / "hermes-home"
    plugin = home / "plugins" / "scope_recall"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "from scope_recall.adapters.hermes import register_adapter\n"
        "# MemoryProvider plugin entrypoint\n"
        "def register(ctx):\n    return register_adapter(ctx)\n",
        encoding="utf-8",
    )
    (plugin / "plugin.yaml").write_text(
        "description: isolated P11 host loader probe\n", encoding="utf-8"
    )
    evidence_dir = worktree / ".execution" / "TEST-P11-HOST"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "host-loader-run.json"
    code = textwrap.dedent(
        """
        from pathlib import Path
        import json, os, sqlite3
        from scope_recall.adapters.hermes import install_hermes_scope_recall

        home = Path(os.environ["HERMES_HOME"])
        import inspect, sys
        import agent.agent_init as agent_init
        import hermes_cli.plugins as hermes_plugins

        install_hermes_scope_recall(
            home, agent_id="TEST-host-agent", platform="cli", user_id="local",
            agent_workspace="TEST-host", test_mode=False,
        )
        from agent.memory_provider import MemoryProvider
        from agent.memory_manager import MemoryManager
        from hermes_cli.plugins import invoke_hook
        from plugins.memory import load_memory_provider

        provider = load_memory_provider("scope_recall", register_skills=False)
        assert isinstance(provider, MemoryProvider), type(provider)
        provider.initialize(
            "host-session-a", hermes_home=str(home), platform="cli", user_id="local",
            agent_context="primary", agent_identity="TEST-host-agent",
            agent_workspace="TEST-host",
        )
        manager = MemoryManager()
        manager.add_provider(provider)

        # Actual global plugin dispatcher: pre_llm UUID precedes ordinal start.
        invoke_hook(
            "pre_llm_call", session_id="host-session-a", turn_id="uuid-a",
            platform="cli", sender_id="local", user_message="host loader anchor",
        )
        provider.on_turn_start(1, "host loader anchor")
        manager.prefetch_all("host loader anchor", session_id="host-session-a")
        manager.sync_all("host loader anchor", "host assistant", session_id="host-session-a")
        assert manager.flush_pending(timeout=3)

        provider.on_session_switch("host-session-b")
        old = manager.prefetch_all("host loader anchor", session_id="host-session-b")
        assert "host loader anchor" in old, old
        invoke_hook(
            "post_tool_call", session_id="host-session-b", turn_id="uuid-b",
            platform="cli", sender_id="local", tool_call_id="tool-b",
            tool_name="echo", result="tool result", status="success",
        )

        provider.on_session_switch(
            "host-group", platform="telegram", chat_type="group",
            chat_id="unknown", thread_id="main",
        )
        invoke_hook(
            "pre_llm_call", session_id="host-group", turn_id="uuid-g",
            platform="telegram", sender_id="local", user_message="must not persist",
        )
        assert not provider.diagnostics.current_source_refs
        assert any("audience_unmapped" in gap or "no_allowed_scope" in gap
                   for gap in provider.diagnostics.capability_gaps)

        manager.shutdown_all()
        with sqlite3.connect(home / "scope-recall" / "memory.sqlite3") as conn:
            event_count = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        print(json.dumps({
            "provider": provider.name,
            "python_executable": sys.executable,
            "provider_module": inspect.getfile(type(provider)),
            "memory_provider_module": inspect.getfile(MemoryProvider),
            "hook_module": inspect.getfile(invoke_hook),
            "agent_init_module": inspect.getfile(agent_init),
            "hermes_plugins_module": inspect.getfile(hermes_plugins),
            "old_rendered": bool(old),
            "event_count": event_count,
            "shutdown": provider.diagnostics.shutdown_state,
            "gaps": provider.diagnostics.capability_gaps,
        }, ensure_ascii=False))
        """
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join((str(worktree), str(hermes_root)))
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    record = {
        "hermes_source": str(hermes_root),
        "manifest": str(manifest_path),
        "source_commit": manifest["source_commit"],
        "source_provenance": {
            "kind": "frozen_test_fixture",
            "fixture": "TEST-Hermes-runtime-v1/hermes-source-79445",
            "manifest_verified": True,
            "live_fleet_dependency": False,
        },
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    evidence_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["provider"] == "scope-recall"
    assert result["old_rendered"] is True
    assert result["event_count"] >= 3
    assert result["shutdown"]["status"] in {"drained", "timed_out"}
