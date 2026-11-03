"""Paired synthetic recall measurements and semantic comparisons on one fixture.

The before worker replaces only the measured modules from a caller-supplied
source snapshot. SQLite truth and vectors are shared read-only during requests;
each worker has its own embedding-response cache and application state.
"""

from __future__ import annotations

import argparse
import cProfile
import ctypes
import importlib
import io
import json
import math
import os
import pstats
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path


def distribution(samples):
    """Return nearest-rank percentiles with the underlying milliseconds."""
    ordered = sorted(samples)
    return dict(
        n=len(samples),
        p50_ms=ordered[math.ceil(len(samples) * 0.5) - 1],
        p95_ms=ordered[math.ceil(len(samples) * 0.95) - 1],
        samples_ms=samples,
    )


def bind(cores):
    """Bind this benchmark process only; verify the effective affinity mask."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        kernel.GetProcessAffinityMask.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        )
        process = kernel.GetCurrentProcess()
        if not kernel.SetProcessAffinityMask(process, sum(1 << core for core in cores)):
            raise ctypes.WinError(ctypes.get_last_error())
        actual, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(process, ctypes.byref(actual), ctypes.byref(system)):
            raise ctypes.WinError(ctypes.get_last_error())
        return dict(cores=cores, mask=actual.value)
    os.sched_setaffinity(0, cores)
    return dict(cores=sorted(os.sched_getaffinity(0)))


def cpu_snapshot():
    """Return system CPU counters; deltas include any competing test processes."""
    if os.name == "nt":
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError()
        return idle.value, kernel.value + user.value
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = list(map(int, fields))
    return values[3] + values[4], sum(values)


def cpu_percent(before, after):
    """Calculate busy percentage from cumulative operating-system CPU ticks."""
    idle = after[0] - before[0]
    total = after[1] - before[1]
    return 100 * (1 - idle / total) if total else 0.0


def install_before(root):
    """Execute archived modules inside their original package to preserve imports."""
    names = ("secret_patterns", "model_content", "retrieval_storage", "background_context", "recall_overflow", "recall")
    for name in names:
        module = importlib.import_module("scope_recall.core." + name)
        source = root / (name + ".py")
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), module.__dict__)
    content = importlib.import_module("scope_recall.core.model_content")
    overflow = importlib.import_module("scope_recall.core.recall_overflow")
    packet = importlib.import_module("scope_recall.core.recall_packet")
    packet.append_overflow = overflow.append_overflow
    # Imported release helpers retain function references until explicitly
    # rebound; using an archived scanner only in retrieval would mix builds.
    for module in tuple(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("scope_recall.core.") and hasattr(
            module, "withhold_secret_items"
        ):
            module.withhold_secret_items = content.withhold_secret_items


def open_lab(args):
    """Open only the caller-selected synthetic fixture and local fake ports."""
    from cache import RequestCache
    from lab import Lab
    from ports import Ports
    from realistic_distribution import NOW, SCOPES

    ports = Ports(RequestCache(args.output / (args.label + "-requests.sqlite3")))
    lab = Lab(args.data, ports, now=NOW, scopes=SCOPES, installation_kind="shared", existing=True)
    lab.context = replace(lab.context, entry_id="bench")
    if args.missing_vectors:
        from cache import digest
        from scope_recall.runtime.vector_port import VectorPort
        from scope_recall.vector.store import build_vector_store

        missing = build_vector_store(
            "sqlite",
            storage_dir=args.output / "absent-vector-generation",
            table_name="public_bench",
            dimensions=3072,
            metric="cosine",
        )

        def search_missing(*unused, **kwargs):
            missing.open_existing()
            raise AssertionError("missing synthetic generation unexpectedly opened")

        missing.search_scopes = search_missing
        lab.core.recall_pipeline.vector_port = VectorPort(
            missing,
            ports.embedder,
            expected_embedding_space=digest(ports.config["embedding"]),
            clock=lab.clock.monotonic,
        )
    return lab


def build(args):
    """Build exactly one synthetic scale, recording actual aggregate counts."""
    import shutil

    import realistic_distribution as fixture
    from cache import RequestCache
    from lab import Lab
    from ports import Ports

    required = args.scale * 3072 * 4 * 2.5 + 2_000_000_000 if args.vectors else 2_000_000_000
    if shutil.disk_usage(args.data.parent).free < required:
        raise RuntimeError("insufficient free space for synthetic vectors and mapped cache")
    started = time.perf_counter()
    lab = Lab(
        args.data,
        Ports(RequestCache(args.output / "build-requests.sqlite3")),
        now=fixture.NOW,
        scopes=fixture.SCOPES,
        installation_kind="shared",
    )
    try:
        refs = fixture.seed_sources(lab, args.scale)
        sources_at = time.perf_counter()
        stats = fixture.seed_claims(lab, refs)
        claims_at = time.perf_counter()
        if args.vectors:
            fixture.seed_vectors(lab, refs)
        result = dict(
            scale=args.scale,
            sources_ms=(sources_at - started) * 1000,
            claims_ms=(claims_at - sources_at) * 1000,
            vectors_ms=(time.perf_counter() - claims_at) * 1000,
            vectors=args.vectors,
            **stats,
        )
        (args.output / "fixture.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        lab.close()


def _signature(value):
    """Compare complete content-bearing values while ignoring request timing."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def source_hashes(before):
    """Identify the exact source modules loaded by one benchmark implementation."""
    import hashlib

    names = ("secret_patterns", "model_content", "retrieval_storage", "background_context", "recall_overflow", "recall")
    result = {}
    for name in names:
        module = importlib.import_module("scope_recall.core." + name)
        source = before / (name + ".py") if before else Path(module.__file__)
        result[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    return result


def worker(args):
    """Serve serialized measurements from one fixed implementation and cache."""
    if args.before:
        install_before(args.before)
    from cache import digest
    from realistic_distribution import NOW

    lab = open_lab(args)
    current = {}
    original_search = lab.core.recall_pipeline.search
    original_claims = lab.core.recall_pipeline.storage_reader.claims

    def search(context):
        result = original_search(context)
        current["retrieval"] = dict(result.diagnostics)
        current["candidates"] = [asdict(candidate) for candidate in result.candidates]
        return result

    def claims(tx, context, *, limit):
        result = original_claims(tx, context, limit=limit)
        current.setdefault("claims", []).append([asdict(candidate) for candidate in result])
        return result

    lab.core.recall_pipeline.search = search
    lab.core.recall_pipeline.storage_reader.claims = claims
    affinity = bind(args.cores)
    print(json.dumps(dict(ready=True, affinity=affinity, source_sha256=source_hashes(args.before))), flush=True)
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("quit"):
                break
            current.clear()
            lab.clock.now = NOW
            mode = command.get("mode", "auto")
            scopes = frozenset(command.get("scopes", ["public", "public-1"]))
            context = replace(lab.context, allowed_scope_ids=scopes, session_id=command["session"])
            body = dict(
                protocol_version="1.1",
                request_id=digest([command["query"], mode])[:24],
                query=command["query"],
                mode=mode,
                max_items=6 if mode == "auto" else 10,
                budget_tokens=4096,
            )
            if mode == "as_of":
                body["as_of"] = command.get("as_of", "2026-01-04T12:00:00Z")
            profiler = cProfile.Profile() if command.get("profile") else None
            ticks, cpu = time.perf_counter(), cpu_snapshot()
            if profiler:
                profiler.enable()
            packet = dict(
                lab.core.recall_packet(context, body, zone=UTC, deadline_seconds=30 if command.get("compare") else 5)
            )
            if profiler:
                profiler.disable()
            elapsed = (time.perf_counter() - ticks) * 1000
            result = dict(
                elapsed_ms=elapsed,
                cpu_busy_percent=cpu_percent(cpu, cpu_snapshot()),
                retrieval_ms=float(current.get("retrieval", {}).get("retrieval", 0)),
                stages=current.get("retrieval", {}),
                gaps=packet.get("gaps", []),
                items=len(packet.get("items", [])),
            )
            if command.get("compare"):
                result["claim_signature"] = _signature(current.get("claims", []))
                result["candidate_signature"] = _signature(current.get("candidates", []))
                # This random diagnostic handle refers to each request's own
                # log record; all content and policy fields remain compared.
                result["packet_signature"] = _signature(
                    {key: value for key, value in packet.items() if key != "diagnostic_ref"}
                )
            if profiler:
                stream = io.StringIO()
                pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(60)
                (args.output / (args.label + "-profile.txt")).write_text(stream.getvalue(), encoding="utf-8")
                profiler.dump_stats(str(args.output / (args.label + ".prof")))
            print(json.dumps(result), flush=True)
    finally:
        lab.core.recall_diagnostics.flush()
        lab.close()


def launch(args, label):
    """Launch an isolated worker with bounded native math threads."""
    command = [
        sys.executable,
        "-B",
        __file__,
        "worker",
        "--data",
        str(args.data),
        "--output",
        str(args.output),
        "--label",
        label,
        "--cores",
        *map(str, args.cores),
    ]
    if args.before and label == "before":
        command += ["--before", str(args.before)]
    if args.missing_vectors:
        command.append("--missing-vectors")
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    error = (args.output / (label + "-stderr.log")).open("w", encoding="utf-8")
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error, text=True, encoding="utf-8", env=env
    )
    line = child.stdout.readline()
    if not line:
        child.wait(timeout=30)
        error.close()
        raise RuntimeError(f"{label} worker failed; inspect stderr log")
    return child, error, json.loads(line)


def ask(child, command):
    """Send exactly one request and require its complete measurement receipt."""
    child.stdin.write(json.dumps(command) + "\n")
    child.stdin.flush()
    line = child.stdout.readline()
    if not line:
        raise RuntimeError("measurement worker ended without a receipt")
    return json.loads(line)


def questions(count):
    """Mix frequent/rare subjects, qualifiers, history and cross-scope reads."""
    result = []
    for index in range(count):
        subject = (index * 127) % 12386 if index % 5 else index % 10
        suffix = (
            "color",
            "color amber",
            f"TESTcondition{index % 19:02d} color",
            "amber",
            f"TESTvalue{index % 97:02d} color",
        )[index % 5]
        result.append(
            dict(
                query=f"TESTsubject{subject:05d} {suffix}",
                mode=("auto", "current", "history", "as_of")[index % 4],
                scopes=["public", "public-1"] if index % 7 else ["public-2", "public-3"],
            )
        )
    return result


def comparison_questions(data, count):
    """Name actual synthetic versions in each state/condition/revision group."""
    import sqlite3

    rows = []
    with sqlite3.connect(data / "truth" / "memory.sqlite3") as connection:
        for state in ("active", "proposed", "retracted", "disputed"):
            for conditioned in (False, True):
                for revised in (False, True):
                    rows.extend(
                        connection.execute(
                            "SELECT json_extract(v.payload_json,'$.subject'),c.scope_id,v.state,v.revision,"
                            "json_extract(v.payload_json,'$.conditions[0]') FROM claim_versions v "
                            "JOIN claims c ON c.claim_id=v.claim_id WHERE v.state=? AND (v.revision>1)=? "
                            "AND (json_array_length(v.payload_json,'$.conditions')>0)=? "
                            "ORDER BY v.claim_id,v.revision LIMIT 4",
                            (state, int(revised), int(conditioned)),
                        ).fetchall()
                    )
    result = []
    for index in range(count):
        subject, scope, state, revision, condition = rows[(index // 4) % len(rows)]
        allowed = [scope] if index % 9 else ["public-2", "public-3"]
        result.append(
            dict(
                query=f"{subject} color amber" + (f" {condition}" if condition else ""),
                mode=("auto", "current", "history", "as_of")[index % 4],
                scopes=allowed,
                fixture_state=state,
                fixture_revision=revision,
                fixture_conditioned=bool(condition),
            )
        )
    return result


def paired(args):
    """AB/BA alternate three repetitions per question, keeping the shortest pair."""
    started_at = datetime.now(UTC).isoformat()
    workers = {label: launch(args, label) for label in ("before", "after")}
    rows = []
    try:
        first = {}
        profiles = {}
        for label, (child, _error, _ready) in workers.items():
            first[label] = ask(child, dict(query="TESTsubject01000 color amber", session="TEST-first-session"))
            profiles[label] = ask(
                child, dict(query="TESTsubject01000 color amber", session="TEST-profile-session", profile=True)
            )
        selected = (
            comparison_questions(args.data, args.queries) if args.action == "compare" else questions(args.queries)
        )
        for index, question in enumerate(selected):
            if args.action == "paired":
                question["mode"] = "auto"
            samples = {label: [] for label in workers}
            for repeat in range(1 if args.action == "compare" else 3):
                order = ("before", "after") if (index + repeat) % 2 == 0 else ("after", "before")
                command = dict(question, session=f"TEST-pair-session-{index}", compare=args.action == "compare")
                for label in order:
                    samples[label].append(ask(workers[label][0], command))
            shortest = {label: min(values, key=lambda row: row["elapsed_ms"]) for label, values in samples.items()}
            row = dict(
                index=index,
                question=question,
                before=shortest["before"],
                after=shortest["after"],
                delta_ms=shortest["after"]["elapsed_ms"] - shortest["before"]["elapsed_ms"],
            )
            if args.action == "paired":
                row["repetitions"] = samples
            if args.action == "compare":
                row["equal"] = {
                    key: shortest["before"].pop(key) == shortest["after"].pop(key)
                    for key in ("claim_signature", "candidate_signature", "packet_signature")
                }
            rows.append(row)
            (args.output / (args.action + "-progress.json")).write_text(
                json.dumps(dict(first=first, pairs=rows)), encoding="utf-8"
            )
            print(json.dumps(dict(index=index, delta_ms=row["delta_ms"], equal=row.get("equal"))), flush=True)
        result = dict(
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            logical_processors=os.cpu_count(),
            first=first,
            profiles=profiles,
            affinity={label: worker[2] for label, worker in workers.items()},
            pairs=rows,
            before=distribution([row["before"]["elapsed_ms"] for row in rows]),
            after=distribution([row["after"]["elapsed_ms"] for row in rows]),
            before_retrieval=distribution([row["before"]["retrieval_ms"] for row in rows]),
            after_retrieval=distribution([row["after"]["retrieval_ms"] for row in rows]),
        )
        if args.action == "compare":
            result["differences"] = {key: sum(not row["equal"][key] for row in rows) for key in rows[0]["equal"]}
        (args.output / (args.action + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        for child, error, _ready in workers.values():
            if child.poll() is None:
                ask_quit = json.dumps(dict(quit=True)) + "\n"
                child.stdin.write(ask_quit)
                child.stdin.flush()
                child.wait(timeout=30)
            error.close()


def main():
    """Run independently resumable fixture construction, comparison or timing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "worker", "paired", "compare"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--scale", type=int, default=350000)
    parser.add_argument("--vectors", action="store_true")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--cores", nargs="+", type=int, default=[0])
    parser.add_argument("--label", default="after")
    parser.add_argument("--missing-vectors", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.action == "build":
        bind(args.cores)
        build(args)
    elif args.action == "worker":
        worker(args)
    else:
        paired(args)


if __name__ == "__main__":
    main()
