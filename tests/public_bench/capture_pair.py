"""Alternate two isolated builds on identical synthetic capture and hook workloads.

Persistent children execute serial requests in AB/BA order on one or four fixed
physical cores. Initialization and warmup are excluded; OS caches are unchanged.
"""

import argparse
import ctypes
import hashlib
import inspect
import json
import math
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def distribution(samples):
    """Retain nearest-rank timing summaries and raw measurements."""
    ordered = sorted(samples)
    return dict(
        p50_ms=ordered[math.ceil(len(ordered) * 0.5) - 1],
        p95_ms=ordered[math.ceil(len(ordered) * 0.95) - 1],
        max_ms=max(ordered),
    )


def bind(cores):
    """Bind this benchmark child only, without touching other processes or settings."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.GetProcessAffinityMask.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        )
        process = kernel.GetCurrentProcess()
        if not kernel.SetProcessAffinityMask(process, sum(1 << core for core in cores)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetPriorityClass(process, 0x8000):
            raise ctypes.WinError(ctypes.get_last_error())
        actual, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(process, ctypes.byref(actual), ctypes.byref(system)):
            raise ctypes.WinError(ctypes.get_last_error())
        return dict(mask=actual.value, cores=cores, priority="ABOVE_NORMAL")
    os.sched_setaffinity(0, cores)
    return dict(cores=sorted(os.sched_getaffinity(0)))


def worker(root):
    """Serve serial, content-free measurement replies from fresh synthetic stores."""
    import capture_timing
    from cache import RequestCache
    from lab import Lab
    from performance import NOW, seed_truth
    from ports import Ports
    from scenarios import ScenarioStore
    from scope_recall.core import capture
    from scope_recall.core.schema import SCHEMA_VERSION
    from v11_support import source_event

    root.mkdir(parents=True, exist_ok=False)
    bind([0])
    lab = Lab(
        root / "capture",
        Ports(RequestCache(root / "cache.sqlite3")),
        now=NOW,
        scopes=frozenset({"public", *(f"public-{i}" for i in range(1, 8))}),
    )
    seed_truth(lab, 1000)
    scenes = ScenarioStore(root / "hooks")
    capture_timing.install()
    package = Path(inspect.getfile(capture)).resolve().parents[1]
    digest = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode())
        digest.update(b"\0" + source.read_bytes() + b"\0")
    print(
        json.dumps(
            dict(ready=True, module=inspect.getfile(capture), schema=SCHEMA_VERSION, source_sha256=digest.hexdigest())
        ),
        flush=True,
    )
    for index, line in enumerate(sys.stdin):
        command = json.loads(line)
        if command.get("quit"):
            break
        affinity = bind(command["cores"])
        trace = capture_timing.CaptureTiming() if command.get("profile") else None
        capture_timing.ACTIVE = trace
        start = time.perf_counter()
        if command["operation"] == "capture":
            receipt = lab.core.record_event(
                lab.context,
                source_event(
                    source_event_key=f"TEST-pair-{index}", content=f"TEST-pair{index} copper color preference"
                ),
                scope_id="public",
                remaining_seconds=10,
            )
            assert receipt.durability == "persisted" and receipt.lexical_state == "ready"
        else:
            session = f"TEST-pair-session-{index}"
            prompt = f"TEST-pair{index} copper color preference"
            scenes.hook("codex", prompt, session=session)
        elapsed = (time.perf_counter() - start) * 1000
        capture_timing.ACTIVE = None
        captured, diagnostics = True, None
        if command["operation"] == "hook":
            stored = scenes.rows("content=? AND role='user' AND entry_id='codex'", (prompt,))
            captured = len(stored) == 1 and stored[0]["content"] == prompt
            if not captured:
                diagnostics = {
                    name: getattr(scenes.hooks["codex"].diagnostics, name, None)
                    for name in ("capture_error_code", "capture_durability", "capture_disposition", "capture_stage")
                }
        print(
            json.dumps(
                dict(
                    elapsed_ms=elapsed,
                    components=trace.result() if trace else None,
                    affinity=affinity,
                    captured=captured,
                    diagnostics=diagnostics,
                )
            ),
            flush=True,
        )


def launch(args, label):
    """Select one installed package or archived source without editing either."""
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    if label == "before":
        env["PYTHONPATH"] = str(args.before)
    else:
        env.pop("PYTHONPATH", None)
    error = (args.output / (label + ".stderr.log")).open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-B", __file__, "--worker", str(args.data_directory / label)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=error,
        text=True,
        encoding="utf-8",
        env=env,
    )
    line = child.stdout.readline()
    if not line:
        child.wait(timeout=30)
        error.close()
        raise RuntimeError(f"measurement child failed; inspect {label}.stderr.log")
    ready = json.loads(line)
    return child, error, ready


def ask(child, command):
    child.stdin.write(json.dumps(command) + "\n")
    child.stdin.flush()
    line = child.stdout.readline()
    if not line:
        raise RuntimeError("measurement child ended without a receipt")
    return json.loads(line)


def measure(args, children):
    """Alternate order per sample, retaining each pair rather than comparing unrelated tails."""
    rows = []
    for cores in ([0], [0, 2, 4, 6]):
        for operation in ("capture", "hook"):
            for index in range(8 + args.samples + args.profile_samples):
                profile = index >= 8 + args.samples
                order = ("before", "after") if index % 2 == 0 else ("after", "before")
                pair = dict(operation=operation, cores=cores, index=index, profile=profile, order=order)
                for label in order:
                    pair[label] = ask(children[label], dict(operation=operation, cores=cores, profile=profile))
                if index >= 8:
                    rows.append(pair)
    summaries = []
    for cores in (1, 4):
        for operation in ("capture", "hook"):
            selected = [
                r for r in rows if len(r["cores"]) == cores and r["operation"] == operation and not r["profile"]
            ]
            summaries.append(
                dict(
                    cores=cores,
                    operation=operation,
                    before=distribution([r["before"]["elapsed_ms"] for r in selected]),
                    after=distribution([r["after"]["elapsed_ms"] for r in selected]),
                    paired_delta=distribution([r["after"]["elapsed_ms"] - r["before"]["elapsed_ms"] for r in selected]),
                )
            )
    return dict(samples=rows, summaries=summaries)


def main():
    """Close every owned measurement child even when a run fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--data-directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--profile-samples", type=int, default=20)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    args.output.mkdir(parents=True, exist_ok=False)
    children, errors, builds = {}, [], {}
    try:
        for label in ("before", "after"):
            children[label], error, builds[label] = launch(args, label)
            errors.append(error)
        result = measure(args, children)
        result.update(
            builds=builds,
            workload=dict(sources=1000, claims=100, embeddings=0),
            rotation="sample index modulo two; serial AB/BA",
            model_calls=False,
            capture_failures=[
                dict(
                    operation=row["operation"],
                    cores=row["cores"],
                    index=row["index"],
                    profile=row["profile"],
                    build=label,
                    diagnostics=row[label]["diagnostics"],
                )
                for row in result["samples"]
                for label in ("before", "after")
                if not row[label]["captured"]
            ],
        )
        (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if result["capture_failures"]:
            raise RuntimeError("Hook capture failures retained in results.json")
    finally:
        for child in children.values():
            if child.poll() is None:
                with suppress(OSError):
                    child.stdin.write('{"quit":true}\n')
                    child.stdin.flush()
            with suppress(OSError):
                child.stdin.close()
        for child in children.values():
            child.wait(timeout=30)
        for error in errors:
            error.close()


if __name__ == "__main__":
    main()
