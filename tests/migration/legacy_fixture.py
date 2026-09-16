"""Test-only builder for an official 578b SQLite fixture.

The child process imports the frozen 578b package from ``git archive`` before
the current package is imported.  This prevents two revisions from sharing
``sys.modules`` and keeps the production migrator independent of Git.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


LEGACY_COMMIT = "578b955802df753f2e2208e26eab6f71971285a0"


def build_official_578b_fixture(path: str | Path, *, repo_root: str | Path, include_multivalue: bool = False) -> Path:
    target = Path(path).resolve()
    root = Path(repo_root).resolve()
    script = r'''
import io, pathlib, sqlite3, subprocess, sys, tarfile, tempfile

target = pathlib.Path(sys.argv[1]).resolve()
repo = pathlib.Path(sys.argv[2]).resolve()
commit = sys.argv[3]
include_multivalue = sys.argv[4] == "1"
with tempfile.TemporaryDirectory(prefix="scope-recall-578b-") as td:
    package = pathlib.Path(td) / "scope_recall"
    package.mkdir()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", commit])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        tf.extractall(package)
    sys.path.insert(0, td)
    from scope_recall import journal_store, privacy_purge_schema, sql_store, temporal_facts
    conn = sqlite3.connect(target)
    try:
        sql_store.ensure_schema(conn, commit=False)
        journal_store.ensure_journal_schema(conn, commit=False)
        temporal_facts.ensure_temporal_fact_schema(conn)
        sql_store.ensure_experience_schema(conn)
        privacy_purge_schema.ensure_privacy_purge_schema(conn)
        journal = [
            (1, "scope-a", "shared-a", "s-1", 1, "user", "用户已确认保留。多值甲。", "2026-09-01T00:00:00Z", '{"scope_mode":"local","runtime_scope_id":"scope-a","shared_scope_id":"shared-a"}'),
            (2, "scope-a", "scope-a", "s-1", 2, "tool", "工具观察到一次结果。", "2026-09-01T00:01:00Z", '{"scope_mode":"shared","runtime_scope_id":"scope-a","shared_scope_id":"scope-a"}'),
            (3, "scope-a", "shared-a", "s-2", 1, "assistant", "多值乙。", "2026-09-01T00:02:00Z", '{"scope_mode":"local","runtime_scope_id":"scope-a","shared_scope_id":"shared-a"}'),
        ]
        for row in journal:
            content_hash = __import__('hashlib').sha256(row[6].encode()).hexdigest()
            conn.execute("INSERT INTO journal_entries(id,scope_id,shared_scope_id,session_id,turn_number,role,content,content_hash,created_at,metadata) VALUES (?,?,?,?,?,?,?,?,?,?)", (*row[:7], content_hash, *row[7:]))
        conn.execute("INSERT INTO memories(id,scope_id,session_id,source,target,content,summary,created_at,updated_at,metadata) VALUES (?,?,?,?,?,?,?,?,?,?)", ("mem-1", "scope-a", "s-1", "legacy", "fact", "持久化记忆", "持久化记忆", "2026-09-01T00:03:00Z", "2026-09-01T00:03:00Z", '{"journal_entry_ids":[1]}'))
        conn.execute("INSERT INTO memory_journal_sources(memory_id,journal_entry_id,run_id,created_at) VALUES ('mem-1',1,'run-1','2026-09-01T00:03:00Z')")
        conn.execute("INSERT INTO task_episodes(id,scope_id,shared_scope_id,session_id,task_class,task_goal,status,outcome,started_at,journal_entry_ids,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("ep-1", "scope-a", "shared-a", "s-1", "migration", "完成迁移验证", "completed", "ok", "2026-09-01T00:00:00Z", "[1,2,3]", '{"scope_mode":"local"}'))
        facts = [("fact-1", "mem-1", "scope-a", "用户", "保留", "retention", "已确认保留", "已确认保留", "fp-1", "single", "direct", "2026-09-01T00:00:00Z", "current", .95, "", "journal", "1")]
        if include_multivalue:
            facts.extend([
                ("fact-m1", "mem-1", "scope-a", "偏好", "颜色", "colors", "甲", "甲", "fp-a", "multi", "direct", "2026-09-01T00:00:00Z", "current", .8, "", "journal", "1"),
                ("fact-m2", "mem-1", "scope-a", "偏好", "颜色", "colors", "乙", "乙", "fp-b", "multi", "direct", "2026-09-01T00:00:00Z", "current", .8, "", "journal", "3"),
            ])
        for fact in facts:
            conn.execute("INSERT INTO fact_claims(claim_id,memory_id,scope_id,subject_key,predicate_key,fact_key,value,normalized_value,value_fingerprint,cardinality,assertion_kind,recorded_at,status,confidence,superseded_by_claim_id,source_type,source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fact)
        conn.execute("INSERT INTO fact_claim_evidence(evidence_id,claim_id,source_type,source_ref,evidence_hash,excerpt,recorded_at) VALUES (?,?,?,?,?,?,?)", ("ev-1", "fact-1", "journal", "1", "eh-1", "用户已确认保留。", "2026-09-01T00:03:00Z"))
        if include_multivalue:
            conn.execute("INSERT INTO fact_claim_evidence(evidence_id,claim_id,source_type,source_ref,evidence_hash,excerpt,recorded_at) VALUES (?,?,?,?,?,?,?)", ("ev-m1", "fact-m1", "journal", "1", "eh-m1", "多值甲。", "2026-09-01T00:03:00Z"))
            conn.execute("INSERT INTO fact_claim_evidence(evidence_id,claim_id,source_type,source_ref,evidence_hash,excerpt,recorded_at) VALUES (?,?,?,?,?,?,?)", ("ev-m2", "fact-m2", "journal", "3", "eh-m2", "多值乙。", "2026-09-01T00:03:00Z"))
        conn.execute("INSERT INTO fact_action_receipts(action_id,idempotency_key,request_hash,scope_id,requested_action,effective_action,status,applied,receipt_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("action-1", "old-key-1", "old-request-1", "scope-a", "add", "add", "applied", 1, '{"claim_id":"fact-1"}', "2026-09-01T00:03:00Z", "2026-09-01T00:03:00Z"))
        playbook = ("pb-1", "scope-a", "candidate", "离线迁移", "按来源逐项迁移", "migration", "读取来源", "[{\"action\":\"读取来源\"}]", "[{\"check\":\"有证据\"}]", "[]", "[]", "[{\"source_ref\":\"1\",\"excerpt\":\"用户已确认保留。\"}]", "[]", "{}", "{}", "2026-09-01T00:04:00Z")
        conn.execute("INSERT INTO procedural_playbooks(id,scope_id,status,title,goal,task_class,trigger,steps,preconditions,pitfalls,verification,evidence_anchors,related_skills,environment_constraints,reuse_policy,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*playbook, playbook[-1]))
        conn.execute("INSERT INTO playbook_versions(id,playbook_id,version,change_type,snapshot,created_at) VALUES (?,?,?,?,?,?)", ("pbv-1", "pb-1", 1, "create", '{"title":"离线迁移","goal":"按来源逐项迁移","status":"candidate","steps":[{"action":"读取来源"}],"evidence_anchors":[{"source_ref":"1","excerpt":"用户已确认保留。"}]}', "2026-09-01T00:04:00Z"))
        conn.commit()
    finally:
        conn.close()
'''
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([sys.executable, "-c", script, str(target), str(root), LEGACY_COMMIT, "1" if include_multivalue else "0"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr or exc.stdout or "official 578b fixture subprocess failed") from exc
    return target


__all__ = ["LEGACY_COMMIT", "build_official_578b_fixture"]
