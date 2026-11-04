"""Profile synthetic hooks on all five supported adapters without starting a host."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from performance import distribution
from scenarios import ScenarioStore
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.identity import resolve_runtime_audience
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, bind_hermes_identity
from scope_recall.core import CoreConfig, MemoryCore


def timed(call):
    """Measure a completed synchronous operation using the real monotonic clock."""
    started = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - started) * 1000


def observe_core(core, sample: dict):
    """Measure API calls inside a hook without changing their behavior or deadlines."""
    originals = {}
    for name in ("record_host_event", "record_event", "recall_packet"):
        original = getattr(core, name)
        originals[name] = original

        def wrapped(*args, _name=name, _original=original, **kwargs):
            started = time.perf_counter()
            try:
                return _original(*args, **kwargs)
            finally:
                sample[_name] = sample.get(_name, 0) + (time.perf_counter() - started) * 1000

        setattr(core, name, wrapped)
    return originals


def restore_core(core, originals):
    """Remove only this profiler's wrappers from its own synthetic core."""
    for name, method in originals.items():
        setattr(core, name, method)


def client_samples(scenes: ScenarioStore, host: str, count: int) -> dict:
    """Profile every hook the client registers, with real config and audience reads."""
    config = scenes.configs[host]
    events = (
        ("SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "Interrupt", "SessionEnd")
        if host == "codex"
        else (("UserPromptSubmit", "Stop") if host == "dsh" else ("UserPromptSubmit", "Stop", "SessionEnd"))
    )
    results = {}
    for event in events:
        samples = []
        for index in range(count):
            session = str(uuid4())
            sample = {"query_embedding_network_ms": None}
            _, sample["read_config_ms"] = timed(lambda: load_shared_client(config.home, host=host))
            _, sample["identity_ms"] = timed(lambda: resolve_runtime_audience(config, str(scenes.root)))
            core = scenes.hooks[host].core
            originals = observe_core(core, sample)
            try:
                _, sample["hook_ms"], _ = scenes.hook(
                    host,
                    f"TEST-profile{index} copper color preference",
                    session=session,
                    event=event,
                    last_assistant_message="TEST-profile synthetic response",
                    tool_name="Bash",
                    tool_input={"command": "echo TEST-synthetic-input"},
                    tool_response="TEST output",
                )
            finally:
                restore_core(core, originals)
            sample["write_ms"] = sample.get("record_host_event", sample.get("record_event", 0))
            sample["retrieval_ms"] = sample.get("recall_packet", 0)
            samples.append(sample)
        results[event] = {"elapsed": distribution([sample["hook_ms"] for sample in samples]), "samples": samples}
    return results


def hermes_samples(scenes: ScenarioStore, count: int) -> dict:
    """Initialize and exercise the actual Hermes provider with synthetic host inputs."""
    home = scenes.root / "hermes-home"
    kwargs = {
        "hermes_home": str(home),
        "platform": "cli",
        "agent_context": "primary",
        "agent_identity": "TEST-scenarios",
        "agent_workspace": "TEST-project",
        "user_id": "TEST-owner",
        "parent_session_id": "",
    }
    identity = bind_hermes_identity("TEST-hermes-profile", **kwargs)
    core = MemoryCore(CoreConfig(identity.binding))
    core.initialize()
    provider = ScopeRecallHermesAdapter(core=core)
    _, initialization = timed(lambda: provider.initialize("TEST-hermes-profile", **kwargs))
    results = {"initialize_ms": initialization, "events": {}}
    operations = {
        "on_turn_start": lambda i: provider.on_turn_start(i + 1, f"TEST-profile{i} color copper", turn_id=f"turn-{i}"),
        "observe_pre_llm": lambda i: provider.observe_pre_llm(
            session_id="TEST-hermes-profile", turn_id=f"turn-{i}", user_message=f"TEST-profile{i} color copper"
        ),
        "sync_turn": lambda i: provider.sync_turn(
            f"TEST-profile{i} color copper", "TEST synthetic reply", session_id="TEST-hermes-profile"
        ),
        "prefetch": lambda i: provider.prefetch(f"TEST-profile{i} color", session_id="TEST-hermes-profile"),
    }
    try:
        for name, operation in operations.items():
            samples = []
            for index in range(count):
                sample = {"query_embedding_network_ms": None}
                _, sample["identity_ms"] = timed(lambda: bind_hermes_identity("TEST-hermes-profile", **kwargs))
                originals = observe_core(core, sample)
                try:
                    _, sample["hook_ms"] = timed(lambda operation=operation, index=index: operation(index))
                finally:
                    restore_core(core, originals)
                sample["write_ms"] = sample.get("record_host_event", sample.get("record_event", 0))
                sample["retrieval_ms"] = sample.get("recall_packet", 0)
                samples.append(sample)
            results["events"][name] = {"elapsed": distribution([s["hook_ms"] for s in samples]), "samples": samples}
    finally:
        provider.shutdown()
    return results


def main():
    """Retain per-hook samples and an isolated interpreter-start measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    scenes = ScenarioStore(args.output / f"store-{time.time_ns()}")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "COMSPEC") if key in os.environ}
    environment.update(TMP=tempfile.gettempdir(), TEMP=tempfile.gettempdir(), PYTHONDONTWRITEBYTECODE="1")
    starts = []
    for _ in range(args.samples):
        _, elapsed = timed(
            lambda: subprocess.run(
                [sys.executable, "-I", "-B", "-c", "pass"], env=environment, timeout=10, check=True, capture_output=True
            )
        )
        starts.append(elapsed)
    clients = {host: client_samples(scenes, host, args.samples) for host in scenes.hooks}
    report = {
        "schema": "hook-profile/1",
        "model_calls": 0,
        "process_start": distribution(starts),
        "clients": clients,
        "hermes": hermes_samples(scenes, args.samples),
        "query_embedding_network_ms": None,
        "limits": "small synthetic store; config/identity microbenchmarks are separate from inclusive hook timings; "
        "interpreter startup excludes adapter imports",
    }
    (args.output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"hosts": [*clients, "hermes"], "process_start_p95_ms": report["process_start"]["p95_ms"]}))


if __name__ == "__main__":
    main()
