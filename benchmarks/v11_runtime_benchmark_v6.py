"""P13 v6 bounded throughput/resource/concurrency TEST measurements.

Does not claim full P13 PASS. Requires installed candidate receipt and isolated
TEST-FINAL runtime (-I -B) without worktree scope_recall source aliases.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable

SCRIPT, ROOT = Path(__file__).resolve(), Path(__file__).resolve().parents[1]
EXEC_ROOT = ROOT / ".execution"
DEFAULT_CORE = EXEC_ROOT / "TEST-P13-PREP-v4" / "core-10k-clean"
DEFAULT_INDEX = EXEC_ROOT / "TEST-P13-PREP-v5" / "lance-10k-high-entropy"
DEFAULT_RECEIPT = EXEC_ROOT / "TEST-CANDIDATE-a2c2ec4" / "installed-codex-receipt.json"
RUNTIME_PY = EXEC_ROOT / "TEST-FINAL-RUNTIME-ENV" / "Scripts" / "python.exe"
SCOPE, AUTO_DEADLINE, DIM, TABLE = "p13-scope-v3", 1.5, 3072, "P13_v5_3072"
CAPTURE_N, CAPTURE_INDEXES, COLD_ORDINALS = 20, tuple((i * 503) % 10_000 for i in range(20)), (998, 4491, 9481)
FIXTURE_COUNTS = {"source_events": 10040, "claims": 10000, "claim_versions": 10000, "work_items": 20080}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as s:
        for chunk in iter(lambda: s.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _quantiles(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2], ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _site_packages_root() -> Path:
    if os.name == "nt":
        return Path(sys.prefix) / "Lib" / "site-packages" / "scope_recall"
    return Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "scope_recall"


def _validate_out_dir(out_dir: Path, core_source: Path, index_dir: Path) -> None:
    if not out_dir.is_absolute():
        raise RuntimeError("out_dir_must_be_absolute")
    if out_dir.exists():
        raise RuntimeError("out_dir_must_be_fresh")
    resolved = out_dir.resolve()
    lowered = str(resolved).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise RuntimeError("forbidden_agents_root")
    for path in (core_source, index_dir, DEFAULT_CORE, DEFAULT_INDEX):
        target = path.resolve()
        if resolved == target or resolved in target.parents or target in resolved.parents:
            raise RuntimeError("fixture_or_index_path_collision")
    if EXEC_ROOT.resolve() not in resolved.parents and not resolved.name.startswith("TEST-"):
        raise RuntimeError("out_dir_must_be_under_execution_or_test_named")


def _verify_installed(receipt_path: Path) -> dict[str, Any]:
    if not receipt_path.is_absolute() or not receipt_path.is_file():
        raise RuntimeError("installed_receipt_missing_or_not_absolute")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("mismatches") not in (None, []):
        raise RuntimeError("installed_mismatches")
    expected = _site_packages_root().resolve()
    import scope_recall

    actual = Path(scope_recall.__file__).resolve().parent
    if actual != expected:
        raise RuntimeError(f"import_path_mismatch:{actual}!={expected}")
    receipt_python = Path(str(receipt["python"])).resolve()
    if Path(sys.executable).resolve() != receipt_python:
        raise RuntimeError(f"python_executable_mismatch:{sys.executable}!={receipt_python}")
    if Path(str(receipt["module_root"])).resolve() != expected:
        raise RuntimeError("receipt_module_root_mismatch")
    if any(Path(p).resolve() == ROOT.resolve() for p in sys.path if p):
        raise RuntimeError("worktree_source_alias_on_syspath")
    manifest = receipt.get("package_files")
    if not manifest:
        build_path = receipt_path.parent / "build-receipt.json"
        if not build_path.is_file():
            raise RuntimeError("package_files_missing_from_receipt")
        build = json.loads(build_path.read_text(encoding="utf-8"))
        if build.get("wheel_sha256") != receipt.get("wheel_sha256"):
            raise RuntimeError("build_wheel_mismatch")
        manifest = build["package_files"]
    for item in manifest:
        target = (expected / item["path"]).resolve()
        if not target.is_file() or _sha256(target) != item["sha256"]:
            raise RuntimeError(f"hash_mismatch:{item['path']}")
    from scope_recall.contracts import InstanceBinding
    from scope_recall.core import CoreConfig

    auto_recall = CoreConfig(InstanceBinding("probe", "probe", receipt_path.parent, frozenset({SCOPE}), True)).auto_recall_seconds
    return {"receipt_path": str(receipt_path), "receipt_sha256": _sha256(receipt_path), "module_root": str(expected),
            "wheel_sha256": receipt.get("wheel_sha256"), "source_commit": receipt.get("source_commit"),
            "version": receipt.get("version"), "package_files_checked": len(manifest),
            "core_auto_recall_seconds": auto_recall, "python_executable": str(receipt_python)}


def _load_v4():
    spec = importlib.util.spec_from_file_location("p13_v4_ro", ROOT / "benchmarks" / "v11_runtime_benchmark_v4.py")
    if not spec or not spec.loader:
        raise RuntimeError("v4_import_unavailable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sqlite_counts(data_dir: Path) -> dict[str, int]:
    with sqlite3.connect(data_dir / "memory.sqlite3") as c:
        return {name: int(c.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
                for name in ("source_events", "claims", "claim_versions", "lexical_projection", "work_items")}


def _binding(data_dir: Path, *, fresh: bool = False):
    from scope_recall.contracts import InstanceBinding
    if fresh:
        token = hashlib.sha256(os.path.normcase(str(data_dir.resolve())).encode()).hexdigest()[:16]
        return InstanceBinding("p13-benchmark-v3", f"p13-v6-{token}", data_dir.resolve(), frozenset({SCOPE}), True)
    with sqlite3.connect(data_dir / "memory.sqlite3") as c:
        row = c.execute("SELECT agent_id, installation_id, test_mode FROM instance_meta WHERE singleton=1").fetchone()
        scopes = frozenset(r[0] for r in c.execute("SELECT scope_id FROM instance_scopes"))
    return InstanceBinding(row[0], row[1], data_dir.resolve(), scopes, bool(row[2]))


def _core(data_dir: Path, session: str = "p13-v6", *, fresh: bool = False, vectors=None, retrieval_policy=None):
    from scope_recall.contracts import TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    binding = _binding(data_dir, fresh=fresh)
    ctx = TrustedContext(binding, session, binding.scope_ids, "human_direct", project_id="P13", branch_id="main")
    core = MemoryCore(CoreConfig(binding), vectors=vectors, retrieval_policy=retrieval_policy)
    core.initialize()
    return core, ctx


def _vector(seed: int, n: int) -> tuple[float, ...]:
    state = (0x9E3779B9 ^ ((seed + 1) * 0x85EBCA6B)) & 0xFFFFFFFF
    out: list[float] = []
    for i in range(n):
        state = (1664525 * state + 1013904223 + i * 0x27D4EB2D) & 0xFFFFFFFF
        out.append(state / 4294967296.0)
    return tuple(out)


def _event(row: dict[str, Any], key: str, content: str | None = None) -> dict[str, Any]:
    return {"protocol_version": "1.1", "source_event_key": key, "source_revision": 1, "origin": "human_direct",
            "role": "user", "content": content or row["source"], "occurred_at": row["recorded_at"],
            "recorded_at": row["recorded_at"], "time_precision": "instant", "capture_state": "complete",
            "evidence_refs": [], "dataset_id": "P13-V6-TEST-ONLY"}


def _backup_db(source: Path, dest: Path) -> None:
    if dest.exists():
        raise RuntimeError("backup_exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_c, dst_c = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True), sqlite3.connect(dest)
    try:
        src_c.backup(dst_c)
        dst_c.commit()
        if str(dst_c.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise RuntimeError("backup_quick_check_failed")
    finally:
        src_c.close()
        dst_c.close()


def _pin_dir(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "memory.sqlite3") as c:
        c.execute("UPDATE instance_meta SET data_directory=? WHERE singleton=1", (os.path.normcase(str(data_dir.resolve())),))
        c.commit()


def _prepare_copy(core_source: Path, target: Path) -> None:
    _backup_db(core_source / "memory.sqlite3", target / "memory.sqlite3")
    for name in ("config.json", "writer.lock"):
        src = core_source / name
        if src.is_file():
            (target / name).write_bytes(src.read_bytes())
    _pin_dir(target)


def _pending(db: Path) -> int:
    with sqlite3.connect(db) as c:
        return int(c.execute("SELECT count(*) FROM work_items WHERE state='pending'").fetchone()[0])


class _Sampler:
    def __init__(self) -> None:
        import psutil
        self.p = psutil.Process()
        self.stop, self.thread = threading.Event(), None
        self.peak = {"peak_rss_bytes": 0, "peak_threads": 0, "peak_owned_children": 0}

    def _tree(self) -> tuple[int, int, int]:
        rss, threads = int(self.p.memory_info().rss), int(self.p.num_threads())
        for ch in self.p.children(recursive=True):
            try:
                rss += int(ch.memory_info().rss)
                threads += int(ch.num_threads())
            except OSError:
                pass
        return rss, threads, len(self.p.children(recursive=True))

    def start(self) -> None:
        def loop() -> None:
            while not self.stop.wait(0.01):
                try:
                    rss, th, owned = self._tree()
                    self.peak["peak_rss_bytes"] = max(self.peak["peak_rss_bytes"], rss)
                    self.peak["peak_threads"] = max(self.peak["peak_threads"], th)
                    self.peak["peak_owned_children"] = max(self.peak["peak_owned_children"], owned)
                except Exception:
                    pass
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self) -> dict[str, int]:
        self.stop.set()
        if self.thread:
            self.thread.join(1)
        return self.peak

    def snap(self) -> dict[str, int]:
        rss, th, owned = self._tree()
        return {"rss_bytes": rss, "threads": th, "owned_children": owned}


class _Embed:
    def prepare_source(self, source, *, remaining_seconds=1.0):
        return {"ref": source.ref, "revision": source.revision, "vector": _vector(source.revision, DIM)}

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
        if not lease_guard():
            raise RuntimeError("lease_rejected")


class _Consolidate:
    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0):
        return json.dumps({"protocol_version": "1.1", "source_refs": [f"{s.ref}@{s.revision}" for s in sources],
                           "claim_proposals": [], "resume_proposals": [], "reference_proposals": []}, ensure_ascii=False)


class _QueryEmbed:
    def __init__(self) -> None:
        self.ordinal = 0

    def select(self, ordinal: int) -> None:
        self.ordinal = ordinal

    def embed_query(self, text: str, *, remaining_seconds: float):
        return () if remaining_seconds <= 0 else _vector(self.ordinal, DIM)


def _capture_phase(core, ctx, dataset, indexes, label) -> dict[str, Any]:
    samples = []
    for index in indexes:
        t0 = time.perf_counter()
        try:
            rcpt = core.record_event(ctx, _event(dataset[index], f"p13-v6-{label}/{index:06d}",
                                                   dataset[index]["source"] + f" {label} {index}"), scope_id=SCOPE, remaining_seconds=10)
            samples.append({"index": index, "ok": bool(rcpt.event_refs), "elapsed_seconds": time.perf_counter() - t0,
                            "refs": [r.ref for r in rcpt.event_refs], "disposition": rcpt.disposition, "scope_id": SCOPE})
        except Exception as exc:
            samples.append({"index": index, "ok": False, "elapsed_seconds": time.perf_counter() - t0, "error": type(exc).__name__, "refs": []})
    all_elapsed = [s["elapsed_seconds"] for s in samples]
    ok_elapsed = [s["elapsed_seconds"] for s in samples if s["ok"]]
    p50, p95 = _quantiles(all_elapsed)
    ok_p50, ok_p95 = _quantiles(ok_elapsed)
    return {"sample_count": len(samples), "successful_count": len(ok_elapsed), "failure_count": len(samples) - len(ok_elapsed),
            "failures": [s for s in samples if not s["ok"]], "p50_seconds": p50, "p95_seconds": p95,
            "successful_p50_seconds": ok_p50, "successful_p95_seconds": ok_p95, "samples": samples}


def _worker_phase(out_dir: Path, dataset) -> dict[str, Any]:
    data = out_dir / "worker-instance"
    data.mkdir(parents=True, exist_ok=True)
    core, ctx = _core(data, fresh=True)

    def work_ids() -> set[int]:
        with sqlite3.connect(core.storage.path) as connection:
            return {int(row[0]) for row in connection.execute("SELECT work_id FROM work_items")}

    def work_snapshot(work_ids: set[int]) -> dict[str, int]:
        with sqlite3.connect(core.storage.path) as connection:
            if work_ids:
                placeholders = ",".join("?" for _ in work_ids)
                rows = connection.execute(
                    f"SELECT work_id,state FROM work_items WHERE work_id IN ({placeholders})",
                    tuple(sorted(work_ids)),
                ).fetchall()
            else:
                rows = []
        states = {"pending": 0, "leased": 0, "done": 0, "failed": 0, "obsolete": 0}
        for _, state in rows:
            states[str(state)] = states.get(str(state), 0) + 1
        states["missing"] = len(work_ids) - len(rows)
        return states

    base = _pending(core.storage.path)
    before_ids = work_ids()
    captured = 0
    for i in range(CAPTURE_N):
        if core.record_event(ctx, _event(dataset[i], f"p13-v6-worker/{i:03d}"), scope_id=SCOPE, remaining_seconds=10).event_refs:
            captured += 1
    pending = _pending(core.storage.path) - base
    after_ids = work_ids()
    batch_ids = after_ids - before_ids
    t0 = time.perf_counter()
    wr = core.drain_worker(ctx, owner_id="p13-v6-worker", max_items=max(1, pending), consolidation=_Consolidate(),
                           embed=_Embed(), remaining_seconds=30)
    elapsed = time.perf_counter() - t0
    final_states = work_snapshot(batch_ids)
    durable_done = final_states.get("done", 0)
    return {"captured": captured, "pending_before_drain": pending, "processed": wr.processed, "completed": wr.completed,
            "failed": wr.failed, "retried": wr.retried, "skipped": wr.skipped, "elapsed_seconds": elapsed,
            "throughput_items_per_second": (wr.processed / elapsed) if elapsed else None,
            "semantic_quality": "not measured; deterministic TEST auxiliary ports only",
            "worker_states": [item.state for item in wr.items], "initial_work_count": len(batch_ids),
            "final_states": final_states, "durably_completed_count": durable_done,
            "all_completed": len(batch_ids) == pending and durable_done == len(batch_ids) and final_states["missing"] == 0}


def _recall_runtime(data_dir: Path, index_dir: Path):
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import SPACE_ID, RecallPolicy
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    store = ProcessLanceVectorStore(index_dir, table_name=TABLE, dimensions=DIM)
    store.open()
    emb = _QueryEmbed()
    port = LanceVectorPort(store, emb, expected_embedding_space=SPACE_ID)
    core, ctx = _core(data_dir, "p13-v6-cold", vectors=port, retrieval_policy=RecallPolicy(vector_threshold=0.0))
    return store, emb, core, ctx


def cold_child(index_dir: Path, data_dir: Path, ordinal: int, dataset) -> dict[str, Any]:
    store, emb, core, ctx = _recall_runtime(data_dir, index_dir)
    emb.select(ordinal)
    t0 = time.perf_counter()
    req = {"protocol_version": "1.1", "request_id": f"p13-v6-cold-{ordinal}", "query": dataset[ordinal]["query"],
           "mode": "auto", "max_items": 6, "budget_tokens": 1200}
    try:
        res = core.recall(ctx, req, deadline_seconds=AUTO_DEADLINE)
        out = {"ordinal": ordinal, "elapsed_seconds": time.perf_counter() - t0, "status": res.answerability_hint,
               "item_count": len(res.items), "items": [{"ref": i.ref, "revision": i.revision} for i in res.items],
               "gaps": list(res.gaps), "deadline_seconds": AUTO_DEADLINE}
    except Exception as exc:
        out = {"ordinal": ordinal, "elapsed_seconds": time.perf_counter() - t0, "status": "error", "item_count": 0,
               "items": [], "gaps": [type(exc).__name__], "deadline_seconds": AUTO_DEADLINE}
    store.close()
    return out


def _child_cmd(receipt_path: Path, index_dir: Path, data_dir: Path, ordinal: int) -> list[str]:
    return [sys.executable, "-I", "-B", str(SCRIPT), "--phase", "cold-child", "--installed-receipt", str(receipt_path),
            "--index-dir", str(index_dir), "--data-dir", str(data_dir), "--ordinal", str(ordinal)]


def _run_owned_child(cmd: list[str], *, run_timeout: float = 45.0, cleanup_timeout: float = 4.0) -> dict[str, Any]:
    import psutil
    proc = subprocess.Popen(cmd, cwd=os.environ.get("TEMP") or str(ROOT.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    owned = psutil.Process(proc.pid)
    timed_out = False
    try:
        stdout_b, stderr_b = proc.communicate(timeout=run_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = proc.communicate(timeout=cleanup_timeout)
    c0 = time.perf_counter()
    cleanup_ok = True
    while time.perf_counter() - c0 < cleanup_timeout:
        try:
            if not owned.is_running() and not owned.children(recursive=True):
                break
            alive = [p for p in owned.children(recursive=True) if p.is_running()]
            if not owned.is_running() and not alive:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    else:
        cleanup_ok = False
    return {"exit_code": proc.returncode, "timed_out": timed_out, "stdout": stdout_b.decode("utf-8"),
            "stderr": stderr_b.decode("utf-8"), "cleanup_seconds": time.perf_counter() - c0, "cleanup_bounded_ok": cleanup_ok}


def _cold_phase(receipt_path: Path, index_dir: Path, query_dir: Path, dataset) -> dict[str, Any]:
    sampler, samples = _Sampler(), []
    sampler.start()
    for ordinal in COLD_ORDINALS:
        t0 = time.perf_counter()
        run = _run_owned_child(_child_cmd(receipt_path, index_dir, query_dir, ordinal))
        payload: dict[str, Any] = {"ordinal": ordinal, "parent_elapsed_seconds": time.perf_counter() - t0,
                                   "exit_code": run["exit_code"], "timed_out": run["timed_out"],
                                   "cleanup_seconds": run["cleanup_seconds"], "cleanup_bounded_ok": run["cleanup_bounded_ok"]}
        if run["timed_out"]:
            payload.update({"status": "child_timeout", "gaps": ["TimeoutExpired"], "stderr_tail": run["stderr"][-500:]})
        else:
            try:
                payload.update(json.loads(run["stdout"].strip().splitlines()[-1]))
            except (ValueError, IndexError, json.JSONDecodeError):
                payload.update({"status": "child_output_invalid", "gaps": ["invalid_stdout"],
                                "stderr_tail": run["stderr"][-500:], "stdout_tail": run["stdout"][-500:]})
        if run["exit_code"] != 0 and "status" not in payload:
            payload["status"] = "child_exit_error"
        samples.append(payload)
    return {"ordinals": list(COLD_ORDINALS), "samples": samples, "resources": sampler.finish(), "deadline_seconds": AUTO_DEADLINE}


def _source_check(core, ctx, ref: str) -> dict[str, Any]:
    stored = core.source(ctx, ref, 1)
    return {"ref": ref, "readable": stored is not None,
            "scope_id": stored.scope_id if stored else None, "session_id": stored.session_id if stored else None,
            "scope_ok": bool(stored and stored.scope_id == SCOPE), "session_ok": bool(stored and stored.session_id == ctx.session_id)}


def _concurrency_phase(mutation_dir: Path, dataset) -> dict[str, Any]:
    from scope_recall.contracts import TrustedContext
    before, sampler = _Sampler().snap(), _Sampler()
    sampler.start()
    core, _ = _core(mutation_dir)
    binding = core.config.binding
    contexts = [TrustedContext(binding, f"p13-v6-conc-{s}", binding.scope_ids, "human_direct", project_id="P13", branch_id="main") for s in ("a", "b")]

    def one(round_idx: int, slot: int) -> dict[str, Any]:
        idx = 9000 + round_idx * 2 + slot
        row, ctx = dataset[idx % len(dataset)], contexts[slot]
        t0 = time.perf_counter()
        rcpt = core.record_event(ctx, _event(row, f"p13-v6-conc/r{round_idx}-s{slot}", f"P13 v6 concurrent r{round_idx} s{slot}"),
                                 scope_id=SCOPE, remaining_seconds=10)
        refs = [r.ref for r in rcpt.event_refs]
        checks = [_source_check(core, ctx, ref) for ref in refs]
        return {"round": round_idx, "slot": slot, "session_id": ctx.session_id, "scope_id": SCOPE,
                "elapsed_seconds": time.perf_counter() - t0, "refs": refs, "disposition": rcpt.disposition,
                "persisted": checks, "persisted_ok": bool(refs) and all(c["scope_ok"] and c["session_ok"] for c in checks)}

    rounds = []
    for r in range(2):
        with ThreadPoolExecutor(2) as pool:
            rounds.append(list(pool.map(lambda slot: one(r, slot), (0, 1))))
    mut_ctx = TrustedContext(binding, "p13-v6-mut-sentinel", binding.scope_ids, "human_direct", project_id="P13", branch_id="main")
    mut_ref = core.record_event(mut_ctx, _event(dataset[17], "p13-v6-mut/sentinel", "P13 v6 mutation sentinel."), scope_id=SCOPE, remaining_seconds=10).event_refs[0].ref
    iso_dir = mutation_dir.parent / "isolation-instance"
    iso_dir.mkdir(parents=True, exist_ok=True)
    iso_core, iso_ctx = _core(iso_dir, "p13-v6-iso", fresh=True)
    iso_ref = iso_core.record_event(iso_ctx, _event(dataset[42], "p13-v6-iso/sentinel", "P13 v6 isolation sentinel."), scope_id=SCOPE, remaining_seconds=10).event_refs[0].ref
    cross = {"mutation_to_isolation": iso_core.source(iso_ctx, mut_ref, 1), "isolation_to_mutation": core.source(mut_ctx, iso_ref, 1)}
    isolated = cross["mutation_to_isolation"] is None and cross["isolation_to_mutation"] is None
    return {"rounds": rounds, "isolation": {"mutation_ref": mut_ref, "isolation_ref": iso_ref,
            "mutation_readable_from_isolation": cross["mutation_to_isolation"] is not None,
            "isolation_readable_from_mutation": cross["isolation_to_mutation"] is not None, "isolated": isolated},
            "resources": {"before": before, "peak": sampler.finish(), "after": _Sampler().snap()}}


def _phase(name: str, fn: Callable[[], dict[str, Any]], phases: dict[str, Any], errors: list[str]) -> None:
    try:
        phases[name] = fn()
    except Exception as exc:
        phases[name] = {"status": "error", "error": type(exc).__name__, "message": str(exc)}
        errors.append(f"{name}:{type(exc).__name__}")


def _collect_errors(phases: dict[str, Any], installed: dict[str, Any], fixture_ok: bool) -> list[str]:
    errors: list[str] = []
    if installed["core_auto_recall_seconds"] != AUTO_DEADLINE:
        errors.append("core_auto_recall_seconds_not_1.5")
    if not fixture_ok:
        errors.append("fixture_integrity_failed")
    p1 = phases.get("capture", {})
    if isinstance(p1, dict) and p1.get("failure_count"):
        errors.append(f"phase1_failures:{p1['failure_count']}")
    p2 = phases.get("worker_drain", {})
    if isinstance(p2, dict) and p2.get("status") != "error":
        if p2.get("failed") or p2.get("retried"):
            errors.append(f"phase2_worker_failed:{p2.get('failed')}_retried:{p2.get('retried')}")
        if not p2.get("all_completed"):
            errors.append("phase2_worker_not_all_completed")
    p3 = phases.get("cold_recall", {})
    for sample in p3.get("samples", []) if isinstance(p3, dict) else []:
        if sample.get("timed_out"):
            errors.append(f"phase3_cold_timeout:{sample.get('ordinal')}")
        if sample.get("status") in {"child_output_invalid", "child_timeout", "child_exit_error", "error"}:
            errors.append(f"phase3_cold_invalid:{sample.get('ordinal')}:{sample.get('status')}")
        if not sample.get("cleanup_bounded_ok", True):
            errors.append(f"phase3_cleanup_exceeded_4s:{sample.get('ordinal')}")
    p4 = phases.get("concurrency", {})
    if isinstance(p4, dict) and p4.get("status") != "error":
        if not p4.get("isolation", {}).get("isolated"):
            errors.append("phase4_isolation_leak")
        for rnd in p4.get("rounds", []):
            for sample in rnd:
                if not sample.get("persisted_ok"):
                    errors.append(f"phase4_persist_failed:r{sample.get('round')}:s{sample.get('slot')}")
    return errors


def run_benchmark(out_dir: Path, core_source: Path, index_dir: Path, receipt_path: Path) -> dict[str, Any]:
    _validate_out_dir(out_dir, core_source, index_dir)
    installed = _verify_installed(receipt_path)
    out_dir.mkdir(parents=True, exist_ok=False)
    v4 = _load_v4()
    dataset = v4.rows(v4.DATASET10)
    baseline_counts, baseline_sha = _sqlite_counts(core_source), _sha256(core_source / "memory.sqlite3")
    if {k: baseline_counts[k] for k in FIXTURE_COUNTS} != FIXTURE_COUNTS:
        raise RuntimeError(f"fixture_count_mismatch:{baseline_counts}")
    mutation_dir, query_dir = out_dir / "core-mutation-copy", out_dir / "core-query-copy"
    _prepare_copy(core_source, mutation_dir)
    _prepare_copy(core_source, query_dir)
    phases: dict[str, Any] = {}
    errors: list[str] = []
    _phase("capture", lambda: _capture_phase(*_core(mutation_dir), dataset, CAPTURE_INDEXES, "capture"), phases, errors)
    _phase("worker_drain", lambda: _worker_phase(out_dir, dataset), phases, errors)
    _phase("cold_recall", lambda: _cold_phase(receipt_path, index_dir, query_dir, dataset), phases, errors)
    _phase("concurrency", lambda: _concurrency_phase(mutation_dir, dataset), phases, errors)
    fixture_ok = _sqlite_counts(core_source) == baseline_counts and _sha256(core_source / "memory.sqlite3") == baseline_sha
    errors.extend(_collect_errors(phases, installed, fixture_ok))
    summary = {"schema": "p13.measurement.v6", "status": "completed_with_errors" if errors else "completed_measurements",
               "tests_passed": False, "p13_pass_claimed": False,
               "boundary": "bounded TEST throughput/resource/concurrency only; no semantic proof or full P13 acceptance",
               "script": {"path": str(SCRIPT), "sha256": _sha256(SCRIPT)}, "candidate": installed, "deadline_seconds": AUTO_DEADLINE,
               "inputs": {"core_source": str(core_source.resolve()), "index_dir": str(index_dir.resolve())},
               "fixture_baseline": {"sqlite_counts": baseline_counts, "memory_sqlite3_sha256": baseline_sha, "expected": FIXTURE_COUNTS},
               "phases": phases,
               "assertions": [{"id": "installed_identity", "ok": installed["core_auto_recall_seconds"] == AUTO_DEADLINE, "detail": installed},
                              {"id": "fixture_integrity", "ok": fixture_ok, "baseline_counts": baseline_counts}],
               "errors": errors, "network": "disabled_by_contract", "semantic_quality": "not measured"}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="P13 v6 bounded TEST benchmark helper")
    p.add_argument("--phase", choices=("run", "cold-child", "preflight"), default="run")
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--installed-receipt", type=Path)
    p.add_argument("--core-data", type=Path, default=DEFAULT_CORE)
    p.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    p.add_argument("--ordinal", type=int, default=998)
    p.add_argument("--data-dir", type=Path)
    a = p.parse_args()
    if a.phase == "preflight":
        if a.installed_receipt is None:
            raise SystemExit("--installed-receipt-required-for-preflight")
        print(json.dumps(_verify_installed(a.installed_receipt), ensure_ascii=False))
        return 0
    if a.phase == "cold-child":
        if a.data_dir is None or a.installed_receipt is None:
            raise SystemExit("--data-dir-and-installed-receipt-required-for-cold-child")
        _verify_installed(a.installed_receipt)
        v4 = _load_v4()
        print(json.dumps(cold_child(a.index_dir, a.data_dir, a.ordinal, v4.rows(v4.DATASET10)), ensure_ascii=False))
        return 0
    if a.out_dir is None or a.installed_receipt is None:
        raise SystemExit("--out-dir-and-installed-receipt-required")
    if not a.out_dir.is_absolute() or not a.installed_receipt.is_absolute():
        raise SystemExit("--out-dir-and-installed-receipt-must-be-absolute")
    print(json.dumps(run_benchmark(a.out_dir, a.core_data, a.index_dir, a.installed_receipt), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
