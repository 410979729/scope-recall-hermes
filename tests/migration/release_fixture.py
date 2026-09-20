"""Test-only builder for a store written by the previous release's own code.

The child process imports the frozen ``v3.1.0`` tree from ``git archive``
before anything of the current package, exactly as ``legacy_fixture`` does for
the 2.0 baseline, so the fixture carries what that release really wrote:
schema 1108, one copied lineage row per episode revision, its work queue.
Every fixture the older upgrade tests used was a fresh store downgraded by
hand, which is how a wrong version stamp in the 1107 step went unnoticed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PREVIOUS_RELEASE = "v3.1.0"
PREVIOUS_SCHEMA = 1108


def build_previous_release_store(directory: str | Path, *, repo_root: str | Path, sources: int = 12) -> Path:
    """Return the ``memory.sqlite3`` the previous release wrote into ``directory``."""
    target = Path(directory).resolve()
    root = Path(repo_root).resolve()
    script = r'''
import io, pathlib, subprocess, sys, tarfile, tempfile
from dataclasses import replace

target = pathlib.Path(sys.argv[1]).resolve()
repo = pathlib.Path(sys.argv[2]).resolve()
tag = sys.argv[3]
count = int(sys.argv[4])
with tempfile.TemporaryDirectory(prefix="scope-recall-release-") as td:
    package = pathlib.Path(td) / "scope_recall"
    package.mkdir()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", tag])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        tf.extractall(package)
    # The gate's sitecustomize binds scope_recall to the checkout under test
    # before this script runs; evict that and every path that would shadow
    # the frozen tree (see tests/migration/legacy_fixture.py).
    for module_name in [name for name in list(sys.modules) if name == "scope_recall" or name.startswith("scope_recall.")]:
        del sys.modules[module_name]
    retained = []
    for entry in sys.path:
        try:
            resolved = pathlib.Path(entry).resolve() if entry else None
        except OSError:
            resolved = None
        if resolved is not None and (resolved.name == "scope_recall" or resolved == repo):
            continue
        retained.append(entry)
    sys.path[:] = retained
    sys.path.insert(0, td)
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore

    target.mkdir(parents=True, exist_ok=True)
    binding = InstanceBinding("TEST-agent", "TEST-installation", target, frozenset({"TEST-scope"}), True)
    context = replace(TrustedContext(binding, "TEST-session", frozenset({"TEST-scope"}), "human_direct"),
                      task_anchor="TEST-previous-release")
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    for index in range(count):
        tool = index % 3 == 2
        event = dict(protocol_version="1.1", source_event_key=f"TEST-release/{index}", source_revision=1,
                     origin="tool_observation" if tool else "human_direct", role="tool" if tool else "user",
                     content=f"TEST previous release event {index}", occurred_at=f"2026-09-01T12:{index:02d}:00Z",
                     recorded_at=f"2026-09-01T12:{index:02d}:00Z", time_precision="instant",
                     capture_state="complete", evidence_refs=[])
        actor = replace(context, actor_origin=event["origin"])  # capture checks the actor against the event
        receipt = core.record_event(actor, event, scope_id="TEST-scope", remaining_seconds=10)
        assert receipt.disposition == "inserted", receipt
'''
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([sys.executable, "-c", script, str(target), str(root), PREVIOUS_RELEASE, str(sources)],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr or exc.stdout or "previous release fixture subprocess failed") from exc
    return target / "memory.sqlite3"


__all__ = ["PREVIOUS_RELEASE", "PREVIOUS_SCHEMA", "build_previous_release_store"]
