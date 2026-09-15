"""Actual process-exit recovery checks against isolated target databases.

The child exits abruptly via os._exit, without Python finally/SQLite close.
No network/model/production activity is allowed in the child.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def child(target: Path, stage: str) -> None:
    allowed = (target, ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
               Path(os.environ.get("SystemRoot", "/usr")).resolve())
    def audit(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2"}:
            raise PermissionError("TEST child cannot call network or external processes")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            mode, flags = args[1:3]
            writing = isinstance(mode, str) and any(c in mode for c in "wax+")
            writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and not path.is_relative_to(target):
                raise PermissionError("TEST write outside target")
            if not any(path.is_relative_to(root) for root in allowed):
                raise PermissionError("TEST read outside source/runtime/target")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            if not Path(args[0]).resolve().is_relative_to(target):
                raise PermissionError("TEST mutation outside target")
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location("scope_recall", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = package
    spec.loader.exec_module(package)
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.contracts import InstanceBinding, TrustedContext
    import scope_recall.core.storage as storage
    binding = InstanceBinding("TEST-crash-agent", "TEST-crash-install", target / "TEST-data", frozenset({"TEST-scope"}), True)
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    context = TrustedContext(binding, "TEST-crash-session", binding.scope_ids, "human_direct")
    event = dict(protocol_version="1.1", source_event_key="TEST-crash/message1", source_revision=1,
                 origin="human_direct", role="user", content="TEST_CRASH_SOURCE 银灰底突出轮廓。",
                 occurred_at=None, recorded_at="2026-09-06T07:00:00Z", time_precision="unknown",
                 capture_state="complete", evidence_refs=[], dataset_id="SYNTHETIC_TEST_ONLY")
    if stage == "inspect_replay":
        before = asdict(core.status(context))
        replay = core.record_event(context, event, scope_id="TEST-scope")
        after = asdict(core.status(context))
        matches = core.search_sources(context, "轮廓")
        conn = storage.connect_truth_database(core.storage.path, mode="ro")
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            lexical_count = conn.execute("SELECT count(*) FROM lexical_projection").fetchone()[0]
            work_types = [r[0] for r in conn.execute("SELECT work_type FROM work_items ORDER BY work_type")]
            query_plan = [list(r) for r in conn.execute("EXPLAIN QUERY PLAN SELECT event_id,source_revision FROM lexical_projection WHERE term=?", ("轮廓",))]
        finally:
            conn.close()
        print(json.dumps(dict(before=before, replay=asdict(replay), after=after, integrity=integrity,
                             lexical_count=lexical_count, work_types=work_types, query_plan=query_plan,
                             exact_source=bool(matches and matches[0].event["content"] == event["content"]))))
        return
    original = storage.connect_truth_database
    class TerminatingConnection:
        def __init__(self, conn):
            self.conn = conn
        @property
        def in_transaction(self):
            return self.conn.in_transaction
        def execute(self, sql, parameters=()):
            result = self.conn.execute(sql, parameters)
            if stage == "after_source_before_index" and sql.startswith("INSERT INTO source_events"):
                os._exit(72)
            return result
        def executemany(self, sql, parameters):
            result = self.conn.executemany(sql, parameters)
            if stage == "after_index_before_work" and sql.startswith("INSERT INTO lexical_projection"):
                os._exit(73)
            return result
        def commit(self):
            if stage == "before_commit":
                os._exit(74)
            self.conn.commit()
            if stage == "after_commit_before_receipt":
                os._exit(75)
        def rollback(self):
            self.conn.rollback()
        def close(self):
            self.conn.close()
    def factory(*args, **kwargs):
        if stage == "before_transaction":
            os._exit(71)
        return TerminatingConnection(original(*args, **kwargs))
    storage.connect_truth_database = factory
    core.record_event(context, event, scope_id="TEST-scope")
    raise AssertionError("expected abrupt exit did not occur")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]).resolve(), sys.argv[3])
        return 0
    started = time.perf_counter()
    evidence = ROOT / "verification/P04"
    evidence.mkdir(exist_ok=True)
    execution = ROOT / ".execution" / f"TEST-P04-crash-{time.time_ns()}"
    execution.mkdir(parents=True)
    results = []
    stages = {"before_transaction": 71, "after_source_before_index": 72, "after_index_before_work": 73,
              "before_commit": 74, "after_commit_before_receipt": 75}
    for stage, expected_code in stages.items():
        target = execution / f"TEST-{stage}"
        target.mkdir()
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE"}}
        for name in ("HOME", "USERPROFILE", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "HERMES_HOME"):
            directory = target / name.lower(); directory.mkdir()
            env[name] = str(directory)
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        command = [sys.executable, "-I", "-X", "utf8", "-B", str(Path(__file__).resolve()), "--child", str(target)]
        crashed = subprocess.run([*command, stage], cwd=target, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        recovered = subprocess.run([*command, "inspect_replay"], cwd=target, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        observation = json.loads(recovered.stdout) if recovered.returncode == 0 else {}
        persisted = stage == "after_commit_before_receipt"
        checks = {
            "actual_abrupt_exit": crashed.returncode == expected_code,
            "new_process_reopens_and_replays": recovered.returncode == 0,
            "commit_boundary_observed": observation.get("before", {}).get("sources") == int(persisted),
            "precommit_work_rolled_back": observation.get("before", {}).get("pending_work") == 2*int(persisted),
            "one_source_after_replay": observation.get("after", {}).get("sources") == 1,
            "one_pair_of_required_work": observation.get("work_types") == ["consolidate", "embed"],
            "real_lexical_source_recovered": observation.get("exact_source") is True,
            "database_integrity": observation.get("integrity") == "ok",
            "honest_replay_disposition": observation.get("replay", {}).get("disposition") == ("duplicate" if persisted else "inserted"),
        }
        results.append(dict(stage=stage, command=command, child_exit=crashed.returncode, child_stderr=crashed.stderr,
                            recovery_exit=recovered.returncode, recovery_stderr=recovered.stderr,
                            observation=observation, checks=checks))
    counts = dict(passed=sum(sum(r["checks"].values()) for r in results),
                  failed=sum(sum(not v for v in r["checks"].values()) for r in results), skipped=0)
    report = dict(**counts, duration_seconds=time.perf_counter()-started, scenarios=results,
                  source_manifest={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'core').glob('*.py'))},
                  scope="Five actual isolated child-process exits and independent-process SQLite recovery; 45 assertions, not 45 host/model tests", model_calls=0)
    path = evidence / f"capture-crashes-{time.time_ns()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({**counts, "duration_seconds": report["duration_seconds"], "evidence_ref": path.relative_to(ROOT).as_posix()}))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
