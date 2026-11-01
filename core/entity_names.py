"""Indexed name projections and name-level identity judgement receipts."""

from __future__ import annotations

import json
from difflib import SequenceMatcher

from ..contracts import ContractError, utc_timestamp
from .aliases import ENTITY_KINDS
from .fact_identity import normalize_fact_component


def audience(context, scope):
    """Encode the exact audience identically to SQLite's hex(json_array(...))."""
    return (
        json.dumps([scope, context.project_id, context.branch_id], ensure_ascii=False, separators=(",", ":"))
        .encode()
        .hex()
        .upper()
    )


def grams(name):
    """Encode Unicode character grams as tokenizer-independent FTS terms."""
    normalized = normalize_fact_component(name, max_chars=240, allow_empty=True)
    return tuple(
        dict.fromkeys(
            "g" + normalized[i : i + size].encode().hex()
            for size in (3, 2, 1)
            for i in range(len(normalized) - size + 1)
        )
    )


def query_keys(text, matches=(), *, identifiers=True):
    """Probe complete Latin identifiers and bounded Chinese spans; overflow preserves literal lookup."""
    spans = [(i, i + len(name)) for name in matches for i in range(len(text)) if text.startswith(name, i)]
    keys, steps = set(), 0
    for start in range(len(text)):
        if (
            identifiers
            and text[start].isascii()
            and text[start].isalnum()
            and start
            and text[start - 1].isascii()
            and (text[start - 1].isalnum() or text[start - 1] in "_-/\\.")
        ):
            continue
        for end in range(start + 1, min(len(text), start + 240) + 1):
            steps += 1
            if steps > 8192:
                return None
            if (
                identifiers
                and text[end - 1].isascii()
                and text[end - 1].isalnum()
                and end < len(text)
                and text[end].isascii()
                and (text[end].isalnum() or text[end] in "_-/\\.")
            ):
                continue
            if not spans or any(start <= left and end >= right for left, right in spans):
                keys.add(text[start:end])
    return tuple(sorted(keys))


def visible_audiences(context):
    """Enumerate the exact namespace keys admitted by inherited project/branch visibility."""
    return [
        (scope, project, branch)
        for scope in sorted(context.allowed_scope_ids)
        for project in {None, context.project_id}
        for branch in {None, context.branch_id}
    ]


def index_claim(tx, version):
    """Maintain name mentions atomically with a fact head; repeated names keep their receipt."""
    if version.payload["kind"] == "alias" or version.current_revision != version.revision:
        return
    conn = tx._check(write=True)
    conn.execute("UPDATE claims SET entity_indexed=1 WHERE claim_id=?", (version.ref,))
    if conn.execute(
        "SELECT 1 FROM entity_name_mentions WHERE claim_ref=? AND claim_revision=? LIMIT 1",
        (version.ref, version.revision),
    ).fetchone():
        return
    conn.execute("DELETE FROM entity_name_mentions WHERE claim_ref=?", (version.ref,))
    if version.payload["kind"] not in ENTITY_KINDS or version.state != "active" or version.suppressed:
        return
    ordinal = conn.execute("SELECT rowid FROM claims WHERE claim_id=?", (version.ref,)).fetchone()[0]
    usable = int(not version.payload.get("conditions") and not version.payload["subject"].startswith("principal:"))
    for name in sorted({version.payload["subject"], *version.payload.get("entity_names", [])}):
        if not name or len(name) > 240:
            continue
        key = audience(tx.context, version.scope_id)
        existing = conn.execute(
            "SELECT name_id,terms FROM entity_names WHERE audience=? AND name=?", (key, name)
        ).fetchone()
        if existing is None:
            name_id = conn.execute(
                "INSERT INTO entity_names(audience,name,normalized,terms) VALUES (?,?,?,?) RETURNING name_id",
                (
                    key,
                    name,
                    normalize_fact_component(name),
                    " ".join(grams(name)) if name == version.payload["subject"] else "",
                ),
            ).fetchone()[0]
        else:
            name_id = existing[0]
            if name == version.payload["subject"] and not existing[1]:
                conn.execute("UPDATE entity_names SET terms=? WHERE name_id=?", (" ".join(grams(name)), name_id))
        conn.execute(
            "INSERT INTO entity_name_mentions VALUES (?,?,?,?,?,?)",
            (name_id, version.ref, version.revision, usable, ordinal, int(name == version.payload["subject"])),
        )


def pending_names(tx, source):
    """Read only the names of this fact which have no current judgement receipt."""
    return tuple(
        row[0]
        for row in tx._check().execute(
            "SELECT n.name FROM entity_name_mentions m JOIN entity_names n USING(name_id) "
            "WHERE m.claim_ref=? AND m.claim_revision=? AND n.status='pending' ORDER BY n.name",
            (source.ref, source.revision),
        )
    )


def backfill_names(tx, scope, *, limit=7):
    """Index a bounded page in the exact namespace before any negative identity decision."""
    conn = tx._check(write=True)
    query = (
        "SELECT claim_id,current_revision FROM claims WHERE scope_id=? AND project_id IS ? AND branch_id IS ? "
        "AND entity_indexed=0 AND kind IN ('fact','preference','constraint','decision','intention') "
        "AND read_blocked=0 AND suppressed=0 ORDER BY claim_id LIMIT ?"
    )
    namespace = (scope, tx.context.project_id, tx.context.branch_id)
    for ref, revision in conn.execute(query, (*namespace, limit)).fetchall():
        version = tx.claims.version(ref, revision)
        if version is None:
            conn.execute("UPDATE claims SET entity_indexed=1 WHERE claim_id=?", (ref,))
        else:
            index_claim(tx, version)
    return conn.execute(query, (*namespace, 1)).fetchone() is None


def reserve_names(tx, source, item, now):
    """Reuse the projection lease to prevent concurrent facts paying for the same name."""
    conn = tx._check(write=True)
    if not tx.work._verify_lease(*item.lease, now=now):
        raise ContractError("VERSION_CONFLICT", "entity_lease")
    blocked = conn.execute(
        "SELECT 1 FROM entity_name_mentions m JOIN entity_names n USING(name_id) "
        "JOIN work_items w ON w.work_id=n.work_id "
        "WHERE m.claim_ref=? AND n.status='pending' AND w.work_id!=? "
        "AND (w.state IN ('pending','failed') OR (w.state='leased' AND w.lease_until>?)) LIMIT 1",
        (source.ref, item.work_id, utc_timestamp(now)),
    ).fetchone()
    if blocked:
        raise ContractError("VERSION_CONFLICT", "entity_name_lease")
    conn.execute(
        "UPDATE entity_names SET work_id=? WHERE status='pending' AND name_id IN "
        "(SELECT name_id FROM entity_name_mentions WHERE claim_ref=?)",
        (item.work_id, source.ref),
    )


def checked(tx, source, name, target=None, alias=None):
    """Pin one name's decision to its evidence endpoints without overriding an owner's withdrawal."""
    tx._check(write=True).execute(
        "UPDATE entity_names SET status='checked',work_id=NULL,source_ref=?,source_revision=?,"
        "target_ref=?,target_revision=?,"
        "alias_ref=?,alias_revision=? WHERE audience=? AND name=? AND status!='withdrawn'",
        (
            source.ref,
            source.revision,
            target.ref if target else None,
            target.revision if target else None,
            alias.ref if alias else None,
            alias.revision if alias else None,
            audience(tx.context, source.scope_id),
            name,
        ),
    )


def ineligible(tx, ref):
    """Exclude an inadmissible mention and hand a pending name to its next usable fact."""
    conn = tx._check(write=True)
    conn.execute("UPDATE entity_name_mentions SET usable=0 WHERE claim_ref=?", (ref,))
    conn.execute(
        "UPDATE entity_names SET status='pending' WHERE status='pending' AND name_id IN "
        "(SELECT name_id FROM entity_name_mentions WHERE claim_ref=?)",
        (ref,),
    )


def candidate_refs(tx, scope, name, vector_refs, evidence):
    """Score at most 128 indexed names, retaining one representative per distinct subject."""
    tx._scope(scope)
    conn, key = tx._check(), audience(tx.context, scope)
    terms = grams(name)
    stride = max(1, (len(terms) + 63) // 64)
    query = "audience:" + key + " AND terms:(" + " OR ".join(terms[::stride][:64]) + ")"
    lexical = (
        tuple(
            conn.execute(
                "SELECT n.name_id FROM entity_name_search f JOIN entity_names n ON n.name_id=f.rowid "
                "WHERE entity_name_search MATCH ? AND n.audience=? AND EXISTS(SELECT 1 FROM entity_name_mentions m "
                "WHERE m.name_id=n.name_id AND m.usable=1 AND m.subject_name=1) "
                "ORDER BY bm25(entity_name_search) LIMIT 128",
                (query, key),
            )
        )
        if terms
        else ()
    )
    # Evidence only nominates candidates; sentence punctuation must not hide a name.
    keys = query_keys(evidence, identifiers=False)
    mentioned = (
        tuple(
            conn.execute(
                "SELECT name_id FROM entity_names WHERE audience=? AND name IN "
                "(SELECT value FROM json_each(?)) AND EXISTS(SELECT 1 FROM entity_name_mentions m "
                "WHERE m.name_id=entity_names.name_id AND m.usable=1 AND m.subject_name=1) LIMIT 32",
                (key, json.dumps(keys)),
            )
        )
        if keys
        else ()
    )
    vectors = tuple(
        conn.execute(
            "SELECT c.claim_id,c.subject FROM json_each(?) r JOIN claims c ON c.claim_id=r.value "
            "WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ? AND c.read_blocked=0 AND c.suppressed=0 "
            "ORDER BY cast(r.key AS INTEGER) LIMIT 64",
            (json.dumps(vector_refs[:64]), scope, tx.context.project_id, tx.context.branch_id),
        )
    )
    vector_names = {}
    for ref, subject in vectors:
        if subject != name and len(subject) <= 240:
            vector_names.setdefault(subject, ref)
    ids = tuple(dict.fromkeys(row[0] for row in (*mentioned, *lexical)))[: 128 - len(vector_names)]
    entries = list(
        conn.execute(
            "SELECT n.name_id,n.name,n.normalized,c.claim_id,c.subject FROM json_each(?) r "
            "JOIN entity_names n ON n.name_id=r.value JOIN claims c ON c.claim_id=(SELECT m.claim_ref "
            "FROM entity_name_mentions m WHERE m.name_id=n.name_id AND m.usable=1 AND m.subject_name=1 "
            "ORDER BY m.ordinal,m.claim_ref LIMIT 1) WHERE c.read_blocked=0 AND c.suppressed=0",
            (json.dumps(ids),),
        )
    )
    entries.extend(
        (None, subject, normalize_fact_component(subject), ref, subject) for subject, ref in vector_names.items()
    )
    explicit = {row[0] for row in mentioned}
    normalized = normalize_fact_component(name)
    ranked = []
    for identity, _label, canonical, ref, subject in entries:
        if subject == name:
            continue
        score = SequenceMatcher(None, normalized, canonical, autojunk=False).ratio()
        if score >= 0.45 or identity in explicit or subject in vector_names:
            ranked.append((identity in explicit, score, subject in vector_names, subject, ref))
    chosen = {}
    for _explicit, _score, _vector, subject, ref in sorted(ranked, reverse=True):
        chosen.setdefault(subject, ref)
    return tuple(chosen.values())[:16]
