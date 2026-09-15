"""The real Hermes recall dispatcher shares Core's cheap authority fence."""
import json

from scope_recall.core.storage import Transaction
from tests.v11_support import source_event


def test_recall_delivery_does_not_run_backlog_diagnostics(adapter, monkeypatch):
    provider, _clock = adapter
    identity = provider._identity
    context = identity.trusted_context()
    receipt = provider._core.record_event(
        context, source_event(content="TEST-H100 launch happened at 09:15."),
        scope_id=identity.local_scope_id,
    )

    def no_diagnostics(*args, **kwargs):
        raise AssertionError("a recall delivery fence must not enumerate the backlog")

    monkeypatch.setattr(Transaction, "status", no_diagnostics)
    response = json.loads(provider.handle_tool_call("recall", {
        "protocol_version": "1.1", "query": "TEST-H100", "mode": "history",
        "max_items": 1, "budget_tokens": 1600,
    }))
    assert response["result"]["items"][0]["ref"] == receipt.event_refs[0].ref
