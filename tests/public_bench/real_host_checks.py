"""Installation, true host sessions and historical-store restart acceptance."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from real_host_io import write_json
from real_host_runtime import homes, library
from real_host_session import HOSTS, PROBE, Session, recalled
from scenario_model import FixtureModel, drain


def host_check(run, prefix="host-"):
    """Capture a fact in one session; require hook injection and answer in a new session."""
    rows = {}
    for host in HOSTS:
        scenes = library(run, prefix + host)
        session = Session(run, scenes.root / "project", homes(scenes))
        statement = "TEST-azalea 配色是琥珀色。请简短确认。"
        first = session.ask(host, statement, label="capture")
        second = session.ask(host, "TEST-azalea 配色是什么？请根据已有记忆回答。", label="recall")
        rows[host] = dict(
            first=first,
            second=second,
            passed=recalled(second, "琥珀色"),
            captured=any(row["content"] == statement and row["origin"] == "human_direct" for row in scenes.rows()),
        )
    return all(row["passed"] and row["captured"] for row in rows.values()), dict(hosts=rows)


def wheel_install(run):
    """Download dependencies separately, then install the wheel into an offline clean uv venv."""
    if run.installed:
        return run.installed
    root = run.root / "installation"
    root.mkdir()
    wheels = sorted(run.args.wheel.glob("*.whl")) if run.args.wheel.is_dir() else [run.args.wheel]
    if len(wheels) != 1 or not wheels[0].is_file():
        raise ValueError("--wheel must name exactly one release wheel")
    destination = root / "wheelhouse"
    tools = root / "download-tools"
    prepared = run.children.run(
        [run.args.uv, "venv", "--seed", "--python", sys.executable, str(tools)],
        root / "download-tools.json",
        timeout=600,
    )
    if prepared["exit_code"]:
        raise RuntimeError("isolated download tooling unavailable")
    downloader = tools / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    download = run.children.run(
        [
            str(downloader),
            "-I",
            "-B",
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(destination),
            str(wheels[0]) + "[codex]",
        ],
        root / "download.json",
        timeout=1800,
    )
    if download["exit_code"]:
        raise RuntimeError("wheel download failed; see installation/download.json")
    started = time.monotonic()
    venv = root / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    creation = run.children.run([run.args.uv, "venv", "--python", sys.executable, str(venv)], root / "venv.json")
    installed = run.children.run(
        [
            run.args.uv,
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            str(destination),
            "--python",
            str(python),
            str(wheels[0]) + "[codex]",
        ],
        root / "install.json",
    )
    if creation["exit_code"] or installed["exit_code"]:
        raise RuntimeError("clean offline wheel install failed")
    run.installed = dict(
        python=python,
        download_seconds=download["seconds"],
        install_seconds=time.monotonic() - started + prepared["seconds"],
        wheel=str(wheels[0]),
    )
    return run.installed


def installed_setup(run, python, root, *, full):
    """Invoke public setup exactly once, in a clean home, with explicit test-only paths."""
    root.mkdir()
    home = root / "home"
    command = [
        str(python),
        "-I",
        "-B",
        "-m",
        "scope_recall.maintenance.cli",
        "setup",
        "--host",
        "claude-code",
        "--home",
        str(home),
        "--store",
        str(root / "shared"),
        "--project",
        str(root),
        "--python",
        str(python),
        "--worker",
        "manual",
        "--test-mode",
        "--yes",
    ]
    if full:
        command += ["--models", str(run.args.routes)]
    began = time.monotonic()
    result = run.children.run(command, root / "setup.json", timeout=600)
    receipts = list((home / "scope-recall/setup-receipts").glob("*.json"))
    receipt = json.loads(receipts[0].read_text(encoding="utf-8")) if len(receipts) == 1 else {}
    raw = run.children.run(
        [
            str(python),
            "-I",
            "-B",
            str(PROBE),
            "raw",
            "--home",
            str(home),
            "--host",
            "claude-code",
            "--output",
            str(root / "raw.json"),
        ],
        root / "probe.json",
    )
    state = "完整记忆功能可用" if full else "原文检索可用"
    passed = result["exit_code"] == 0 and raw["exit_code"] == 0 and receipt.get("state") == state
    return dict(
        passed=passed,
        expected_state=state,
        receipt=receipt,
        seconds=time.monotonic() - began,
        setup_evidence=str(root / "setup.json"),
        raw_evidence=str(root / "raw.json"),
    )


def install_check(run):
    """Check raw and full readiness independently; non-download time includes both verifications."""
    if run.args.dry_run:
        scenes = library(run, "install-flow")
        raw = scenes.hook("codex", "TEST-azalea 配色是琥珀色。")
        worker = drain(scenes, FixtureModel())
        rows = scenes.rows("SELECT state FROM claim_versions")
        passed = bool(raw) and any(row["state"] == "active" for row in rows)
        return passed, dict(
            raw_available=True, full_available=passed, worker=worker, non_download_seconds=0, simulated_install=True
        )
    installed = wheel_install(run)
    raw = installed_setup(run, installed["python"], run.root / "install-raw", full=False)
    full = installed_setup(run, installed["python"], run.root / "install-full", full=True)
    seconds = installed["install_seconds"] + raw["seconds"] + full["seconds"]
    return raw["passed"] and full["passed"], dict(
        **installed, raw=raw, full=full, non_download_seconds=seconds, repairs=0
    )


def historical_library(run, sample, name):
    """Relocate a historical snapshot's binding only; its truth and queue are untouched."""
    from scope_recall.adapters.hermes.shared_entries import (
        attach_shared_record,
        client_entry_record,
        new_shared_payload,
        write_shared_payload,
    )
    from scope_recall.contracts import InstanceBinding
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.storage import _directory

    root = run.root / name
    root.mkdir()
    store = root / "shared"
    snapshot = (sample / "pre-upgrade.sqlite3").resolve()
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        meta = dict(source.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone())
        scopes = [row[0] for row in source.execute("SELECT scope_id FROM instance_scopes")]
        if len(scopes) != 1:
            raise ValueError("historical host samples require one capture scope")
        payload = new_shared_payload(store, agent_id=meta["agent_id"], test_mode=bool(meta["test_mode"]))
        write_shared_payload(store, payload)
        with sqlite3.connect(store / "memory.sqlite3") as dest:
            source.backup(dest)
            dest.execute(
                "UPDATE instance_meta SET data_directory=?,installation_id=? WHERE singleton=1",
                (_directory(store), payload["installation_id"]),
            )
    if "installation_kind" not in meta:
        # Stores predating shared entries are local by contract. Let the normal
        # local open migrate the copy before converting its installation identity.
        binding = InstanceBinding(
            meta["agent_id"], payload["installation_id"], store, frozenset(scopes), bool(meta["test_mode"])
        )
        MemoryCore(CoreConfig(binding)).initialize()
    with sqlite3.connect(store / "memory.sqlite3") as dest:
        dest.execute("UPDATE instance_meta SET installation_kind='shared' WHERE singleton=1")
    for host in HOSTS:
        attach_shared_record(
            store,
            client_entry_record(
                host=host,
                home=root / (host + "-home"),
                entry_id=host,
                display_name=host,
                attached_at="2026-01-08T12:00:00Z",
                allowed_scope_ids=scopes,
                writable_scope_ids=scopes,
                capture_scope_id=scopes[0],
            ),
            now="2026-01-08T12:00:00Z",
        )
    return root, store


def restart_check(run):
    """Verify all 58 releases, then start both real hosts for every schema family."""
    if run.args.dry_run:
        passed, result = host_check(run, prefix="restart-flow-")
        return passed, dict(simulated_matrix=True, matrix_count=58, host_flow=result)
    installed = wheel_install(run)
    output = run.root / "compatibility"
    result = run.children.run(
        [
            str(installed["python"]),
            "-I",
            "-B",
            str(PROBE),
            "matrix",
            "--samples",
            str(run.args.samples),
            "--output",
            str(output),
        ],
        run.root / "matrix-command.json",
        timeout=1800,
    )
    matrix = json.loads((output / "matrix.json").read_text(encoding="utf-8"))
    families = {}
    for row in matrix:
        families.setdefault(row["from_schema"], row)
    if len(families) > 8:
        raise ValueError(f"{len(families)} schema families exceed the eight-family host budget")
    hosts = []
    for schema, row in families.items():
        root, store = historical_library(run, Path(row["sample"]), "restart-" + str(schema))
        session = Session(
            run, root / "project", {host: root / (host + "-home") for host in HOSTS}, python=installed["python"]
        )
        replies = {host: session.ask(host, "TEST-project 配色是什么？根据记忆简短回答。", label=host) for host in HOSTS}
        # The installed verifier compares ordered fixed-query refs separately;
        # host acceptance additionally requires automatic injection and answer.
        hosts.append(
            dict(
                schema=schema,
                sample=row["sample"],
                replies=replies,
                passed=all(recalled(reply, "绿色") for reply in replies.values()),
            )
        )
    return result["exit_code"] == 0 and all(row["passed"] for row in hosts), dict(
        matrix_count=len(matrix), matrix_evidence=str(output / "matrix.json"), families=hosts, repairs=0
    )
