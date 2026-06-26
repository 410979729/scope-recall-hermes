from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from scope_recall import doctor_sqlite  # type: ignore[attr-defined]


def test_sqlite_report_opens_truth_db_read_only(tmp_path, monkeypatch):
    db_dir = tmp_path / "scope-recall"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real_connect = sqlite3.connect

    def capture_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append((database, args, kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(doctor_sqlite.sqlite3, "connect", capture_connect)

    payload, check, recommendations = doctor_sqlite.sqlite_report(Path(tmp_path))

    assert payload["status"] == "ready"
    assert check == {"ok": True, "failures": []}
    assert recommendations == []
    assert calls
    database, _args, kwargs = calls[0]
    assert str(database) == f"file:{db_path}?mode=ro"
    assert kwargs.get("uri") is True
