"""CLI tests for Skill Bridge dry-run candidate generation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import scope_recall.cli as cli
from scope_recall.experience_store import create_playbook
from scope_recall.sql_store import ensure_schema

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "skill.bridge.py"


def _playbook_payload() -> dict:
    return {
        "schema_version": "procedural_playbook.v1",
        "task_class": "scope-recall-rollout",
        "title": "Scope Recall rollout smoke checks",
        "trigger": "When preparing a Scope Recall rollout",
        "goal": "Verify rollout readiness before publishing.",
        "preconditions": [{"name": "clean worktree", "required": True}],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Run focused pytest before release gate.",
                "evidence_required": "pytest output exits 0",
            }
        ],
        "pitfalls": [],
        "verification": ["Focused pytest exits 0"],
        "cleanup": [],
        "reuse_policy": {},
    }


def test_cli_routes_playbook_skill_candidates_to_bridge_script():
    matched = cli._match_script_command(["playbooks", "skill-candidates", "--json"])

    assert matched == ("skill.bridge.py", ["skill-candidates", "--dry-run", "--json"])


def test_skill_bridge_script_generates_skill_candidates_dry_run(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    create_playbook(
        conn,
        playbook_id="pb_success",
        scope_id="scope-a",
        payload=_playbook_payload(),
        evidence_anchors=["session:s1:turn:4"],
        confidence=0.91,
    )
    conn.execute("UPDATE procedural_playbooks SET status='reviewed', success_count=2 WHERE id='pb_success'")
    conn.commit()
    conn.close()

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "skill-candidates", "--db", str(db_path), "--scope-id", "scope-a", "--dry-run", "--json"],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["count"] == 1
    assert payload["candidates"][0]["source_playbook_id"] == "pb_success"
