"""Measure a health page against the frozen synthetic source and fact distribution."""

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode

from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance.ui_api import UIApi
from scope_recall.maintenance.ui_http import UIServer

from tests.host.test_memory_ui_http import send


def seed_health_findings(lab, now):
    """Install low-sorting synthetic findings using the benchmark's existing SQLite fixture."""
    from cases import Turn

    text = "TEST-Health test-health TeSt-Health color amber blue."
    receipt = lab.capture(Turn("TEST-health-fixture", "TEST-health", "user", text, now))
    source = receipt.event_refs[0].ref
    with sqlite3.connect(lab.core.storage.path) as db:
        db.row_factory = sqlite3.Row
        claim = dict(db.execute("SELECT * FROM claims WHERE scope_id='public' LIMIT 1").fetchone())
        version = dict(db.execute("SELECT * FROM claim_versions WHERE claim_id=?", (claim["claim_id"],)).fetchone())
        for index, (subject, value) in enumerate(
            (("TEST-Health", "amber"), ("test-health", "amber"), ("TeSt-Health", "blue"))
        ):
            ref = "claim-" + f"{index:064x}"
            row = dict(
                claim,
                claim_id=ref,
                subject=subject,
                predicate="color",
                kind="fact",
                slot_key=hashlib.sha256(ref.encode()).hexdigest(),
            )
            proposal = json.loads(version["payload_json"])
            proposal.update(
                kind="fact",
                subject=subject,
                predicate="color",
                value_text=value,
                statement_kind="assertion",
                conditions=[],
                evidence_spans=[dict(source_ref=source, source_revision=1, quote=text)],
            )
            body = dict(version, claim_id=ref, payload_json=json.dumps(proposal))
            db.execute("INSERT INTO claims VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
            db.execute("INSERT INTO claim_versions VALUES (" + ",".join("?" for _ in body) + ")", tuple(body.values()))
            db.execute(
                "INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?,?)",
                ("claim", ref, 1, source, 1, "supports", text, None),
            )
    return tuple("claim-" + f"{index:064x}" for index in range(3))


def seed_recall_log(lab):
    """Give the page a real retained history to scan inside its time budget."""
    store = lab.core.recall_diagnostics.store
    for day in ("2025-12-30", "2026-01-08"):
        store.write(
            dict(
                ref="TEST-" + day,
                timestamp=day + "T12:00:00Z",
                installation_id=lab.binding.installation_id,
                session_id="TEST-health",
                request_id="TEST-" + day,
                items=[],
            )
        )
    with sqlite3.connect(store.path) as db:
        rows = [
            (
                "TEST-log-" + str(i),
                "2026-01-01T12:00:00Z",
                lab.binding.installation_id,
                "TEST-health",
                "TEST-log-" + str(i),
                json.dumps(dict(items=[], padding="x" * 500)),
            )
            for i in range(5000)
        ]
        db.executemany(
            "INSERT INTO attempts(ref,timestamp,installation_id,session_id,request_id,payload) VALUES (?,?,?,?,?,?)",
            rows,
        )
    lab.core.recall_diagnostics.writer._persist_health()


def test_health_first_page_at_frozen_scale(tmp_path, record_property):
    sys.path.insert(0, str(Path(__file__).parents[1] / "public_bench"))
    from cache import RequestCache
    from lab import Lab
    from performance import NOW, SCOPES, seed_truth
    from ports import Ports

    ports = Ports(RequestCache(tmp_path / "cache.sqlite3"))
    lab = Lab(tmp_path / "health-scale", ports, now=NOW, backend="sqlite", scopes=SCOPES)
    try:
        seed_truth(lab, 350000)
        refs = seed_health_findings(lab, NOW)
        seed_recall_log(lab)
        api = UIApi(MemoryManagement(lab.core, InstallationMaintenance(lab.context)))
        with UIServer(api) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                started = time.perf_counter()
                status, _, raw = send(server, "/api/health")
                elapsed = (time.perf_counter() - started) * 1000
                assert status == 200
                result = json.loads(raw)["data"]
                assert "unused_complete" in result
                first_complete = result["complete"]
                status, _, raw = send(server, "/api/health?limit=1")
                assert status == 200
                result = json.loads(raw)["data"]
                found = set()
                cursor = None
                for _ in range(12):
                    for category in ("duplicates", "contradictions"):
                        if any(set(refs) >= {fact["ref"] for fact in issue["facts"]} for issue in result[category]):
                            found.add(category)
                    if found == {"duplicates", "contradictions"}:
                        break
                    next_cursor = result["next_cursor"]
                    assert next_cursor and next_cursor != cursor
                    cursor = next_cursor
                    status, _, raw = send(server, "/api/health?" + urlencode(dict(limit=1, cursor=cursor)))
                    assert status == 200
                    result = json.loads(raw)["data"]
                assert found == {"duplicates", "contradictions"}
                record_property("first_page_ms", elapsed)
                record_property("complete", str(first_complete))
                record_property("source_count", 350001)
                record_property("fact_count", 35003)
                record_property("recall_records", 5002)
            finally:
                server.shutdown()
                thread.join(6)
                assert not thread.is_alive()
    finally:
        lab.close()
