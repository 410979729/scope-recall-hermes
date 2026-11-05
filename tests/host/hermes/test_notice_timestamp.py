"""Notification identity regressions; synthetic fixtures from the live host contract.

Gateway run_turn_prepare._hmwa_apply_message_timestamp renders the user row but
turn_context passes the clean persist_user_message as the hook's user_message.
These are contract reconstructions, not captured production hook kwargs.
"""

from __future__ import annotations

import unittest
from threading import Lock
from types import SimpleNamespace

from scope_recall.adapters.hermes.backpressure import HostBackpressure
from scope_recall.adapters.hermes.boundary import host_notice
from scope_recall.adapters.hermes.turn_capture import TurnCapture

NOTICE = "[ASYNC DELEGATION BATCH COMPLETE] TEST background result"
STAMP = "[Mon 2026-11-02 08:00:00 CST] "
# Corresponds to the rendered wall time above in Asia/Shanghai.
EPOCH = 1793577600.0


def notice_row(**changes):
    return {
        "role": "user",
        "content": STAMP + NOTICE,
        "timestamp": EPOCH,
        "display_kind": "async_delegation_complete",
        **changes,
    }


class NoticeTimestampTests(unittest.TestCase):
    def test_timestamp_rendered_current_notice_matches_clean_hook_input(self):
        self.assertTrue(host_notice([notice_row()], NOTICE))

    def test_untyped_human_paste_is_not_a_notice(self):
        self.assertFalse(host_notice([notice_row(display_kind=None)], NOTICE))
        self.assertFalse(host_notice([{"role": "user", "content": NOTICE}], NOTICE))

    def test_older_same_text_notice_does_not_take_human_paste(self):
        history = [notice_row(), {"role": "assistant", "content": "TEST done"}, {"role": "user", "content": NOTICE}]
        self.assertFalse(host_notice(history, NOTICE))

    def test_steer_is_still_human(self):
        self.assertFalse(host_notice([notice_row(display_kind="steer")], NOTICE))

    def test_unrelated_prefix_and_body_substring_do_not_match(self):
        self.assertFalse(host_notice([notice_row(content="[Note: model switched]\n" + NOTICE)], NOTICE))
        self.assertFalse(host_notice([notice_row(content=STAMP + "Quoted: " + NOTICE)], NOTICE))

    def test_metadata_kind_is_not_an_invented_host_contract(self):
        self.assertFalse(
            host_notice([notice_row(display_kind=None, metadata={"display_kind": "async_delegation_complete"})], NOTICE)
        )

    def test_unstamped_timestamp_lookalike_is_not_unwrapped(self):
        self.assertFalse(host_notice([notice_row(timestamp=None)], NOTICE))

    def test_same_classifier_is_cached_for_pre_llm_and_busy_sync_fallback(self):
        adapter = SimpleNamespace(_said_lock=Lock(), _notice_turns={}, _holder=None, _backpressure={})
        capture = TurnCapture(adapter)
        adapter._turns = capture
        self.assertTrue(capture.note_opener("TEST-turn", [notice_row()], NOTICE))
        self.assertTrue(capture.opened_by_host("TEST-turn", NOTICE))
        self.assertFalse(capture.opened_by_host("TEST-turn", "TEST human request"))
        self.assertFalse(capture.opened_by_host("TEST-other-turn", NOTICE))
        with self.assertLogs("scope_recall.adapters.hermes.provider", level="WARNING"):
            HostBackpressure(adapter).busy(
                "pre_llm_call",
                {
                    "turn_id": "TEST-busy",
                    "user_message": NOTICE,
                    "conversation_history": [notice_row()],
                },
            )
        self.assertEqual(adapter._skipped_turn_id, "TEST-busy")
        self.assertTrue(capture.opened_by_host("TEST-busy", NOTICE))


if __name__ == "__main__":
    unittest.main()
