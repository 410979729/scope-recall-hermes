"""Completed-turn notice fallback: synthetic host rows, real isolated Core capture.

No historical hook kwargs are claimed. The fixture follows the offline Hermes
host fixture, without pytest, models, runtime configuration or helper processes.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall

NOTICE = "TEST background result"
STAMP = "[Mon 2026-11-02 08:00:00 CST] "
EPOCH = 1793577600.0
ANSWER = "TEST acknowledgement"


def notice_row(**changes):
    return {
        "role": "user",
        "content": STAMP + NOTICE,
        "timestamp": EPOCH,
        "display_kind": "process_complete",
        **changes,
    }


class FixedClock:
    def utc_now(self):
        return "2026-11-02T00:00:00Z"

    def monotonic(self):
        return time.monotonic()


class NoticeSyncTests(unittest.TestCase):
    SESSION = "TEST-session"

    def setUp(self):
        # The runner/test harness owns the temporary parent, never a live home.
        self.temp = tempfile.TemporaryDirectory(prefix="scope-notice-sync-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "TEST-hermes-home"
        self.home.mkdir()
        clock = FixedClock()
        _binding, core = install_hermes_scope_recall(
            self.home,
            agent_id="TEST-agent",
            platform="cli",
            user_id="TEST-user",
            agent_workspace="TEST-workspace",
            test_mode=False,
            clock=clock,
        )
        self.provider = ScopeRecallHermesAdapter(core=core, clock=clock)
        self.provider.initialize(
            self.SESSION,
            hermes_home=str(self.home),
            platform="cli",
            agent_context="primary",
            agent_identity="TEST-agent",
            agent_workspace="TEST-workspace",
            user_id="TEST-user",
            parent_session_id="",
        )
        self.addCleanup(self.provider.shutdown)
        self.provider.on_turn_start(1, NOTICE, turn_id="TEST-turn")

    def stored(self):
        with closing(sqlite3.connect(self.home / "scope-recall" / "memory.sqlite3")) as conn:
            return conn.execute("SELECT role,content,origin FROM source_events ORDER BY rowid").fetchall()

    def finish(self, history, text=NOTICE):
        self.provider.sync_turn(text, ANSWER, session_id="TEST-session", messages=history)
        rows = self.stored()
        self.assertIn(("assistant", ANSWER, "assistant_visible"), rows)
        users = [row for row in rows if row[0] == "user" and row[1] == text]
        self.assertEqual(len(users), 1, rows)
        return users[0][2]

    def test_missing_pre_llm_marker_uses_completed_history(self):
        history = [
            notice_row(),
            {"role": "assistant", "content": "TEST checking"},
            {"role": "tool", "content": "TEST result"},
            {"role": "assistant", "content": ANSWER},
        ]
        self.assertEqual(self.finish(history), "host_generated")

    def test_missing_pre_llm_history_cache_uses_completed_history(self):
        self.assertFalse(self.provider._turns.note_opener("TEST-turn", [], NOTICE))
        self.assertEqual(self.finish([notice_row(), {"role": "assistant", "content": ANSWER}]), "host_generated")

    def test_existing_host_cache_remains_authoritative_without_history(self):
        self.assertTrue(self.provider._turns.note_opener("TEST-turn", [notice_row()], NOTICE))
        self.assertEqual(self.finish(None), "host_generated")

    def test_existing_human_capture_is_not_reclassified(self):
        from unittest.mock import patch

        self.provider.observe_pre_llm(
            session_id="TEST-session",
            turn_id="TEST-turn",
            user_message=NOTICE,
            conversation_history=[{"role": "user", "content": NOTICE}],
        )
        with patch(
            "scope_recall.adapters.hermes.turn_capture.sync_host_notice", side_effect=AssertionError("already captured")
        ):
            self.assertEqual(self.finish([notice_row(), {"role": "assistant", "content": ANSWER}]), "human_direct")

    def test_old_notice_does_not_take_new_human_request(self):
        history = [
            notice_row(),
            {"role": "assistant", "content": "TEST old answer"},
            {"role": "user", "content": "TEST new request"},
            {"role": "assistant", "content": ANSWER},
        ]
        self.assertEqual(self.finish(history, "TEST new request"), "human_direct")

    def test_old_notice_does_not_take_same_text_human_request(self):
        history = [
            notice_row(),
            {"role": "assistant", "content": "TEST old answer"},
            {"role": "user", "content": NOTICE},
            {"role": "assistant", "content": ANSWER},
        ]
        self.assertEqual(self.finish(history), "human_direct")

    def test_human_pasted_notice_has_no_host_authority(self):
        text = "[IMPORTANT: Background process TEST finished]"
        self.assertEqual(
            self.finish([{"role": "user", "content": text}, {"role": "assistant", "content": ANSWER}], text),
            "human_direct",
        )

    def test_steer_kind_is_not_host_authority(self):
        self.assertEqual(
            self.finish([notice_row(display_kind="steer"), {"role": "assistant", "content": ANSWER}]), "human_direct"
        )

    def test_untyped_timestamp_row_is_not_host_authority(self):
        self.assertEqual(
            self.finish([notice_row(display_kind=None), {"role": "assistant", "content": ANSWER}]), "human_direct"
        )

    def test_nested_display_kind_is_not_host_authority(self):
        self.assertEqual(
            self.finish(
                [
                    notice_row(display_kind=None, metadata={"display_kind": "process_complete"}),
                    {"role": "assistant", "content": ANSWER},
                ]
            ),
            "human_direct",
        )

    def test_missing_timestamp_does_not_strip_presentation(self):
        row = notice_row()
        row.pop("timestamp")
        self.assertEqual(self.finish([row, {"role": "assistant", "content": ANSWER}]), "human_direct")

    def test_invalid_timestamp_does_not_strip_presentation(self):
        from scope_recall.adapters.hermes.boundary import sync_host_notice

        for stamp in (None, True, "1793577600", 0, -1, float("nan"), float("inf"), 1e300):
            with self.subTest(timestamp=stamp):
                self.assertFalse(
                    sync_host_notice([notice_row(timestamp=stamp), {"role": "assistant", "content": ANSWER}], NOTICE)
                )

    def test_extra_body_does_not_match(self):
        self.assertEqual(
            self.finish([notice_row(content=STAMP + NOTICE + " TEST extra"), {"role": "assistant", "content": ANSWER}]),
            "human_direct",
        )

    def test_double_timestamp_prefix_is_not_stripped_twice(self):
        self.assertEqual(
            self.finish([notice_row(content=STAMP + STAMP + NOTICE), {"role": "assistant", "content": ANSWER}]),
            "human_direct",
        )

    def test_no_history_preserves_human_default(self):
        self.assertEqual(self.finish(None), "human_direct")

    def test_unknown_tail_role_refuses_even_folded_notice(self):
        from scope_recall.adapters.hermes.boundary import sync_host_notice

        row = self.folded(NOTICE)
        for tail in ({"role": "system", "content": "TEST unknown"}, {"role": "unknown"}, {}, None):
            with self.subTest(tail=tail):
                self.assertFalse(sync_host_notice([row, tail, {"role": "assistant", "content": ANSWER}], NOTICE))

    @staticmethod
    def folded(own, summary="TEST summary"):
        return notice_row(
            content="[PRIOR CONTEXT — for reference only; not a new message]\n"
            + own
            + "\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
            + summary
            + "\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---",
            _compressed_summary=True,
        )

    def test_folded_notice_uses_only_own_text(self):
        self.assertEqual(self.finish([self.folded(NOTICE), {"role": "assistant", "content": ANSWER}]), "host_generated")

    def test_folded_summary_quotation_does_not_own_user_text(self):
        self.assertEqual(
            self.finish(
                [self.folded("TEST different own words", summary=NOTICE), {"role": "assistant", "content": ANSWER}]
            ),
            "human_direct",
        )

    def test_folded_hidden_human_text_does_not_gain_host_authority(self):
        row = self.folded(NOTICE)
        row["display_kind"] = "hidden"
        self.assertEqual(self.finish([row, {"role": "assistant", "content": ANSWER}]), "human_direct")

    def test_folded_old_notice_does_not_take_newer_human(self):
        history = [
            self.folded(NOTICE),
            {"role": "assistant", "content": "TEST old"},
            {"role": "user", "content": NOTICE},
            {"role": "assistant", "content": ANSWER},
        ]
        self.assertEqual(self.finish(history), "human_direct")


if __name__ == "__main__":
    unittest.main()
