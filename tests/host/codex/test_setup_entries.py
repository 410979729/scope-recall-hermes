"""Windows setup acceptance invokes the installed hook commands on synthetic conversations."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.local_endpoint import endpoints, resident_running
from scope_recall.maintenance.setup import build_plan, parser
from scope_recall.maintenance.setup_apply import apply_setup
from scope_recall.maintenance.setup_verify import MARKER, QUESTION, STATEMENT
from scope_recall.runtime.worker_entry import run_worker

from tests.packaging.test_setup import fake_user  # noqa: F401 -- shared isolated host fixture.


def disable_resident(home, host):
    """Keep installed-hook acceptance runs from starting detached library services."""
    runtime = load_shared_client(home, host).runtime_config_path
    config = json.loads(runtime.read_text(encoding="utf-8"))
    config["resident_recall_minutes"] = 0
    runtime.write_text(json.dumps(config), encoding="utf-8")


@pytest.mark.parametrize("host,folder", [("codex", ".codex"), ("claude-code", ".claude"), ("workbuddy", ".workbuddy")])
def test_installed_hook_command_recalls_a_previous_session(fake_user, host, folder, record_property):
    home = fake_user / folder
    home.mkdir()
    started = time.monotonic()
    result = apply_setup(build_plan(parser().parse_args(["--host", host, "--test-mode", "--worker", "manual"])))
    assert result["status"] == "ready", result
    # This acceptance test exercises the installed hook and worker, not a long-lived recall service.
    # Disable residence before either can request a detached start, including one that has not yet registered.
    disable_resident(home, host)
    path = home / "settings.json" if host == "workbuddy" else home / "skills" / "scope-recall" / "hooks" / "hooks.json"
    hook = json.loads(path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    command = hook["command"] if host == "workbuddy" else shlex.split(hook["command"])
    # Codex uses its generated Windows launcher; the other hosts run the command through a shell.
    if host == "codex" and os.name == "nt":
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", hook["commandWindows"]]

    def invoke(session, text):
        event = dict(
            hook_event_name="UserPromptSubmit",
            session_id=session,
            turn_id=session + "-turn",
            generation_id=session,
            prompt_id=session,
            cwd=str(fake_user),
            prompt=text,
        )
        child = subprocess.run(
            command,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            shell=host == "workbuddy",
        )
        assert child.returncode == 0, child.stderr
        return child.stdout

    invoke("TEST-installed-first", STATEMENT)
    worker = result["hosts"][0]["worker"]["command"]
    child = subprocess.run(worker, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert child.returncode == 0, child.stderr
    answer = invoke("TEST-installed-second", QUESTION)
    assert MARKER in answer, answer
    assert not resident_running(home, host)
    assert not (endpoints(home) / "resident.start").exists()
    record_property("seconds_to_installed_recall", time.monotonic() - started)
    record_property("installed_command", json.dumps(command))


def test_installed_hermes_registration_runs_in_an_isolated_process(fake_user, record_property):
    home = fake_user / ".hermes"
    home.mkdir()
    started = time.monotonic()
    result = apply_setup(build_plan(parser().parse_args(["--host", "hermes", "--test-mode", "--worker", "manual"])))
    assert result["status"] == "ready", result
    probe = r"""
import importlib.util, json, sys, subprocess
from pathlib import Path
from types import SimpleNamespace
home = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("installed_provider", home / "plugins/scope-recall/__init__.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
providers = []
ctx = SimpleNamespace(register_memory_provider=providers.append, register_hook=lambda *a, **kw: None)
module.register(ctx)
p = providers[0]
kwargs = dict(hermes_home=str(home), platform="cli", agent_context="primary", agent_identity="scope-recall",
              agent_workspace="hermes", user_id="local", parent_session_id="")
p.initialize("TEST-first", **kwargs)
p.observe_pre_llm(session_id="TEST-first", turn_id="TEST-first-turn",
                  user_message="The synthetic bluebird project marker is amber-orchid.")
p.shutdown()
worker = subprocess.run(json.loads(sys.argv[2]), capture_output=True, timeout=120)
assert worker.returncode == 0
p = type(p)()
p.initialize("TEST-second", **kwargs)
try: print(json.dumps(p.prefetch("What is the synthetic bluebird project marker?")))
finally: p.shutdown()
"""
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe, str(home), json.dumps(result["hosts"][0]["worker"]["command"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert child.returncode == 0, child.stderr
    assert MARKER in child.stdout, child.stdout
    record_property("seconds_to_installed_recall", time.monotonic() - started)


def test_installed_dsh_plugin_uses_its_configured_python_bridge(fake_user, record_property):
    import shutil

    node = os.environ.get("SCOPE_RECALL_TEST_NODE") or shutil.which("node")
    if "SCOPE_RECALL_TEST_NODE" not in os.environ:
        pytest.skip("native dsh plugin belongs to the host tier with its Node executable allowlist")
    home = fake_user / ".dsh"
    home.mkdir()
    started = time.monotonic()
    result = apply_setup(build_plan(parser().parse_args(["--host", "dsh", "--test-mode", "--worker", "manual"])))
    assert result["status"] == "ready", result
    disable_resident(home, "dsh")
    layers = yaml.safe_load((home / "cordis.patch.yml").read_text(encoding="utf-8"))
    rows = [row for layer in layers for row in layer.get("insert", []) if row.get("id") == "scope-recall"]
    config = dict(rows[0]["config"], prompt=STATEMENT, spool=str(home / "scope-recall" / "TEST-spool"))
    harness = Path(__file__).with_name("dsh_harness") / "harness.mjs"
    plugin = home / "scope-recall" / "dsh-plugin" / "index.mjs"
    child = subprocess.run(
        [node, str(harness), str(plugin), json.dumps(config), "turn"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert child.returncode == 0, child.stderr
    worker = subprocess.run(result["hosts"][0]["worker"]["command"], capture_output=True, timeout=120)
    assert worker.returncode == 0
    hook = [
        config["python"],
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.hook_entry",
        "--home",
        config["home"],
        "--host",
        "dsh",
    ]
    event = dict(
        hook_event_name="UserPromptSubmit",
        session_id="TEST-next-dsh",
        turn_id="TEST-turn",
        cwd=str(fake_user),
        prompt=QUESTION,
    )
    child = subprocess.run(hook, input=json.dumps(event), capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert child.returncode == 0, child.stderr
    assert MARKER in child.stdout, child.stdout
    assert not (endpoints(home) / "resident.start").exists()
    assert not resident_running(home, "dsh")
    record_property("seconds_to_installed_recall", time.monotonic() - started)
