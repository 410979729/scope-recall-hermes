"""P13 TEST-only before/after evidence for the related-candidate SQL."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".execution" / "TEST-P13-PREP-v4" / "core-10k-clean" / "memory.sqlite3"
DIAGNOSTIC = ROOT / ".execution" / "TEST-P13-PREP-v5" / "diagnose-fixed.stdout-stderr.txt"
OUT = ROOT / ".execution" / "TEST-P13-PREP-v5" / "related-plan.stdout.json"
LIMIT = 24

OLD_SQL = """SELECT object_kind,object_ref,object_revision FROM evidence_links
               WHERE source_ref=? UNION SELECT 'event',source_ref,source_revision
               FROM evidence_links WHERE object_ref=?
               UNION SELECT dependency_kind,dependency_ref,dependency_revision
               FROM object_dependencies WHERE object_kind=? AND object_ref=?
               UNION SELECT object_kind,object_ref,object_revision
               FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
               ORDER BY object_kind,object_ref,object_revision LIMIT ?"""
NEW_SQL = """SELECT object_kind,object_ref,object_revision FROM evidence_links
               WHERE source_ref=? UNION SELECT 'event',source_ref,source_revision
               FROM evidence_links WHERE object_kind=? AND object_ref=?
               UNION SELECT dependency_kind,dependency_ref,dependency_revision
               FROM object_dependencies WHERE object_kind=? AND object_ref=?
               UNION SELECT object_kind,object_ref,object_revision
               FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
               ORDER BY object_kind,object_ref,object_revision LIMIT ?"""


def candidate_keys() -> list[tuple[str, str]]:
    payload = json.loads(DIAGNOSTIC.read_text(encoding="utf-8").splitlines()[0])
    keys: set[tuple[str, str]] = set()
    for sample in payload["hot"]:
        for raw in sample["actual_refs"]:
            ref = raw.rsplit("@", 1)[0]
            kind = ref.split("-", 1)[0]
            keys.add((kind, ref))
    return sorted(keys)


def old_params(kind: str, ref: str) -> tuple[object, ...]:
    return (ref, ref, kind, ref, kind, ref, LIMIT)


def new_params(kind: str, ref: str) -> tuple[object, ...]:
    return (ref, kind, ref, kind, ref, kind, ref, LIMIT)


def main() -> int:
    keys = candidate_keys()
    with sqlite3.connect(DB) as connection:
        connection.row_factory = sqlite3.Row
        plans = {
            "old": [dict(row) for row in connection.execute("EXPLAIN QUERY PLAN " + OLD_SQL, old_params(*keys[0])).fetchall()],
            "new": [dict(row) for row in connection.execute("EXPLAIN QUERY PLAN " + NEW_SQL, new_params(*keys[0])).fetchall()],
        }
        outputs: dict[str, list[list[tuple[object, ...]]]] = {"old": [], "new": []}
        timings: dict[str, float] = {}
        for name, sql, params in (
            ("old", OLD_SQL, old_params),
            ("new", NEW_SQL, new_params),
        ):
            started = time.perf_counter()
            for kind, ref in keys:
                outputs[name].append([tuple(row) for row in connection.execute(sql, params(kind, ref)).fetchall()])
            timings[name] = time.perf_counter() - started
    result = {
        "schema": "p13.related-plan.v5",
        "candidate_count": len(keys),
        "candidate_keys": keys,
        "plans": plans,
        "batch_timings_seconds": timings,
        "outputs_equal": outputs["old"] == outputs["new"],
        "output_rows": {"old": sum(map(len, outputs["old"])), "new": sum(map(len, outputs["new"]))},
        "note": "only evidence_links object_kind restriction differs; all other UNIONs and LIMIT are unchanged",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
