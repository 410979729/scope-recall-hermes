"""Native memory notifications retain the writer reported by the host."""

import json
import sqlite3
from contextlib import closing

import pytest


@pytest.mark.parametrize(
    "writer,origin,kind",
    [
        ("assistant_tool", "assistant_visible", "assistant"),
        ("background_review", "assistant_visible", "assistant"),
        ("user", "origin_unknown", "human"),
        (None, "origin_unknown", "unknown"),
    ],
)
def test_memory_write_retains_reported_writer_without_owner_authority(adapter, hermes_home, writer, origin, kind):
    provider, _clock = adapter
    metadata = {"write_origin": writer, "session_id": "TEST-session-1"} if writer else None
    provider.on_memory_write("add", "memory", "TEST palette saffron", metadata)
    with closing(sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")) as conn:
        row = conn.execute("SELECT origin,role,extra_json FROM source_events WHERE content LIKE '%saffron%'").fetchone()
    assert row is not None
    assert row[0] == origin
    principal = json.loads(row[2])["source_principal"]
    assert principal["kind"] == kind
    if kind == "human":
        assert principal["resolution"] == "unresolved"


@pytest.mark.parametrize("writer", ["assistant_tool", "background_review", "user", None])
def test_native_memory_material_cannot_reserve_important_work(adapter, hermes_home, writer):
    from scope_recall.core.admission import classify

    provider, _clock = adapter
    provider.on_memory_write("add", "memory", "TEST remember this decision always", {"write_origin": writer})
    with closing(sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM source_events WHERE content LIKE '%remember%'").fetchone()
    event = {**dict(row), **json.loads(row["extra_json"])}
    decision = classify(event)
    assert decision.reason == "import_requires_confirmation"
    assert decision.disposition == "source_only" and not decision.important
    from scope_recall.core.admission import AdmissionPolicy

    assert classify(event, AdmissionPolicy(enabled=False)) == decision
