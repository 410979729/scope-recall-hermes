"""Generate legacy problem-state fixtures with an installed historical release."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_store import NOW, SCOPE, Clock, binding, create, fingerprint, query  # noqa: E402
from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.claims import Qualification  # noqa: E402
from scope_recall.core.recall_policy import SPACE_ID  # noqa: E402
from scope_recall.runtime.instance_config import RuntimeInstanceConfig  # noqa: E402


def legacy_config(instance, backend: str) -> dict:
    """Create a validated old configuration with external model capabilities disabled."""
    identity = asdict(instance)
    identity["data_directory"] = str(identity["data_directory"])
    identity["scope_ids"] = sorted(identity["scope_ids"])
    payload = {
        "binding": identity,
        "session_id": "TEST-fixture-worker",
        "allowed_scope_ids": sorted(instance.scope_ids),
        "actor_origin": "human_direct",
        "owner_id": "TEST-fixture-worker",
        "vector": {
            "backend": backend,
            "storage_dir": str(instance.data_directory / "vectors" / SPACE_ID),
            "table_name": "TEST-fixture",
            "dimensions": 3072,
            "metric": "cosine",
        },
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    RuntimeInstanceConfig.from_mapping(payload)
    return payload


def main():
    """Create a displaced-value head, retry stamps, leases, deletion and both old backends."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backend", choices=["lancedb", "sqlite-bruteforce"], required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    original = create(args.root)
    instance, context = binding(args.root)
    core = MemoryCore(CoreConfig(instance), clock=Clock())
    core.initialize()
    event = {
        "protocol_version": "1.1",
        "source_event_key": "TEST-historical-denial",
        "source_revision": 1,
        "origin": "human_direct",
        "role": "user",
        "content": "TEST-project 配色 不是红色。",
        "occurred_at": "2026-01-07T12:00:00Z",
        "recorded_at": NOW,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
    }
    captured = core.record_event(context, event, scope_id=SCOPE, remaining_seconds=30)
    with sqlite3.connect(core.storage.path) as connection:
        cid = connection.execute("SELECT claim_id FROM claims ORDER BY claim_id LIMIT 1").fetchone()[0]
    with core.storage.write(context) as transaction:
        head = next(
            version for version in transaction.claims.versions(cid) if version.revision == version.current_revision
        )
        payload = dict(
            head.payload,
            value_text="不是红色",
            valid_from=event["occurred_at"],
            valid_to=None,
            evidence_spans=[
                {"source_ref": captured.event_refs[0].ref, "source_revision": 1, "quote": event["content"]}
            ],
        )
        # This intentionally represents a previously stored invalid head. It
        # uses the storage boundary, not a second runtime claim implementation.
        transaction.claims.append(
            SCOPE,
            payload,
            Qualification("active", "direct_report", "explicit_scoped_source"),
            recorded_at=NOW,
            previous=head,
        )
    with sqlite3.connect(core.storage.path) as connection:
        connection.execute(
            "UPDATE work_items SET last_error_code='retried:3.9.7|derivation_retry:1|derivation_invalid' "
            "WHERE state='failed'"
        )
        connection.commit()
        with sqlite3.connect(args.root / "pre-upgrade.sqlite3") as snapshot:
            connection.backup(snapshot)
    config = legacy_config(instance, args.backend)
    (args.root / "runtime-config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    original.update(
        before=fingerprint(core.storage.path),
        fixed_query=query(core, context),
        historical_states=["displaced_denial", "release_retry_marker", "failed", "pending", "leased", "deletion"],
        vector_backend=args.backend,
    )
    (args.root / "sample.json").write_text(json.dumps(original, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": original["before"]["schema"],
                "backend": args.backend,
                "historical_states": original["historical_states"],
            }
        )
    )


if __name__ == "__main__":
    main()
