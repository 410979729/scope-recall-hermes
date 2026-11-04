"""Entity backfill over immutable stores written by historical releases."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.entity_resolution import entity_progress, queue_entity_backfill
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.worker_projection import process_rebuild_projection
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_storage

from tests.contract.test_aliases import app
from tests.public_bench.sample_store import Clock, binding, fingerprint, query, verify


class SeparateEntities:
    """Leave synthetic fixture subjects separate without constructing a transport."""

    def judge(self, messages, **kwargs):
        return json.dumps(dict(support="uncertain", action="new", target_ref=None, target_revision=None))


@pytest.mark.parametrize("release", ["3.1.0", "3.1.2", "3.2.0", "3.12.2", "3.16.1-pypi"])
def test_release_fixture_entity_backfill_is_nonblocking_and_preserves_truth(tmp_path, release):
    samples = os.environ.get("SCOPE_RECALL_COMPAT_SAMPLES")
    if not samples:
        pytest.skip("historical synthetic fixtures are supplied outside the checkout")
    snapshot = Path(samples) / release / "pre-upgrade.sqlite3"
    original_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    report = verify(Path(samples) / release, output=tmp_path / "TEST-upgrade")
    assert report["passed"] and report["to_schema"] == SCHEMA_VERSION
    path = Path(report["validation_path"])
    instance, ctx = binding(path.parents[1])
    clock = Clock()
    core = MemoryCore(CoreConfig(instance), clock=clock)
    before = fingerprint(path)
    fixed = query(core, ctx)
    # The first open performs no model work; ordinary recall is available before backfill.
    assert fixed == report["query_before"]
    for _ in range(10):
        with core.storage.write(ctx) as tx:
            queue_entity_backfill(tx, now=clock.utc_now(), limit=2)
            jobs = tx.work.claim_next(
                "TEST-entity-upgrade",
                clock.utc_now(),
                lease_seconds=60,
                limit=16,
                allowed_work_types=frozenset({"rebuild_projection"}),
            )
        for item in jobs:
            process_rebuild_projection(
                core.storage, clock, ctx, item, model=SeparateEntities(), started=clock.monotonic(), budget=30
            )
        with core.storage.read(ctx) as tx:
            if entity_progress(tx)["pending"] == 0:
                break
    with core.storage.read(ctx) as tx:
        progress = entity_progress(tx)
    assert progress["total"] and progress["pending"] == 0
    after = fingerprint(path)
    for table in ("source_events", "claim_versions", "deletion_operations", "object_blocks"):
        assert after.get(table) == before.get(table)
    assert query(core, ctx) == fixed
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == original_hash
    doctor = DoctorReport("codex", "checking")
    assert check_storage(doctor, instance, path.parent)
    assert any(row["name"] == "entity_resolution" and row["result"] == "ok" for row in doctor.checks)


def test_additive_entity_upgrade_queues_bounded_backfill(app):
    from tests.contract.test_entity_resolution import drain_entities, fact
    from tests.v11_support import downgrade_store

    core, ctx = app
    item = fact(core, ctx, "TEST-local", "category", "cat")
    before = core.current_claim(ctx, item.ref).payload
    downgrade_store(core.storage.path, 1112)
    core.storage.initialize()
    assert tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    assert core.current_claim(ctx, item.ref).payload == before
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx) == dict(total=1, completed=0, pending=1)
    receipt = drain_entities(core, ctx, SeparateEntities())
    assert receipt.failed == 0
    with core.storage.read(ctx) as tx:
        assert entity_progress(tx) == dict(total=1, completed=1, pending=0)
    assert core.current_claim(ctx, item.ref).payload == before
