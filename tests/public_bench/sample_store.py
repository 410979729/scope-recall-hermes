"""Create real-release synthetic stores and verify their in-place upgrades.

Run this file with each release's isolated interpreter using -I. Imports then
come from that release, never from this checkout. Only explicitly supplied
task-owned directories are used. No model port or host program is constructed.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.storage import _directory

NOW = "2026-01-08T12:00:00Z"
SCOPE = "TEST-sample-scope"


class Clock:
    """Fixed business time with a real monotonic deadline clock."""

    def utc_now(self):
        """Return the fixture's fixed UTC business time."""
        return NOW

    def monotonic(self):
        """Use real elapsed time for storage deadlines."""
        import time

        return time.monotonic()


def binding(root: Path):
    """Use the common release contract; new optional context fields stay optional."""
    instance = InstanceBinding("TEST-samples", "TEST-samples-install", root / "store", frozenset({SCOPE}))
    values = {
        "binding": instance,
        "session_id": "TEST-fixtures",
        "allowed_scope_ids": instance.scope_ids,
        "actor_origin": "human_direct",
    }
    if "source_principal" in inspect.signature(TrustedContext).parameters:
        from scope_recall.contracts import TrustedSourcePrincipal

        values["source_principal"] = TrustedSourcePrincipal("human", "verified", "principal:human:TEST-samples")
    return instance, TrustedContext(**values)


def fingerprint(path: Path) -> dict:
    """Hash actual source text, truth, deletions and preserved queue state."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {"schema": connection.execute("PRAGMA user_version").fetchone()[0]}
        selections = {
            "source_events": ["event_id", "source_revision", "content", "content_sha256", "read_blocked", "suppressed"],
            "claim_versions": ["claim_id", "revision", "state", "payload_json"],
            "deletion_operations": ["operation_id", "request_sha256", "mode", "requested_refs_json"],
            "object_blocks": ["object_kind", "object_ref", "read_blocked", "suppressed"],
            "work_items": [
                "work_id",
                "work_type",
                "subject_ref",
                "subject_revision",
                "scope_id",
                "state",
                "attempt",
                "available_at",
                "lease_token",
                "lease_owner",
                "lease_until",
                "last_error_code",
                "consolidation_offset",
            ],
        }
        for table, wanted in selections.items():
            if table not in tables:
                continue
            actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            columns = [name for name in wanted if name in actual]
            names = ",".join(columns)
            where = " WHERE state IN ('failed','pending','leased')" if table == "work_items" else ""
            rows = connection.execute(f"SELECT {names} FROM {table}{where} ORDER BY {names}").fetchall()
            encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
            result[table] = {"columns": columns, "count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
            if table == "work_items":
                result[table]["rows"] = rows
        result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
    return result


def query(core, context) -> list:
    """A fixed lexical query, compared before and after upgrading."""
    packet = core.recall_packet(
        context,
        {
            "protocol_version": "1.1",
            "request_id": "TEST-fixed-query",
            "query": "TEST-project 配色",
            "mode": "current",
            "max_items": 10,
            "budget_tokens": 4096,
        },
    )
    return [[item["kind"], item["ref"], item["revision"]] for item in packet["items"]]


def create(root: Path) -> dict:
    """Write the same history, current/candidate claims and deletion through real APIs."""
    instance, context = binding(root)
    core = MemoryCore(CoreConfig(instance), clock=Clock())
    core.initialize()
    sources = []
    texts = [
        "TEST-project 配色 蓝色。",
        "TEST-project 配色 绿色。",
        "TEST-orchard cultivar golden.",
        "TEST-history 配色 不是红色。",
        "TEST-assistant 只是一条建议。",
        "TEST-delete synthetic disposable.",
    ]
    for index, text in enumerate(texts):
        origin = "assistant_visible" if index == 4 else "human_direct"
        event = {
            "protocol_version": "1.1",
            "source_event_key": f"TEST-sample/{index}",
            "source_revision": 1,
            "origin": origin,
            "role": "assistant" if index == 4 else "user",
            "content": text,
            "occurred_at": f"2026-01-0{index + 1}T12:00:00Z",
            "recorded_at": NOW,
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }
        receipt = core.record_event(
            replace(context, actor_origin=origin, session_id=f"TEST-session-{index}"),
            event,
            scope_id=SCOPE,
            remaining_seconds=30,
        )
        if receipt.durability != "persisted":
            raise RuntimeError(f"sample capture failed: {index}: {receipt.error_code}")
        sources.append(core.source(context, receipt.event_refs[0].ref, 1))
    for source, value in zip(sources[:2], ("蓝色", "绿色"), strict=True):
        proposal = {
            "kind": "decision",
            "subject": "TEST-project",
            "predicate": "配色",
            "value_text": value,
            "conditions": [],
            "statement_kind": "decision",
            "valid_from": source.event["occurred_at"],
            "valid_to": None,
            "evidence_spans": [{"source_ref": source.ref, "source_revision": 1, "quote": source.event["content"]}],
        }
        records = getattr(core, "records", core)
        records.accept_claim_proposals(
            context,
            {
                "protocol_version": "1.1",
                "source_refs": [f"{source.ref}@1"],
                "claim_proposals": [proposal],
                "resume_proposals": [],
                "reference_proposals": [],
            },
            scope_id=SCOPE,
            remaining_seconds=30,
        )
    source = sources[-1]
    authorization = dict(
        event,
        source_event_key="TEST-sample/delete-authorization",
        origin="human_direct",
        role="user",
        content=f"删除 {source.ref}",
        occurred_at=NOW,
        recorded_at=NOW,
    )
    core.record_event(context, authorization, scope_id=SCOPE, remaining_seconds=30)
    # The deletion target is synthetic data created above by this invocation.
    core.forget(
        context,
        {
            "protocol_version": "1.1",
            "target_refs": [source.ref],
            "mode": "delete",
            "expected_revisions": {source.ref: 1},
        },
        remaining_seconds=30,
    )
    with sqlite3.connect(core.storage.path) as connection:
        connection.execute(
            "UPDATE work_items SET state='failed',last_error_code='TEST-retryable' "
            "WHERE work_id=(SELECT min(work_id) FROM work_items WHERE state='pending')"
        )
        connection.execute(
            "UPDATE work_items SET state='leased',lease_owner='TEST-old-worker',lease_token=1,"
            "lease_until='2026-01-09T12:00:00Z' "
            "WHERE work_id=(SELECT min(work_id) FROM work_items WHERE state='pending')"
        )
        connection.commit()
        with sqlite3.connect(root / "pre-upgrade.sqlite3") as backup:
            connection.backup(backup)
    return {
        "path": str(core.storage.path),
        "before": fingerprint(core.storage.path),
        "fixed_query": query(core, context),
    }


def verify(root: Path, *, output: Path | None = None, exercise_next_schema: bool = False) -> dict:
    """Upgrade an independent original snapshot, preserving the historical fixture."""
    original = json.loads((root / "sample.json").read_text(encoding="utf-8"))
    parent = output or root
    parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="verify-", dir=parent))
    instance, context = binding(workspace)
    instance.data_directory.mkdir(parents=True)
    snapshot = root / "pre-upgrade.sqlite3"
    expected = fingerprint(snapshot)
    target = instance.data_directory / "memory.sqlite3"
    with (
        closing(sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(target)) as destination,
    ):
        source.backup(destination)
        # Local fixtures bind their absolute directory. Rebind only the copy;
        # truth and queue contents remain comparable with the original snapshot.
        destination.execute(
            "UPDATE instance_meta SET data_directory=? WHERE singleton=1", (_directory(instance.data_directory),)
        )
        destination.commit()
    core = MemoryCore(CoreConfig(instance), clock=Clock())
    # Exercise admission through a normal automatic writable open.
    with core.storage.write(context):
        pass
    upgrade_snapshots = sorted((target.parent / "upgrade-snapshots").glob("*.sqlite3"))
    automatic_snapshot_equal = bool(upgrade_snapshots) and fingerprint(upgrade_snapshots[0]) == expected
    first_open_schema = fingerprint(target)["schema"]
    first_snapshot_checked = expected["schema"] == first_open_schema or automatic_snapshot_equal
    if exercise_next_schema:
        before_next = fingerprint(target)
        register_test_migration()
        with core.storage.write(context):
            pass
        upgrade_snapshots = sorted((target.parent / "upgrade-snapshots").glob("*.sqlite3"))
        next_snapshot_equal = len(upgrade_snapshots) == 1 and fingerprint(upgrade_snapshots[0]) == before_next
    else:
        next_snapshot_equal = True
    actual = fingerprint(core.storage.path)
    comparisons = {
        table: actual.get(table) == block
        for table, block in expected.items()
        if isinstance(block, dict) and table != "work_items"
    }
    # Upgrades may queue new derived work; original work must retain its state
    # and lease/error details regardless of those additional rows.
    required, present = expected.get("work_items"), actual.get("work_items")
    if required is not None:
        current = {row[0]: row for row in present["rows"]} if present else {}
        comparisons["work_items"] = bool(present and required["columns"] == present["columns"]) and all(
            current.get(row[0]) == row for row in required["rows"]
        )
    fixed = query(core, context)
    rollback = workspace / "rollback-rehearsal.sqlite3"
    with (
        closing(sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(rollback)) as destination,
    ):
        source.backup(destination)
    rollback_ok = fingerprint(rollback) == expected
    config_path = root / "runtime-config.json"
    config_backend = None
    if config_path.exists():
        from scope_recall.runtime.instance_config import RuntimeInstanceConfig

        configured = RuntimeInstanceConfig.from_mapping(json.loads(config_path.read_text(encoding="utf-8")))
        config_backend = configured.vector.backend
    return {
        "from_schema": expected["schema"],
        "to_schema": actual["schema"],
        "truth_checks": comparisons,
        "query_equal": fixed == original["fixed_query"],
        "query_before": original["fixed_query"],
        "query_after": fixed,
        "integrity": actual["integrity"],
        "snapshot_restore_equal": rollback_ok,
        "old_config_backend": config_backend,
        "validation_path": str(core.storage.path),
        "snapshot_path": str(snapshot),
        "automatic_upgrade_snapshots": [str(path) for path in upgrade_snapshots],
        "automatic_snapshot_equal": automatic_snapshot_equal,
        "first_open_schema": first_open_schema,
        "first_snapshot_checked": first_snapshot_checked,
        "next_snapshot_equal": next_snapshot_equal,
        "after": actual,
        "passed": all(comparisons.values())
        and fixed == original["fixed_query"]
        and rollback_ok
        and first_snapshot_checked
        and next_snapshot_equal
        and actual["integrity"] == "ok"
        and expected["integrity"] == "ok",
    }


def register_test_migration() -> None:
    """Register a synthetic next-format step only inside the benchmark test process."""
    from scope_recall.core import storage

    current = storage.SCHEMA_VERSION

    def advance(conn):
        conn.execute("UPDATE instance_meta SET schema_version=?", (current + 1,))
        conn.execute(f"PRAGMA user_version={current + 1}")

    storage.SCHEMA_MIGRATIONS = {**storage.SCHEMA_MIGRATIONS, current: advance}
    storage.UPGRADE_CHAIN = (*storage.UPGRADE_CHAIN, current)
    storage.SCHEMA_VERSION = current + 1


def main():
    """Create or upgrade a sample only at its explicit task-owned root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exercise-next-schema", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    result = (
        create(args.root)
        if args.action == "create"
        else verify(args.root, output=args.output, exercise_next_schema=args.exercise_next_schema)
    )
    destination = (
        args.root / "sample.json"
        if args.action == "create"
        else (args.output or Path(result["validation_path"]).parents[1]) / "upgrade.json"
    )
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result))
    if args.action == "verify" and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
