"""Global mapped candidates and full float32 scoring under one raw SQLite snapshot.

Approved prefixes select candidates only. Every returned distance uses the
original full vector. Rows beyond the cache watermark join the same ranking
through a bounded exact scan.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

import numpy as np

from .cache import MappedIndex, MappedScope, check_deadline

BATCH_ROWS = 512
MIN_CANDIDATES = 256
COARSE_BATCH_ROWS = 8192


def as_vector(values: Any, dimensions: int) -> np.ndarray:
    """Validate a finite vector and return its lossless little-endian float32 representation."""
    vector = np.asarray(values, dtype="<f4")
    if vector.shape != (dimensions,) or not np.isfinite(vector).all():
        raise ValueError("vector dimensions or finite values are invalid")
    return vector


def _distances(matrix: np.ndarray, query: np.ndarray, metric: str, *, norms: np.ndarray | None = None) -> np.ndarray:
    if metric in {"l2", "euclidean"}:
        return np.linalg.norm(matrix - query, axis=1)
    dots = np.einsum("ij,j->i", matrix, query, optimize=False, dtype=np.float64)
    if metric in {"dot", "inner_product"}:
        return 1.0 - dots
    if norms is None:
        norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix, dtype=np.float64))
    denominators = np.asarray(norms, dtype=np.float64) * float(
        np.sqrt(np.einsum("i,i->", query, query, dtype=np.float64))
    )
    similarities = np.divide(dots, denominators, out=np.zeros_like(dots), where=denominators > 0)
    return np.clip(1.0 - similarities, 0.0, 2.0)


def _rank(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows.sort(key=lambda item: str(item["id"]))
    rows.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    rows.sort(key=lambda item: item["_distance"])
    return rows[:limit]


def scan(
    conn: sqlite3.Connection, query: np.ndarray, where: str, params: Iterable[Any], *, limit: int, metric: str
) -> list[dict[str, Any]]:
    """Read bounded pages, keeping only exact nearest candidates in application memory."""
    cursor = conn.execute(
        f"SELECT id,scope_id,source,target,updated_at,vector FROM vector_records WHERE {where}", tuple(params)
    )
    best: list[dict[str, Any]] = []
    while batch := cursor.fetchmany(BATCH_ROWS):
        check_deadline()
        matrix = np.frombuffer(b"".join(row[5] for row in batch), dtype="<f4").reshape(-1, len(query))
        distances = _distances(matrix, query, metric)
        cutoff = np.partition(distances, min(limit, len(batch)) - 1)[min(limit, len(batch)) - 1]
        for index in np.flatnonzero(distances <= cutoff):
            row = batch[int(index)]
            best.append(
                dict(
                    id=row[0],
                    scope_id=row[1],
                    source=row[2],
                    target=row[3],
                    updated_at=row[4],
                    content="",
                    summary="",
                    _distance=float(distances[index]),
                )
            )
        best = _rank(best, limit)
    return best


def _coarse_scores(parts: list[MappedScope], query: np.ndarray) -> np.ndarray:
    """Scan only authorized partitions into one global score array."""
    scores = np.empty(sum(part.count for part in parts), dtype=np.float64)
    if not parts:
        return scores
    width = parts[0].coarse.shape[1]
    prefix = width < len(query)
    coarse_query = query[:width].astype(np.float64)
    norm = float(np.sqrt(np.einsum("i,i->", coarse_query, coarse_query)))
    coarse_query = (coarse_query / norm).astype(np.float32) if norm else np.zeros(width, dtype=np.float32)
    offset = 0
    for part in parts:
        for begin in range(0, part.count, COARSE_BATCH_ROWS):
            check_deadline()
            end = min(begin + COARSE_BATCH_ROWS, part.count)
            if prefix:
                found = np.einsum("ij,j->i", part.coarse[begin:end], coarse_query, dtype=np.float32, optimize=False)
            else:
                found = 1.0 - _distances(part.raw[begin:end], query, "cosine", norms=part.norms[begin:end])
            scores[offset + begin : offset + end] = found
        offset += part.count
    return scores


def _live_groups(conn: sqlite3.Connection, parts: list[MappedScope], selected: np.ndarray):
    """Validate cached sequence keys through the raw covering index, without reading BLOBs."""
    groups = []
    offset = 0
    for part in parts:
        rows = selected[(selected >= offset) & (selected < offset + part.count)] - offset
        kept = []
        for start in range(0, len(rows), BATCH_ROWS):
            check_deadline()
            page = rows[start : start + BATCH_ROWS]
            seqs = [int(seq) for seq in part.seqs[page]]
            marks = ",".join("?" for _ in seqs)
            live = {
                int(row[0])
                for row in conn.execute(
                    f"SELECT seq FROM vector_records WHERE scope_id=? AND seq IN ({marks})", (part.scope, *seqs)
                )
            }
            kept.extend(int(row) for row, seq in zip(page, seqs, strict=True) if seq in live)
        if kept:
            groups.append((part, np.asarray(kept, dtype=np.int64)))
        offset += part.count
    return groups


def _candidate_groups(conn: sqlite3.Connection, parts: list[MappedScope], query: np.ndarray, limit: int):
    """Expand one global cutoff past stale rows and retain its entire boundary tie group."""
    scores = _coarse_scores(parts, query)
    if not len(scores):
        return []
    target = min(len(scores), max(MIN_CANDIDATES, limit))
    take = target
    while True:
        check_deadline()
        boundary = np.partition(scores, len(scores) - take)[len(scores) - take]
        selected = np.flatnonzero(scores >= boundary - 1e-6)
        groups = _live_groups(conn, parts, selected)
        if sum(len(rows) for _, rows in groups) >= target or take == len(scores):
            return groups
        take = min(len(scores), max(take + MIN_CANDIDATES, take * 2))


def _mapped_distances(groups, query: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Read original float32 rows by position and accumulate full cosine scores in float64."""
    seqs, distances = [], []
    for part, rows in groups:
        for start in range(0, len(rows), BATCH_ROWS):
            check_deadline()
            page = rows[start : start + BATCH_ROWS]
            seqs.append(np.asarray(part.seqs[page]))
            distances.append(_distances(part.raw[page], query, "cosine", norms=part.norms[page]))
    if not seqs:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    return np.concatenate(seqs), np.concatenate(distances)


def _metadata(conn: sqlite3.Connection, seqs: np.ndarray, distances: np.ndarray, tail, limit: int):
    """Fetch only possible final winners, resolving complete exact-score ties by metadata."""
    all_distances = np.concatenate((distances, np.asarray([row["_distance"] for row in tail])))
    count = min(limit, len(all_distances))
    if not count:
        return []
    boundary = np.partition(all_distances, count - 1)[count - 1]
    chosen = np.flatnonzero(distances <= boundary)
    best = [row for row in tail if row["_distance"] <= boundary]
    for start in range(0, len(chosen), BATCH_ROWS):
        check_deadline()
        page = chosen[start : start + BATCH_ROWS]
        wanted = {int(seqs[i]): float(distances[i]) for i in page}
        marks = ",".join("?" for _ in wanted)
        for row in conn.execute(
            f"SELECT seq,id,scope_id,source,target,updated_at FROM vector_records WHERE seq IN ({marks})", tuple(wanted)
        ):
            best.append(
                dict(
                    id=row[1],
                    scope_id=row[2],
                    source=row[3],
                    target=row[4],
                    updated_at=row[5],
                    content="",
                    summary="",
                    _distance=wanted[row[0]],
                )
            )
        best = _rank(best, limit)
    return _rank(best, limit)


def search(
    conn: sqlite3.Connection,
    values: Any,
    scopes: list[str],
    *,
    dimensions: int,
    metric: str,
    limit: int,
    index: MappedIndex,
) -> list[dict[str, Any]]:
    """Merge mapped candidates and every raw row newer than the committed watermark."""
    query = as_vector(values, dimensions)
    if limit <= 0 or not scopes:
        return []
    marks = ",".join("?" for _ in scopes)
    where = f"scope_id IN ({marks})"
    view = index.view(conn, scopes) if metric == "cosine" else None
    if view is None:
        return scan(conn, query, where, scopes, limit=limit, metric=metric)
    state, parts = view
    groups = _candidate_groups(conn, parts, query, limit)
    seqs, distances = _mapped_distances(groups, query)
    tail = scan(conn, query, where + " AND seq>?", [*scopes, state.watermark], limit=limit, metric=metric)
    return _metadata(conn, seqs, distances, tail, limit)
