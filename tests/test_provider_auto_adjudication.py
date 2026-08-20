"""Provider cadence for no-human candidate adjudication."""

from __future__ import annotations

import json

from plugins.memory import load_memory_provider


def _write_scope_recall_config(hermes_home, values):
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(values, ensure_ascii=False) + "\n", encoding="utf-8")


def test_background_digest_runs_auto_adjudication_when_enabled(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "journal": {
                "enabled": True,
                "background_digest_enabled": True,
                "background_digest_synchronous": True,
                "digest_interval_hours": 0.001,
                "max_entries_per_digest": 5,
                "extractor": "heuristic",
            },
            "auto_adjudication": {"enabled": True, "interval_hours": 0},
        },
    )
    calls = {"digest": 0, "adjudicate": []}

    def fake_digest(**kwargs):
        calls["digest"] += 1
        return {"ok": True, "processed_entries": 1}

    def fake_adjudicate(*, trigger):
        calls["adjudicate"].append(trigger)

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    monkeypatch.setitem(
        plugin._run_background_journal_digest.__globals__,
        "run_journal_digest",
        fake_digest,
    )
    plugin.initialize(
        "session-auto-adjudication",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="tianji",
        agent_workspace="hermes",
    )
    monkeypatch.setattr(plugin._background, "maybe_adjudicate", fake_adjudicate)
    try:
        plugin._run_background_journal_digest(plugin._journal_config())
    finally:
        plugin.shutdown()

    assert calls["digest"] == 1
    assert calls["adjudicate"] == ["background-journal-digest"]


def test_auto_adjudication_skips_when_disabled(tmp_path, monkeypatch):
    _write_scope_recall_config(
        tmp_path,
        {
            "vector": {"enabled": False},
            "auto_adjudication": {"enabled": False},
        },
    )
    ran = {"count": 0}

    def fake_run(*args, **kwargs):
        ran["count"] += 1
        return {"ok": True, "status": "applied"}

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-auto-adjudication-off",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="tianji",
        agent_workspace="hermes",
    )
    monkeypatch.setattr(
        "scope_recall.auto_adjudication.run_auto_adjudication",
        fake_run,
    )
    try:
        plugin._maybe_run_auto_adjudication(trigger="manual")
    finally:
        plugin.shutdown()

    assert ran["count"] == 0


def test_stats_include_last_auto_adjudication_report(tmp_path):
    _write_scope_recall_config(
        tmp_path,
        {"vector": {"enabled": False}, "auto_adjudication": {"enabled": True}},
    )
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-auto-adjudication-stats",
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="joy",
        agent_context="primary",
        agent_identity="tianji",
        agent_workspace="hermes",
    )
    plugin._last_adjudication_report = {"ok": True, "status": "applied", "lanes": {"promoted": 1}}
    try:
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
    finally:
        plugin.shutdown()

    assert stats["auto_adjudication"]["status"] == "applied"
    assert stats["auto_adjudication"]["lanes"]["promoted"] == 1
