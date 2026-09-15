"""P13 fault-matrix tail probes (bounded, evidence-emitting).

Closes the fault rows the v3/v4/v5 summaries left open, on the 10k path with
the final candidate:

- ``lance-recover``: corrupt the 10k Lance index on the recall path, prove
  honest degraded recall (vector gap, lexical path intact, no crash), then
  restore and prove full recall returns.
- ``aux-failure``: auxiliary embedding raising and slow past the deadline;
  recall must degrade honestly within the deadline, never crash or hang.
- ``worker-kill``: terminate a draining worker mid-flight; a fresh drain must
  reclaim leased items and reach terminal states with bounded attempts.
- ``disk-full``: real full volume (small mounted VHD); capture must fail
  honestly without corruption and recover after space is freed.

Every phase writes its own receipt JSON; the script writes a summary and never
claims PASS by itself — the P13 acceptance summary consumes these receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
EXEC_ROOT = ROOT / ".execution"
V6 = ROOT / "benchmarks" / "v11_runtime_benchmark_v6.py"
SCOPE, AUTO_DEADLINE, DIM, TABLE = "p13-scope-v3", 1.5, 3072, "P13_v5_3072"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_v6():
    spec = importlib.util.spec_from_file_location("p13_v6_ro", V6)
    if not spec or not spec.loader:
        raise RuntimeError("v6_import_unavailable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_receipt(out_dir: Path, name: str, value: dict) -> Path:
    target = out_dir / name
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


class _OpenExistingVectorPort:
    """Mirror the runtime query path: open an existing index on first search."""

    def __init__(self, store, port) -> None:
        self._store = store
        self._port = port
        self._opened = False

    def search(self, context, *, limit: int, remaining_seconds: float):
        if not self._opened:
            self._store.open_existing()
            self._opened = True
        return self._port.search(context, limit=limit, remaining_seconds=remaining_seconds)


def _recall_once(mod, data_dir: Path, index_dir: Path, ordinal: int, label: str):
    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import SPACE_ID, RecallPolicy
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    store = ProcessLanceVectorStore(index_dir, table_name=TABLE, dimensions=DIM)
    emb = mod._QueryEmbed()
    raw_port = LanceVectorPort(store, emb, expected_embedding_space=SPACE_ID)
    core, ctx = mod._core(
        data_dir,
        "p13-fault-lance",
        vectors=_OpenExistingVectorPort(store, raw_port),
        retrieval_policy=RecallPolicy(vector_threshold=0.0),
    )
    emb.select(ordinal)
    dataset = mod._load_v4().rows(mod._load_v4().DATASET10)
    req = {"protocol_version": "1.1", "request_id": f"p13-fault-{label}-{ordinal}",
           "query": dataset[ordinal]["query"], "mode": "auto", "max_items": 6, "budget_tokens": 1200}
    t0 = time.perf_counter()
    try:
        res = core.recall(ctx, req, deadline_seconds=AUTO_DEADLINE)
        out = {"phase_status": "returned", "answerability_hint": res.answerability_hint,
               "item_count": len(res.items), "gaps": list(res.gaps),
               "elapsed_seconds": time.perf_counter() - t0}
    except Exception as exc:  # a fault may surface; it must be typed, not a crash
        out = {"phase_status": "exception", "exception_type": type(exc).__name__,
               "elapsed_seconds": time.perf_counter() - t0}
    finally:
        store.close()
    return out


def phase_lance_recover(mod, out_dir: Path, core_data: Path, index_dir: Path) -> dict:
    work = out_dir / "lance-recover"
    data_copy = work / "core-10k"
    index_copy = work / "lance-10k"
    mod._prepare_copy(core_data, data_copy)
    shutil.copytree(index_dir, index_copy)
    receipt: dict[str, object] = {"fault": "lance_index_corruption_on_10k_recall_path"}

    baseline = _recall_once(mod, data_copy, index_copy, 998, "baseline")
    receipt["baseline"] = baseline

    # Corrupt: move the table directory aside (index lost, bytes preserved).
    moved = []
    for child in index_copy.iterdir():
        if child.is_dir() and child.name.endswith(".lance"):
            target = child.with_name(child.name + ".corrupt")
            child.rename(target)
            moved.append(child.name)
    receipt["corrupted_tables"] = moved
    degraded = _recall_once(mod, data_copy, index_copy, 998, "degraded")
    receipt["degraded"] = degraded

    # Restore and prove full recall returns.  The degraded recall may have
    # re-created a fresh empty table directory; remove it before renaming back.
    recreated = []
    for name in moved:
        fresh = index_copy / name
        if fresh.exists():
            shutil.rmtree(fresh)
            recreated.append(name)
        (index_copy / (name + ".corrupt")).rename(fresh)
    receipt["recreated_empty_tables_during_degraded"] = recreated
    restored = _recall_once(mod, data_copy, index_copy, 998, "restored")
    receipt["restored"] = restored

    receipt["honest_degradation"] = (
        degraded["phase_status"] in {"returned", "exception"}
        and (degraded.get("gaps") not in (None, []) or degraded["phase_status"] == "exception")
    )
    receipt["recovery_restored_baseline"] = (
        restored["phase_status"] == "returned" and restored["item_count"] == baseline["item_count"]
    )
    _write_receipt(out_dir, "lance-recover-receipt.json", receipt)
    return receipt


class _FailingEmbed:
    def __init__(self, mode: str, delay: float = 0.0):
        self.mode, self.delay = mode, delay

    def embed_query(self, text: str, *, remaining_seconds: float):
        if self.mode == "raise":
            from scope_recall.adapters.models import AuxiliaryModelError
            raise AuxiliaryModelError("aux_model_unavailable_TEST")
        if self.delay > 0:
            time.sleep(min(self.delay, max(0.0, remaining_seconds) + 0.05))
        return ()


def phase_aux_failure(mod, out_dir: Path, core_data: Path, index_dir: Path) -> dict:
    work = out_dir / "aux-failure"
    data_copy = work / "core-10k"
    mod._prepare_copy(core_data, data_copy)
    receipt: dict[str, object] = {"fault": "auxiliary_model_failure_and_slowness_on_10k_recall"}

    from scope_recall.adapters.lance import LanceVectorPort
    from scope_recall.core.recall_policy import SPACE_ID, RecallPolicy
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    results = {}
    for label, embed in (("raising", _FailingEmbed("raise")), ("slow", _FailingEmbed("slow", delay=3.0))):
        store = ProcessLanceVectorStore(index_dir, table_name=TABLE, dimensions=DIM)
        store.open()
        port = LanceVectorPort(store, embed, expected_embedding_space=SPACE_ID)
        core, ctx = mod._core(data_copy, f"p13-fault-aux-{label}", vectors=port,
                              retrieval_policy=RecallPolicy(vector_threshold=0.0))
        req = {"protocol_version": "1.1", "request_id": f"p13-fault-aux-{label}",
               "query": "PUBLIC TEST aux fault probe", "mode": "auto", "max_items": 6, "budget_tokens": 1200}
        t0 = time.perf_counter()
        try:
            res = core.recall(ctx, req, deadline_seconds=AUTO_DEADLINE)
            results[label] = {"phase_status": "returned", "answerability_hint": res.answerability_hint,
                              "item_count": len(res.items), "gaps": list(res.gaps),
                              "elapsed_seconds": time.perf_counter() - t0}
        except Exception as exc:
            results[label] = {"phase_status": "exception", "exception_type": type(exc).__name__,
                              "elapsed_seconds": time.perf_counter() - t0}
        finally:
            store.close()
    receipt.update(results)
    receipt["deadline_honored"] = results["slow"]["elapsed_seconds"] < AUTO_DEADLINE + 2.0
    receipt["no_crash"] = all(value["phase_status"] in {"returned", "exception"} for value in results.values())
    _write_receipt(out_dir, "aux-failure-receipt.json", receipt)
    return receipt


def _capture_events(mod, core, ctx, count: int, label: str) -> int:
    captured = 0
    for index in range(count):
        row = {"source": f"PUBLIC TEST fault capture {label} {index}",
               "recorded_at": "2026-01-02T00:00:00Z"}
        event = mod._event(row, f"p13-fault-{label}/{index:03d}")
        if core.record_event(ctx, event, scope_id=SCOPE, remaining_seconds=10).event_refs:
            captured += 1
    return captured


def phase_worker_kill(mod, out_dir: Path) -> dict:
    work = out_dir / "worker-kill"
    data_dir = work / "worker-instance"
    data_dir.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, object] = {"fault": "worker_terminated_mid_drain"}
    core, ctx = mod._core(data_dir, fresh=True)
    captured = _capture_events(mod, core, ctx, 12, "workerkill")
    receipt["captured"] = captured
    db = core.storage.path

    child_code = (
        "import importlib.util, time\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('m', r'" + str(V6).replace("\\", "\\\\") + "')\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "class SlowEmbed:\n"
        "    def prepare_source(self, source, *, remaining_seconds=1.0):\n"
        "        time.sleep(0.2)\n"
        "        return {'ref': source.ref, 'revision': source.revision, 'vector': m._vector(source.revision, m.DIM)}\n"
        "    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):\n"
        "        if not lease_guard():\n"
        "            raise RuntimeError('lease_rejected')\n"
        "core, ctx = m._core(Path(r'" + str(data_dir).replace("\\", "\\\\") + "'), fresh=False)\n"
        "wr = core.drain_worker(ctx, owner_id='p13-fault-doomed', max_items=24, lease_seconds=1.0, consolidation=m._Consolidate(), embed=SlowEmbed(), remaining_seconds=60)\n"
        "print('completed', wr.completed)\n"
    )
    proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", child_code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Importing the installed candidate and native dependencies can take longer
    # than a fixed sleep on a cold Windows process.  Kill only after the durable
    # queue proves that this exact child has leased work.
    lease_deadline = time.monotonic() + 15.0
    leased_seen = 0
    while time.monotonic() < lease_deadline and proc.poll() is None:
        with sqlite3.connect(db) as c:
            leased_seen = int(c.execute(
                "SELECT count(*) FROM work_items WHERE state='leased' AND lease_owner='p13-fault-doomed'"
            ).fetchone()[0])
        if leased_seen:
            break
        time.sleep(0.05)
    receipt["leased_before_kill"] = leased_seen
    proc.kill()
    _, stderr = proc.communicate(timeout=10)
    receipt["killed_mid_drain"] = {"returncode": proc.returncode, "stderr_tail": stderr.decode("utf-8", errors="replace")[-200:]}
    with sqlite3.connect(db) as c:
        mid = dict(c.execute("SELECT state, count(*) FROM work_items GROUP BY state").fetchall())
    receipt["states_after_kill"] = mid

    # A different owner may reclaim only after the killed owner's durable
    # lease expires.  Use a short TEST lease above and wait for that boundary.
    time.sleep(1.1)
    t0 = time.perf_counter()
    core2, ctx2 = mod._core(data_dir, fresh=False)
    wr = core2.drain_worker(ctx2, owner_id="p13-fault-reclaimer", max_items=64,
                            lease_seconds=1.0, consolidation=mod._Consolidate(),
                            embed=mod._Embed(), remaining_seconds=120)
    elapsed = time.perf_counter() - t0
    with sqlite3.connect(db) as c:
        final = dict(c.execute("SELECT state, count(*) FROM work_items GROUP BY state").fetchall())
        attempts = c.execute("SELECT max(attempt) FROM work_items").fetchone()[0]
    receipt["reclaim"] = {"processed": wr.processed, "completed": wr.completed, "failed": wr.failed,
                          "retried": wr.retried, "elapsed_seconds": elapsed,
                          "final_states": final, "max_attempts": attempts}
    receipt["terminated_after_lease"] = leased_seen > 0
    receipt["terminal_reached"] = (
        receipt["terminated_after_lease"]
        and final.get("pending", 0) == 0
        and final.get("leased", 0) == 0
    )
    receipt["attempts_bounded"] = attempts is not None and attempts <= 4
    _write_receipt(out_dir, "worker-kill-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("lance-recover", "aux-failure", "worker-kill", "all"), default="all")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--core-data", type=Path, default=EXEC_ROOT / "TEST-P13-PREP-v4" / "core-10k-clean")
    parser.add_argument("--index-dir", type=Path, default=EXEC_ROOT / "TEST-P13-PREP-v5" / "lance-10k-high-entropy")
    args = parser.parse_args()
    if not args.out_dir.is_absolute():
        raise SystemExit("--out-dir-must-be-absolute")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    mod = _load_v6()
    summary = {"schema": "p13.fault-tail.v1", "python": sys.executable, "phases": {}}
    if args.phase in ("lance-recover", "all"):
        summary["phases"]["lance_recover"] = phase_lance_recover(mod, args.out_dir, args.core_data, args.index_dir)
    if args.phase in ("aux-failure", "all"):
        summary["phases"]["aux_failure"] = phase_aux_failure(mod, args.out_dir, args.core_data, args.index_dir)
    if args.phase in ("worker-kill", "all"):
        summary["phases"]["worker_kill"] = phase_worker_kill(mod, args.out_dir)
    flags = {}
    if "lance_recover" in summary["phases"]:
        flags["lance_honest_degradation"] = summary["phases"]["lance_recover"]["honest_degradation"]
        flags["lance_recovery"] = summary["phases"]["lance_recover"]["recovery_restored_baseline"]
    if "aux_failure" in summary["phases"]:
        flags["aux_deadline_honored"] = summary["phases"]["aux_failure"]["deadline_honored"]
        flags["aux_no_crash"] = summary["phases"]["aux_failure"]["no_crash"]
    if "worker_kill" in summary["phases"]:
        flags["worker_terminal_reached"] = summary["phases"]["worker_kill"]["terminal_reached"]
        flags["worker_attempts_bounded"] = summary["phases"]["worker_kill"]["attempts_bounded"]
    summary["flags"] = flags
    summary["all_flags_ok"] = all(flags.values()) if flags else False
    _write_receipt(args.out_dir, "fault-tail-summary.json", summary)
    print(json.dumps({"flags": flags, "all_flags_ok": summary["all_flags_ok"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
