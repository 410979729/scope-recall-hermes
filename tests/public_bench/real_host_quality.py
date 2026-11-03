"""Real-clock promotion/adoption and independent service-failure experiments."""

from __future__ import annotations

import json
import socket
import sqlite3
import time
from pathlib import Path

from real_host_io import write_json
from real_host_runtime import active, doctor, homes, library, queue, supervisor, wait_until
from real_host_session import HOSTS, Session, injected, recalled
from scenario_checks import adoption, cross_entry, p95
from scenarios import Results


def deterministic_quality(run, *, adoption_only=False):
    """Reuse scenario assertions and fixture ports; never count them as model-quality evidence."""
    scenes = library(run, "adoption-flow" if adoption_only else "quality-flow")
    result = Results(run.specs)
    (adoption if adoption_only else cross_entry)(scenes, result, run.args)
    keys = ("S4.adoption", "S4.inject") if adoption_only else ("S3.raw", "S3.auto", "S3.promotion")
    rows = [result.rows[key] for key in keys]
    return all(row["status"] == "pass" for row in rows), dict(checks=rows, clock="deterministic_business_clock")


def quality_check(run):
    """Measure five sufficient-evidence facts with ordinary production supervisor scheduling."""
    if run.args.dry_run:
        return deterministic_quality(run)
    scenes = library(run, "quality", models=True)
    session = Session(run, scenes.root / "project", homes(scenes))
    preference = session.ask("claude-code", "TEST-preference 我的长期配色偏好是琥珀色。", label="preference")
    remembered = session.ask("codex", "TEST-preference 我的配色偏好是什么？", label="cross-host")
    samples = []
    for index in range(5):
        marker = "TEST-promotion" + str(index)
        tentative = session.ask("claude-code", marker + " 配色可能是琥珀色。", label=f"tentative-{index}")
        began = time.monotonic()
        confirmation = session.ask(
            "claude-code", marker + " 配色是琥珀色。这是已核实的事实。", label=f"confirm-{index}"
        )
        samples.append(
            dict(
                marker=marker,
                sufficient_at=began,
                tentative=tentative,
                confirmation=confirmation,
                promotion_seconds=None,
                active=[],
            )
        )
    worker = supervisor(run, scenes)
    deadline = max(row["sufficient_at"] for row in samples) + run.args.promotion_timeout
    observations = []
    while time.monotonic() < deadline:
        for row in samples:
            if row["promotion_seconds"] is None:
                claims = promoted_facts(scenes, row["marker"])
                if claims:
                    row.update(active=claims, promotion_seconds=time.monotonic() - row["sufficient_at"])
        observations.append(dict(elapsed=time.monotonic() - samples[0]["sufficient_at"], queue=queue(scenes)))
        write_json(scenes.root / "promotion-observations.json", observations)
        if all(row["promotion_seconds"] is not None for row in samples) or worker.poll() is not None:
            break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    promoted = [row["promotion_seconds"] for row in samples if row["promotion_seconds"] is not None]
    for row in samples:
        row["read"] = session.ask("codex", row["marker"] + " 配色是什么？", label=row["marker"] + "-read")
    observed_p95 = p95(promoted) if len(promoted) == 5 else None
    passed = (
        recalled(remembered, "琥珀色")
        and len(promoted) == 5
        and observed_p95 <= 1200
        and all(recalled(row["read"], "琥珀色") and row["confirmation"]["exit_code"] == 0 for row in samples)
    )
    return passed, dict(
        samples=samples,
        sample_count=5,
        completed=len(promoted),
        promotion_p95_seconds=observed_p95,
        missing_samples_censored=True,
        preference=preference,
        cross_host=remembered,
        cadence="production defaults; wall clock; no queue edits",
        queue=queue(scenes),
    )


def promoted_facts(scenes, marker):
    """A correct quoted source alone cannot establish correct factual extraction."""
    result = []
    for row in active(scenes, marker):
        payload = json.loads(row["payload_json"])
        if payload["kind"] == "fact" and payload["subject"] == marker and "琥珀色" in payload["value_text"]:
            result.append(row)
    return result


def adopted_decisions(scenes):
    """Require decision evidence from the assistant's plan and the owner's later adoption."""
    result = []
    for row in active(scenes, "平滑重启"):
        payload = json.loads(row["payload_json"])
        if payload["kind"] != "decision" or not all(text in payload["value_text"] for text in ("备份", "平滑重启")):
            continue
        sources = []
        for span in payload["evidence_spans"]:
            sources.extend(
                scenes.rows(
                    "SELECT origin,content FROM source_events WHERE event_id=? AND source_revision=?",
                    (span["source_ref"], span["source_revision"]),
                )
            )
        assistant = any(
            source["origin"] == "assistant_visible"
            and "方案二" in source["content"]
            and "平滑重启" in source["content"]
            for source in sources
        )
        owner = any(
            source["origin"] == "human_direct" and source["content"].strip() == "好，就用方案二" for source in sources
        )
        if assistant and owner:
            result.append(row)
    return result


def adoption_check(run):
    """Ask for two literal plans, adopt plan two in the same Claude session and read in Codex."""
    if run.args.dry_run:
        return deterministic_quality(run, adoption_only=True)
    scenes = library(run, "adoption", models=True)
    session = Session(run, scenes.root / "project", homes(scenes))
    proposal = session.ask(
        "claude-code",
        "TEST 重启策略，请列出两个方案：方案一直接覆盖配置；"
        "方案二先备份配置，再平滑重启，失败时恢复备份。只列方案，不执行。",
        label="plans",
    )
    if not proposal["session"]:
        return False, dict(proposal=proposal, reason="host did not return resumable session identity")
    confirmation = session.ask("claude-code", "好，就用方案二", session=proposal["session"], label="adopt")
    supervisor(run, scenes)
    claims, elapsed = wait_until(lambda: adopted_decisions(scenes), run.args.promotion_timeout)
    read = session.ask("codex", "TEST 按上次定的方案来，复述采用的重启策略，不执行。", label="read")
    supported = bool(claims) and any(len(json.loads(row["payload_json"])["evidence_spans"]) >= 2 for row in claims)
    passed = (
        proposal["exit_code"] == confirmation["exit_code"] == 0
        and supported
        and all(recalled(read, text) for text in ("备份", "平滑重启"))
        and "直接覆盖配置" not in read["answer"]
        and not injected(read, "直接覆盖配置")
    )
    return passed, dict(
        proposal=proposal,
        confirmation=confirmation,
        active=claims,
        read=read,
        observed_seconds=elapsed,
        both_sources_supported=supported,
    )


def closed_endpoint():
    """Reserve a loopback TCP port without listening, so connections fail without hitting a service."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}/v1/chat/completions"


def outage_configs(scenes, kind, endpoint):
    """Change only this experiment's route or vector directory, preserving the good configurations."""
    from scope_recall.maintenance.shared import bind_runtime_configuration
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig

    originals, backups = {}, {}
    paths = [scenes.store / "runtime-config.json", *(config.runtime_config_path for config in scenes.configs.values())]
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        originals[path] = raw
        changed = json.loads(json.dumps(raw))
        if kind in {"model", "embedding"}:
            route = changed["auxiliary"]["consolidation" if kind == "model" else "embedding"]
            route["endpoint"] = endpoint
            route["allow_insecure_endpoint"] = True
            changed = bind_runtime_configuration(changed, RuntimeInstanceConfig.from_mapping(raw).binding)
        else:
            vector = changed.get("vector")
            if vector is None:
                raise ValueError("configured vector path required for vector outage")
            corrupt_vector_copy(scenes, Path(vector["storage_dir"]), backups)
        RuntimeInstanceConfig.from_mapping(changed)
        write_json(path, changed)
    return originals, backups


def corrupt_vector_copy(scenes, directory, backups):
    """Keep the legal vector path while replacing its test-only file with a damaged copy."""
    import shutil

    directory.resolve().relative_to(scenes.store.resolve())
    paths = [path for path in directory.rglob("*") if path.suffix in {".sqlite3", ".sqlite", ".db"}]
    if not paths:
        raise ValueError("no physical vector database found to copy and corrupt")
    for source in paths:
        if source in backups:
            continue
        destination = scenes.root / "TEST-vector-backups" / source.relative_to(directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as conn, sqlite3.connect(destination) as backup:
            conn.backup(backup)
        backups[source] = destination
        shutil.copyfile(destination, source)
        with source.open("r+b") as stream:
            stream.write(b"TEST-corrupt-vector-header")


def restore_outage(scenes, original, backups):
    """Restore only this experiment's configs/files between normal supervisor passes."""
    import shutil

    from scope_recall.core.file_lock import advisory_file_lock

    with advisory_file_lock(scenes.store / "runtime-worker.lock", timeout_seconds=40):
        for source, backup in backups.items():
            shutil.copyfile(backup, source)
        for path, raw in original.items():
            write_json(path, raw)


def outage_check(run):
    """Exercise each real failure independently and observe automatic recovery of failed work IDs."""
    if run.args.dry_run:
        from scenario_mutations import outages

        scenes = library(run, "outage-flow")
        result = Results(run.specs)
        outages(scenes, result, run.args)
        rows = [row for row in result.rows.values() if row["id"] != "S10.real"]
        return all(row["status"] == "pass" for row in rows), dict(checks=rows, fault_ports="deterministic")
    results = []
    for kind in ("model", "embedding", "vector"):
        scenes = library(run, "outage-" + kind, models=True)
        session = Session(run, scenes.root / "project", homes(scenes))
        warmup = session.ask("claude-code", "TEST-outage-warm 配色是琥珀色。", label="warm")
        # One normal pass materializes the good vector files before the copied-file fault.
        warm = supervisor(run, scenes)
        wait_until(
            lambda scenes=scenes: any(
                row["state"] == "done" and row["work_type"] == "embed" for row in queue(scenes)["rows"]
            ),
            run.args.recovery_timeout,
        )
        # Gracefully retire only this owned supervisor before exchanging test configurations.
        run.stop_supervisor(warm)
        reserved, endpoint = closed_endpoint()
        try:
            original, backups = outage_configs(scenes, kind, endpoint)
            failed = session.ask("claude-code", "TEST-outage 配色是琥珀色。请继续正常对话。", label="during-claude")
            other = session.ask("codex", "TEST-outage 配色是什么？", label="during-codex")
            worker = supervisor(run, scenes)
            observed, fault_seconds = wait_until(
                lambda scenes=scenes, kind=kind: fault_items(scenes, kind),
                min(180, run.args.recovery_timeout),
            )
            diagnosis = doctor(scenes)
            before = queue(scenes)
            supervisor_alive = worker.poll() is None
            restore_outage(scenes, original, backups)
            failed_ids = {row["work_id"] for row in observed or []}
            recovered, recovery_seconds = wait_until(
                lambda scenes=scenes, failed_ids=failed_ids: recovery_state(scenes, failed_ids),
                run.args.recovery_timeout,
            )
            final = session.ask("codex", "TEST-outage 配色是什么？", label="after")
            diagnosed = doctor_explains(diagnosis, observed or [])
            passed = (
                failed["exit_code"] == other["exit_code"] == 0
                and failed["answer"]
                and other["answer"]
                and bool(failed_ids)
                and diagnosed
                and bool(recovered)
                and supervisor_alive
                and recalled(final, "琥珀色")
            )
            results.append(
                dict(
                    kind=kind,
                    passed=bool(passed),
                    warmup=warmup,
                    during=[failed, other],
                    doctor=diagnosis,
                    failed_ids=sorted(failed_ids),
                    before=before,
                    after=queue(scenes),
                    final=final,
                    fault_observation_seconds=fault_seconds,
                    recovery_seconds=recovery_seconds,
                    same_supervisor_pid=worker.pid,
                    supervisor_alive_at_restore=supervisor_alive,
                )
            )
        finally:
            reserved.close()
    return all(row["passed"] for row in results), dict(
        faults=results, recovery="production supervisor; no repair command"
    )


def fault_items(scenes, kind):
    """Observe errors only on the channel this experiment actually disabled."""
    types = {"model": {"consolidate", "evaluate_candidate"}, "embedding": {"embed"}, "vector": {"embed", "purge"}}
    return [row for row in queue(scenes)["rows"] if row["work_type"] in types[kind] and row["last_error_code"]]


def doctor_explains(report, observed):
    """Require the actual target work error in the doctor's work diagnostics."""
    fields = ("work_error_counts", "pending_error_counts", "recent_work_errors", "worker_status")
    text = json.dumps({key: report.get(key) for key in fields}, ensure_ascii=False)
    return bool(observed) and all(row["last_error_code"] in text for row in observed)


def recovery_state(scenes, failed_ids):
    """Require observed failed items to finish, plus all remaining channels to drain."""
    state = queue(scenes)
    done = {row["work_id"] for row in state["rows"] if row["state"] in {"done", "obsolete"}}
    return state if failed_ids and failed_ids <= done and state["depth"] == 0 else None
