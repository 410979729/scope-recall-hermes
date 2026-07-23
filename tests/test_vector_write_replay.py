"""Bounded vector replay policy tests for ordinary writes."""
from __future__ import annotations

from scope_recall.vector_runtime import vector_write_replay_limit


class Provider:
    def __init__(self, value=None) -> None:
        self._vector_config = {}
        if value is not None:
            self._vector_config["write_outbox_replay_limit"] = value


def test_vector_write_replay_limit_drains_more_than_one_event_by_default() -> None:
    assert vector_write_replay_limit(Provider()) == 20
    assert vector_write_replay_limit(Provider(37)) == 37


def test_vector_write_replay_limit_is_bounded_and_tolerates_bad_config() -> None:
    assert vector_write_replay_limit(Provider(0)) == 1
    assert vector_write_replay_limit(Provider(99999)) == 2000
    assert vector_write_replay_limit(Provider("bad")) == 20
