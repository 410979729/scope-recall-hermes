"""Table-driven synchronous screening and content-free quarantine visibility.

Screen the whole captured message, including boundaries between its segments.
A blocked version withholds its identity until every held version is reviewed;
changing the source revision cannot silently bypass an outstanding decision.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ScanResult:
    """A content-free decision; rule identities are retained for owner review."""

    state: str
    rules: tuple[str, ...] = ()


@lru_cache(maxsize=256)
def _compiled(pattern):
    return re.compile(pattern, re.IGNORECASE)


def _matches(pattern, text, normalized) -> bool:
    """Apply each data rule to the original text and its compatibility-folded form."""
    compiled = _compiled(pattern)
    return bool(compiled.search(text) or (normalized != text and compiled.search(normalized)))


def scan(tx, event) -> ScanResult:
    """Read active SQLite rules and screen raw Unicode before compatibility folding.

    Default regexes use bounded spans. Emoji and joining-script controls remain
    valid text; controls embedded in English or Chinese words are held.
    Invalid rule data fails closed and never grants content visibility.
    """
    rules = (
        tx._check()
        .execute("SELECT rule_id,matcher,pattern,disposition FROM injection_rules WHERE enabled=1 ORDER BY rule_id")
        .fetchall()
    )
    text = event["content"]
    metadata = {k: v for k, v in event.items() if k != "content"}
    text += "\n" + json.dumps(metadata, ensure_ascii=False)
    if len(text) > 2097152:
        return ScanResult("pending_review", ("scan_input_budget",))
    normalized = unicodedata.normalize("NFKC", text)
    matches = []
    states = set()
    for rule in rules:
        try:
            if rule["matcher"] == "origin":
                origin = event["origin"]
                if origin == "imported":
                    origin = (
                        event.get("source_original_origin", "origin_unknown")
                        if tx.context.import_provenance is not None
                        else "origin_unknown"
                    )
                hit = origin == rule["pattern"]
            else:
                hit = _matches(rule["pattern"], text, normalized)
        except (re.error, ValueError):
            return ScanResult("pending_review", ("invalid_scan_rule",))
        if hit:
            matches.append(rule["rule_id"])
            states.add(rule["disposition"])
    if "quarantined" in states:
        return ScanResult("quarantined", tuple(matches))
    return ScanResult("pending_review" if states else "clear", tuple(matches))


def screen_source(tx, source, prepared, now, scans) -> bool:
    """Persist the whole-message decision before capture can index or schedule a part."""
    if "message" not in scans:
        first = prepared.events[0]
        segment = first.get("segment")
        if segment and segment["total"] != len(prepared.events):
            scans["message"] = ScanResult("pending_review", ("incomplete_scan_group",))
        else:
            scans["message"] = scan(tx, dict(first, content="".join(e["content"] for e in prepared.events)))
    result = scans["message"]
    if result.state == "clear":
        evidence = tuple(ref.rsplit("@", 1)[0] for event in prepared.events for ref in event.get("evidence_refs", ()))
        if blocked_refs(tx, "event", evidence):
            result = scans["message"] = ScanResult("pending_review", ("held_evidence",))
    if result.state == "clear":
        if source.ref not in blocked_refs(tx, "event", (source.ref,)):
            return True
        result = ScanResult("pending_review", ("held_source_revision",))
    tx._check(write=True).execute(
        "INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at) VALUES (?,?,?,?,?)",
        (source.ref, source.revision, result.state, json.dumps(result.rules), now),
    )
    return False


def blocked_refs(tx, kind, refs) -> frozenset[str]:
    """Return held identities or objects depending on them, for every content exit."""
    wanted = tuple(dict.fromkeys(refs))
    if not wanted:
        return frozenset()
    rows = tx._check().execute(
        """WITH RECURSIVE lineage(root,kind,ref) AS (
            SELECT value,?,value FROM json_each(?) WHERE EXISTS (
                SELECT 1 FROM source_safety s JOIN source_events e
                ON e.event_id=s.source_ref AND e.source_revision=s.source_revision
                WHERE s.state IN ('quarantined','pending_review') AND e.read_blocked=0)
            UNION
            SELECT l.root,d.dependency_kind,d.dependency_ref FROM lineage l JOIN object_dependencies d
            ON d.object_kind=l.kind AND d.object_ref=l.ref UNION
            SELECT l.root,'event',e.source_ref FROM lineage l JOIN evidence_links e
            ON e.object_kind=l.kind AND e.object_ref=l.ref)
        SELECT DISTINCT l.root FROM lineage l JOIN source_safety s ON l.kind='event' AND s.source_ref=l.ref
        WHERE s.state!='released'""",
        (kind, json.dumps(wanted)),
    )
    return frozenset(row[0] for row in rows)


def notice(tx, ref, revision=None):
    """Return only a held marker and rule names inside normal scope/deletion boundaries."""
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = (
        tx._check()
        .execute(
            f"""SELECT s.* FROM source_safety s JOIN source_events e
        ON e.event_id=s.source_ref AND e.source_revision=s.source_revision
        WHERE s.source_ref=? AND s.state!='released' AND (? IS NULL OR s.source_revision=?)
        AND e.read_blocked=0 AND e.scope_id IN ({",".join("?" for _ in scopes)})
        AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
        AND NOT EXISTS(SELECT 1 FROM object_blocks b
            WHERE b.object_kind='event' AND b.object_ref=e.event_id AND b.read_blocked=1)
        AND NOT EXISTS(SELECT 1 FROM restored_absence_blocks a WHERE a.object_kind='event' AND a.object_ref=e.event_id)
        ORDER BY s.source_revision DESC LIMIT 1""",
            (ref, revision, revision, *scopes, tx.context.project_id, tx.context.branch_id),
        )
        .fetchone()
    )
    if rows is None:
        return None
    return dict(state=rows["state"], rules=json.loads(rows["rules_json"]), revision=rows["source_revision"])
