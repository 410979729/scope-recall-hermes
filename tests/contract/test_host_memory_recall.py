"""Automatic recall removes only exact statements already supplied by a host."""

import pytest

from tests.contract.test_claims import app, capture
from tests.v11_support import recall_request


@pytest.mark.parametrize("host", ["claude-code", "codex", "hermes"])
def test_automatic_packet_omits_host_duplicate_and_records_reason(app, host):
    core, ctx = app
    sentence = "TEST observatory palette is amber."
    source = capture(core, ctx, sentence)
    request = recall_request(query="TEST observatory palette", mode="auto", request_id="TEST-" + host)
    packet = core.recall_packet(ctx, request, host_memory_entries=(sentence,))
    assert source.ref not in {item["ref"] for item in packet["items"]}
    record = core.recall_diagnostics.get(packet["diagnostic_ref"]).to_public()
    item = next(item for item in record["items"] if item.get("ref") == source.ref)
    assert item["reason"] == "host_memory_duplicate"
    assert item["source_type"] == "event"


def test_explicit_recall_does_not_deduplicate_host_context(app):
    core, ctx = app
    sentence = "TEST observatory palette is amber."
    source = capture(core, ctx, sentence)
    packet = core.recall_packet(
        ctx, recall_request(query="TEST observatory palette", mode="current"), host_memory_entries=(sentence,)
    )
    assert source.ref in {item["ref"] for item in packet["items"]}


def test_optional_host_comparison_error_preserves_automatic_recall(app, monkeypatch):
    import scope_recall.core.recall_packet as recall_packet

    core, ctx = app
    sentence = "TEST observatory palette is amber."
    source = capture(core, ctx, sentence)

    def fail(*_args):
        raise RuntimeError("TEST comparison error")

    monkeypatch.setattr(recall_packet, "host_memory_duplicate", fail)
    packet = core.recall_packet(
        ctx, recall_request(query="TEST observatory palette", mode="auto"), host_memory_entries=(sentence,)
    )
    assert source.ref in {item["ref"] for item in packet["items"]}


def test_invalid_optional_snapshot_preserves_automatic_recall(app):
    core, ctx = app
    sentence = "TEST observatory palette is amber."
    source = capture(core, ctx, sentence)
    packet = core.recall_packet(
        ctx,
        recall_request(query="TEST observatory palette", mode="auto"),
        host_memory_entries=("x" * 65537,),
    )
    assert source.ref in {item["ref"] for item in packet["items"]}
