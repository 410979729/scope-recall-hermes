"""Attachment authorization and bounded worker shutdown contracts."""

from __future__ import annotations

from threading import Event

from scope_recall.adapters.hermes.attachments import authorize_attachment_metadata
from scope_recall.adapters.hermes.worker import AdapterWorker

# This generous limit only bounds a hang; shutdown behavior uses its own short budget.
_HANG_TIMEOUT_SECONDS = 60.0


def test_authorized_svg_metadata_is_accepted():
    auth = authorize_attachment_metadata(
        {
            "filename": "TEST-same.svg",
            "svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "comment": "v1",
        }
    )
    assert auth.authorized
    assert auth.media_type == "image/svg+xml"
    assert auth.sha256


def test_untrusted_paths_are_rejected():
    auth = authorize_attachment_metadata(
        {
            "filename": "x.png",
            "path": "/etc/passwd",
            "sha256": "a" * 64,
            "byte_length": 10,
        }
    )
    assert not auth.authorized
    assert auth.gap and "untrusted_path" in auth.gap


def test_url_and_traversal_metadata_are_rejected():
    auth = authorize_attachment_metadata(
        {
            "filename": "../secret.png",
            "mediaType": "image/png",
            "sha256": "b" * 64,
            "byte_length": 10,
        }
    )
    assert not auth.authorized


def test_missing_authorization_records_attachment_gap(adapter):
    provider, _clock = adapter
    provider.on_turn_start(1, "attach", turn_id="turn-a")
    provider.observe_pre_llm(
        session_id="TEST-session-1",
        turn_id="turn-a",
        user_message="with attachment",
        attachments=[{"filename": "x.png", "mediaType": "image/png"}],
    )
    assert any("attachment_gap" in gap for gap in provider.diagnostics.pending_outcome_gaps)


def test_bounded_shutdown_leaves_unfinished_work_pending():
    """Shutdown reports unfinished callbacks whether they are queued or already running."""
    worker = AdapterWorker()
    release = Event()

    def slow():
        assert release.wait(_HANG_TIMEOUT_SECONDS), "the callback must be released"

    try:
        assert worker.submit(slow)
        state = worker.shutdown(timeout=0.05)
        assert state["status"] == "timed_out"
        assert state["active_tasks"] + state["pending_wakeups"] == 1
    finally:
        release.set()
        worker.shutdown(timeout=0)
        if worker._thread is not None:
            worker._thread.join(timeout=_HANG_TIMEOUT_SECONDS)
            assert not worker._thread.is_alive(), "the released worker must exit"
