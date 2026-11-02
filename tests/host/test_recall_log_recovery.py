"""Crash recovery for the independent diagnostic database using owned child processes."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scope_recall.core.recall_log_storage import RecallLogStore


def test_killed_log_transaction_recovers_without_losing_committed_history(tmp_path):
    path = tmp_path / "recall.sqlite3"
    store = RecallLogStore(path)
    record = dict(
        ref="TEST-committed",
        timestamp="2026-09-01T12:00:00Z",
        installation_id="TEST",
        session_id="TEST",
        request_id="TEST",
    )
    store.write(record)
    child_code = r"""
import importlib.util,json,sqlite3,sys,time
from pathlib import Path
repo=Path(sys.argv[1]);path=Path(sys.argv[2])
spec=importlib.util.spec_from_file_location('scope_recall',repo/'__init__.py',submodule_search_locations=[str(repo)])
module=importlib.util.module_from_spec(spec);sys.modules['scope_recall']=module;spec.loader.exec_module(module)
from scope_recall.core.recall_log_storage import RecallLogStore
original=sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self,sql,*args):
        if sql=='PRAGMA cache_spill=OFF':sql='PRAGMA cache_spill=1'
        if sql=='BEGIN IMMEDIATE':super().execute('PRAGMA cache_size=1')
        result=super().execute(sql,*args)
        if sql.startswith('INSERT OR REPLACE INTO attempts('):
            active=self.in_transaction
            super().execute('PRAGMA cache_size=1')
            super().execute('PRAGMA cache_spill=ON')
            for i in range(1024):
                super().execute('INSERT INTO attempts(ref,timestamp,installation_id,session_id,request_id,payload) '
                    'VALUES(?,?,?,?,?,?)',
                    ('TEST-uncommitted-'+str(i),'2026-09-02T12:00:00Z','TEST','TEST','TEST','x'*4096))
            print(json.dumps(dict(in_transaction=active,journal=self.execute('PRAGMA journal_mode').fetchone()[0],
                synchronous=self.execute('PRAGMA synchronous').fetchone()[0],
                wal_bytes=Path(str(path)+'-wal').stat().st_size)),flush=True)
            sys.stdin.readline()
        return result
sqlite3.connect=lambda *a,**kw:original(*a,**dict(kw,factory=Connection))
RecallLogStore(path).write(dict(ref='TEST-in-flight',timestamp='2026-09-02T12:00:00Z',installation_id='TEST',session_id='TEST',request_id='TEST'))
"""
    child = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", child_code, str(Path(__file__).resolve().parents[2]), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    try:
        state = json.loads(child.stdout.readline())
        assert state["in_transaction"] and state["journal"] == "wal" and state["synchronous"] == 1
        assert state["wal_bytes"] > 0
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    with sqlite3.connect(path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
    assert integrity == "ok" and count == 1
    (tmp_path / "crash-receipt.json").write_text(
        json.dumps(dict(state, child_exited=child.poll() is not None, integrity=integrity, committed_rows=count)),
        encoding="utf-8",
    )
