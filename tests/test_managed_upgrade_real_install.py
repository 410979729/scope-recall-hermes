"""One disposable N-1-style managed activation with real installer code."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import types
from typing import Any

from scope_recall import installer
from scope_recall.sql_store import ensure_schema


ROOT = Path(__file__).resolve().parents[1]
V2_0_0_TAG = "v2.0.0"
V2_0_0_COMMIT = "3eb37ba6caf3fed60e7d50b2e6d8396c71bfa935"
V2_0_0_TREE = "ac1fc080ca403c8b8cdb5f0f86e0299664c1fa26"


def _install_hermes_import_stubs(monkeypatch) -> None:
    """Supply only the Hermes import boundary needed by installer activation."""

    agent_package = types.ModuleType("agent")
    agent_package.__path__ = []  # type: ignore[attr-defined]
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    memory_provider_module.MemoryProvider = MemoryProvider
    agent_package.memory_provider = memory_provider_module  # type: ignore[attr-defined]
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = []  # type: ignore[attr-defined]
    tools_registry_module = types.ModuleType("tools.registry")
    tools_registry_module.tool_error = lambda message: json.dumps(  # type: ignore[attr-defined]
        {"ok": False, "error": str(message)}
    )
    tools_package.registry = tools_registry_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider_module)
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.registry", tools_registry_module)


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _archive_frozen_v2_0_0(tmp_path: Path) -> Path:
    """Materialize only the exact public v2.0.0 Git tree under pytest temp."""

    assert _git_output("rev-parse", f"{V2_0_0_TAG}^{{commit}}") == V2_0_0_COMMIT
    assert _git_output("rev-parse", f"{V2_0_0_TAG}^{{tree}}") == V2_0_0_TREE
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--prefix=scope_recall/",
            V2_0_0_COMMIT,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    archive_root = tmp_path / "frozen-v2.0.0"
    archive_root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as payload:
        payload.extractall(archive_root, filter="data")
    source = archive_root / "scope_recall"
    assert source.is_dir()
    assert "version: 2.0.0" in (source / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    return source


def _bootstrap_frozen_v2_0_0_truth(source: Path, home: Path) -> dict[str, Any]:
    """Create baseline truth through the archived code in a site-free process."""

    bootstrap = textwrap.dedent(
        r"""
        import json
        from pathlib import Path
        import sqlite3
        import sys

        import scope_recall
        from scope_recall.candidate_extraction import ExtractedCandidate
        from scope_recall.candidate_store import store_event_candidates
        from scope_recall.models import RuntimeScope
        from scope_recall.sql_store import ensure_schema, store_row

        source = Path(sys.argv[1]).resolve()
        home = Path(sys.argv[2]).resolve()
        assert Path(scope_recall.__file__).resolve() == source / "__init__.py"
        storage = home / "scope-recall"
        storage.mkdir(parents=True, exist_ok=True)
        overlay = {
            "auto_recall": False,
            "journal": {"enabled": False},
            "retrieval": {"top_k": 7},
            "vector": {"enabled": False},
        }
        (storage / "config.json").write_text(
            json.dumps(overlay, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        database = storage / "memory.sqlite3"
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        ordinary_metadata = {
            "lifecycle": "promoted",
            "memory_type": "preference",
            "custom_patch_marker": {
                "keep": True,
                "owner": "v2.0.0-ordinary",
            },
        }
        ordinary_id, _summary, _updated_at, inserted = store_row(
            conn,
            memory_id="patch-baseline-ordinary",
            scope_id="scope:patch-baseline",
            platform="telegram",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="thread-a",
            gateway_session_key="session-key-a",
            agent_identity="agent-a",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="tool-store",
            target="user",
            content="User prefers exact patch baseline preservation across upgrades.",
            metadata=json.dumps(ordinary_metadata, sort_keys=True),
            timestamp="2026-08-30T00:00:00+00:00",
        )
        assert inserted is True
        candidate = ExtractedCandidate(
            target="user",
            content="User prefers review before adopting a candidate memory.",
            memory_type="preference",
            confidence=0.73,
            evidence_refs=["session:patch-baseline:turn:4"],
            metadata={
                "custom_patch_marker": {
                    "keep": True,
                    "owner": "v2.0.0-candidate",
                    "ordinal": 201,
                }
            },
        )
        candidate_report = store_event_candidates(
            conn,
            candidates=[candidate],
            scope=RuntimeScope(
                platform="telegram",
                user_id="user-a",
                chat_id="chat-a",
                thread_id="thread-a",
                gateway_session_key="session-key-a",
                agent_identity="agent-a",
                agent_workspace="workspace-a",
            ),
            scope_id="scope:patch-baseline",
            session_id="session-a",
            dry_run=False,
        )
        assert candidate_report["inserted"] == 1
        candidate_id = str(candidate_report["ids"][0])
        conn.commit()
        columns = (
            "id, scope_id, platform, user_id, chat_id, thread_id, "
            "gateway_session_key, agent_identity, agent_workspace, session_id, "
            "source, target, content, summary, created_at, updated_at, metadata"
        )
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT {columns} FROM memories WHERE id IN (?, ?) ORDER BY id",
                (ordinary_id, candidate_id),
            ).fetchall()
        ]
        migrations = [
            list(row)
            for row in conn.execute(
                "SELECT id, applied_at, plugin_version, description, checksum, "
                "status, error FROM schema_migrations ORDER BY rowid"
            ).fetchall()
        ]
        payload = {
            "candidate_id": candidate_id,
            "config_overlay": overlay,
            "migrations": migrations,
            "rows": rows,
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        }
        conn.close()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        """
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    for name in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-S", "-c", bootstrap, str(source), str(home)],
        cwd=source.parent,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def _truth_rows_and_migrations(database: Path, ids: list[str]) -> dict[str, Any]:
    columns = (
        "id, scope_id, platform, user_id, chat_id, thread_id, "
        "gateway_session_key, agent_identity, agent_workspace, session_id, "
        "source, target, content, summary, created_at, updated_at, metadata"
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in ids)
        rows = [
            dict(row)
            for row in connection.execute(
                f"SELECT {columns} FROM memories WHERE id IN ({marks}) ORDER BY id",
                ids,
            ).fetchall()
        ]
        migrations = [
            list(row)
            for row in connection.execute(
                "SELECT id, applied_at, plugin_version, description, checksum, "
                "status, error FROM schema_migrations ORDER BY rowid"
            ).fetchall()
        ]
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return {
        "migrations": migrations,
        "rows": rows,
        "user_version": user_version,
    }


def _write_previous_plugin(home: Path) -> None:
    plugin = home / "plugins" / "scope-recall"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "# register_memory_provider previous fixture\n",
        encoding="utf-8",
    )
    (plugin / "provider.py").write_text("PREVIOUS = True\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        "name: scope-recall\nversion: 1.10.3\n",
        encoding="utf-8",
    )
    (plugin / "config.json").write_text("{}\n", encoding="utf-8")


def test_real_managed_upgrade_preserves_memory_and_degrades_legacy_vector_debt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # CI validates against the pinned real Hermes tree.  This local integration
    # test supplies only the import boundary needed to exercise the complete
    # installer transaction in the intentionally lean developer environment.
    _install_hermes_import_stubs(monkeypatch)

    home = tmp_path / "hermes-home"
    _write_previous_plugin(home)
    (home / "config.yaml").write_text(
        "memory:\n  provider: scope-recall\n",
        encoding="utf-8",
    )
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": True}}) + "\n",
        encoding="utf-8",
    )
    db_path = storage / "memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO memories(
                id, scope_id, platform, user_id, chat_id, thread_id,
                gateway_session_key, agent_identity, agent_workspace, session_id,
                source, target, content, summary, created_at, updated_at, metadata
            ) VALUES (
                'upgrade-memory', 'scope-a', 'test', 'user', '', '', '',
                'agent', 'workspace', 'session', 'tool-store', 'memory',
                'The managed upgrade must preserve this exact memory.',
                'The managed upgrade must preserve this exact memory.',
                '2026-08-30T00:00:00+00:00', '2026-08-30T00:00:00+00:00', '{}'
            )
            """
        )
        connection.commit()

    # Keep credential discovery inside the disposable home. The managed path
    # must neither need nor contact an embedding provider.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "isolated-user"))
    for name in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    private = (
        storage
        / "upgrades"
        / "operations"
        / "real-n-minus-one"
        / "private"
    )
    result = installer.install(
        hermes_home=home,
        activate=True,
        maintenance_mode=True,
        managed_upgrade=True,
        managed_state_dir=private,
    )

    assert result["ok"] is True, json.dumps(
        {
            "mode": result.get("mode"),
            "activation_error": result.get("activation_error"),
            "upgrade_compatibility": result.get("upgrade_compatibility"),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    assert result["activated"] is True
    assert result["previous_version"] == "1.10.3"
    assert result["new_version"] == "2.0.1"
    assert result["activation_transaction"]["status"] == "committed"
    assert result["upgrade_compatibility"]["requires_vector_degrade"] is True
    assert json.loads((storage / "config.json").read_text(encoding="utf-8"))[
        "vector"
    ]["enabled"] is False

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT content FROM memories WHERE id='upgrade-memory'"
        ).fetchone()
    assert row == ("The managed upgrade must preserve this exact memory.",)
    assert not (storage / ".activation-maintenance.json").exists()


def test_exact_v2_0_0_tag_to_v2_0_1_managed_upgrade_preserves_patch_baseline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_hermes_import_stubs(monkeypatch)
    frozen_source = _archive_frozen_v2_0_0(tmp_path)
    home = tmp_path / "hermes-home-v2-patch"
    plugin = home / "plugins" / "scope-recall"
    plugin.parent.mkdir(parents=True)
    shutil.copytree(frozen_source, plugin)
    (home / "config.yaml").write_text(
        "memory:\n  provider: scope-recall\n",
        encoding="utf-8",
    )
    before = _bootstrap_frozen_v2_0_0_truth(frozen_source, home)
    candidate_id = str(before["candidate_id"])
    memory_ids = ["patch-baseline-ordinary", candidate_id]

    isolated_user = tmp_path / "isolated-user"
    monkeypatch.setenv("HOME", str(isolated_user))
    monkeypatch.setenv("USERPROFILE", str(isolated_user))
    for name in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    private = (
        home
        / "scope-recall"
        / "upgrades"
        / "operations"
        / "exact-v2.0.0-to-v2.0.1"
        / "private"
    )
    result = installer.install(
        hermes_home=home,
        activate=True,
        maintenance_mode=True,
        managed_upgrade=True,
        managed_state_dir=private,
    )

    assert result["ok"] is True, json.dumps(result, ensure_ascii=False, default=str)
    assert result["activated"] is True
    assert result["previous_version"] == "2.0.0"
    assert result["new_version"] == "2.0.1"
    assert result["activation_transaction"]["status"] == "committed"
    assert result["upgrade_compatibility"]["requires_vector_degrade"] is False
    assert "version: 2.0.1" in (plugin / "plugin.yaml").read_text(encoding="utf-8")

    storage = home / "scope-recall"
    assert json.loads((storage / "config.json").read_text(encoding="utf-8")) == before[
        "config_overlay"
    ]
    after = _truth_rows_and_migrations(storage / "memory.sqlite3", memory_ids)
    assert after["user_version"] == before["user_version"]
    assert after["migrations"][: len(before["migrations"])] == before["migrations"]
    assert after["rows"] == before["rows"]

    rows_by_id = {str(row["id"]): row for row in after["rows"]}
    candidate_metadata = json.loads(rows_by_id[candidate_id]["metadata"])
    ordinary_metadata = json.loads(rows_by_id["patch-baseline-ordinary"]["metadata"])
    assert candidate_metadata["lifecycle"] == "candidate"
    assert candidate_metadata["custom_patch_marker"] == {
        "keep": True,
        "owner": "v2.0.0-candidate",
        "ordinal": 201,
    }
    assert ordinary_metadata["custom_patch_marker"] == {
        "keep": True,
        "owner": "v2.0.0-ordinary",
    }
    assert json.loads((storage / "config.json").read_text(encoding="utf-8"))[
        "vector"
    ] == {"enabled": False}
    assert not (storage / ".activation-maintenance.json").exists()
