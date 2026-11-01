"""Native memory notifications retain the writer reported by the host."""

import json
import sqlite3

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
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        row = conn.execute("SELECT origin,role,extra_json FROM source_events WHERE content LIKE '%saffron%'").fetchone()
    assert row is not None
    assert row[0] == origin
    principal = json.loads(row[2])["source_principal"]
    assert principal["kind"] == kind
    if kind == "human":
        assert principal["resolution"] == "unresolved"
