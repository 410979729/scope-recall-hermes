"""Worker-published scoped arrays derived exclusively from raw SQLite vectors.

Files have immutable committed prefixes. The worker fsyncs appended bytes
before publishing counts and a watermark in one SQLite transaction. Readers
map only those counts; a failed publication leaves an ignored tail which the
next pass overwrites. No truncation of a file with active Windows mappings is
needed. Generations separate rebuilds from readers holding an older snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from ..core.deadline import remaining_seconds
from .defaults import INDEX_BATCH_ROWS, INDEX_PAGE_ROWS, MIN_CANDIDATES, compute_threads

FORMAT = "mapped-float32-sha256"
PAGE_ROWS = 512
VERIFY_PAGES = 8
VERIFY_SECONDS = 3600.0
COLLECT_FILES = 64
COLLECT_RETRY_SECONDS = 60.0
# Windows junctions can retain directory mode while exposing a reparse point.
_REPARSE_POINT = 0x400
_GENERATION = re.compile(r"[0-9a-f]{32}")
_OWNED_FILE = re.compile(
    r"(?:[0-9a-f]{32}-[0-9a-f]{64}\.(?:raw\.f32|prefix\.f32|norms\.f64|seqs\.i64)|[0-9a-f]{32}\.invalid)"
)


class CacheCorrupt(ValueError):
    """Derived bytes disagree with their finite-value or authoritative-score contract."""


def check_deadline() -> None:
    """Stop bounded vector work when the current request has no time left."""
    left = remaining_seconds()
    if left is not None and left <= 0:
        raise TimeoutError("vector search deadline exhausted")


@cache
def _capability_table() -> dict[str, Any]:
    try:
        value = json.loads(Path(__file__).with_name("model-capabilities.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def space_capabilities(descriptor: dict) -> dict[str, Any]:
    """Return approved truncation data without changing the embedding-space identity."""
    model, dimensions = descriptor.get("model"), descriptor.get("dimensions")
    data = _capability_table().get(model, {}) if isinstance(model, str) else {}
    native = data.get("native_dimensions")
    prefixes = data.get("prefix_dimensions", ())
    if type(native) is not int or type(dimensions) is not int or dimensions > native:
        return {}
    return {
        "native_dimensions": native,
        "prefix_dimensions": [n for n in prefixes if type(n) is int and 0 < n < native],
    }


def coarse_dimensions(conn: sqlite3.Connection, dimensions: int) -> int:
    """Use a prefix only when every registered space records and approves it."""
    try:
        records = conn.execute("SELECT descriptor,capabilities FROM embedding_spaces").fetchall()
        common: set[int] | None = None
        for encoded, recorded in records:
            descriptor, declared = json.loads(encoded), json.loads(recorded)
            if descriptor.get("dimensions") != dimensions:
                return dimensions
            approved = set(space_capabilities(descriptor).get("prefix_dimensions", ()))
            allowed = approved & set(declared.get("prefix_dimensions", ()))
            common = allowed if common is None else common & allowed
        return min((n for n in common or () if 0 < n < dimensions), default=dimensions)
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return dimensions


def cache_directory(db_path: Path) -> Path:
    """Locate the removable derived directory beside its authoritative companion."""
    return db_path.with_name(db_path.name + ".cache")


@dataclass(frozen=True)
class CacheState:
    """One committed layout and the raw sequence watermark it covers."""

    generation: str
    dimensions: int
    coarse_dimensions: int
    watermark: int
    rows: int

    def layout(self) -> tuple[tuple[str, str, int], ...]:
        """Give the on-disk dtype and components per row for each owned file."""
        base = (("raw.f32", "<f4", self.dimensions), ("norms.f64", "<f8", 1), ("seqs.i64", "<i8", 1))
        return (
            base + (("prefix.f32", "<f4", self.coarse_dimensions),)
            if self.coarse_dimensions < self.dimensions
            else base
        )


class MappedScope:
    """Read-only arrays for exactly the row count visible in a raw SQLite snapshot."""

    def __init__(self, root: Path, state: CacheState, scope: str, key: str, count: int) -> None:
        """Open regular files without mutating permissions or mapping unpublished tails."""
        self.scope, self.key, self.count = scope, key, count
        self.watermark = state.watermark
        self.arrays: dict[str, np.memmap] = {}
        try:
            for suffix, dtype, width in state.layout():
                path = root / f"{key}.{suffix}"
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    _regular_file(stream.fileno(), path, mutable=False)
                    if os.fstat(stream.fileno()).st_size < count * width * np.dtype(dtype).itemsize:
                        raise ValueError("mapped vector file is incomplete")
                    shape = (count, width) if suffix in {"raw.f32", "prefix.f32"} else (count,)
                    self.arrays[suffix] = np.memmap(stream, mode="r", dtype=dtype, shape=shape)
        except BaseException:
            self.close()
            raise
        self.raw = self.arrays["raw.f32"]
        self.norms = self.arrays["norms.f64"]
        self.seqs = self.arrays["seqs.i64"]
        self.coarse = self.arrays.get("prefix.f32", self.raw)

    def close(self) -> None:
        """Release only this scope's owned read mappings."""
        for array in self.arrays.values():
            mapping = getattr(array, "_mmap", None)
            if mapping is not None:
                mapping.close()
        self.arrays.clear()


def _regular_file(descriptor: int, path: Path, *, mutable: bool) -> None:
    info, named = os.fstat(descriptor), os.lstat(path)
    reparse = getattr(named, "st_file_attributes", 0) & _REPARSE_POINT
    if (
        not stat.S_ISREG(info.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or reparse
        or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise ValueError("mapped vector cache requires regular files")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        if not mutable:
            raise ValueError("mapped vector cache requires owner-only files")
        os.fchmod(descriptor, 0o600)


def _file_key(generation: str, scope: str) -> str:
    return generation + "-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def _state(conn: sqlite3.Connection, dimensions: int) -> CacheState | None:
    meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
    if meta.get("cache_format") != FORMAT or not _GENERATION.fullmatch(meta.get("cache_generation", "")):
        return None
    coarse = int(meta["cache_coarse_dimensions"])
    if int(meta.get("cache_dimensions", 0)) != dimensions or not 0 < coarse <= dimensions:
        return None
    watermark, rows = int(meta.get("index_watermark", 0)), int(meta["cache_rows"])
    if not 0 <= rows <= watermark:
        return None
    return CacheState(meta["cache_generation"], dimensions, coarse, watermark, rows)


def _directory(root: Path, *, create: bool = False) -> None:
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise ValueError("mapped vector cache directory is unsafe")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        if not create:
            raise ValueError("mapped vector cache requires an owner-only directory")
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def _intact(root: Path, state: CacheState, records, *, complete: bool = False) -> bool:
    try:
        _directory(root)
        if any(type(row[2]) is not int or row[2] < 1 for row in records):
            return False
        total = sum(row[2] for row in records)
        if total > state.rows or (complete and total != state.rows):
            return False
        for scope, key, count in records:
            if key != _file_key(state.generation, scope) or type(count) is not int or count < 1:
                return False
            for suffix, dtype, width in state.layout():
                info = os.lstat(root / f"{key}.{suffix}")
                if not stat.S_ISREG(info.st_mode) or info.st_size < count * width * np.dtype(dtype).itemsize:
                    return False
        return True
    except (OSError, ValueError):
        return False


def _metadata(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO vector_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        values.items(),
    )


def _append(path: Path, start: int, values: np.ndarray, *, sync: bool = True) -> None:
    """Overwrite only an unpublished suffix; committed mapped bytes stay immutable."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        _regular_file(stream.fileno(), path, mutable=True)
        if os.fstat(stream.fileno()).st_size < start:
            raise ValueError("mapped vector committed prefix is incomplete")
        stream.seek(start)
        payload = memoryview(np.ascontiguousarray(values)).cast("B")
        if stream.write(payload) != len(payload):
            raise OSError("mapped vector cache write was incomplete")
        stream.flush()
        if sync:
            os.fsync(stream.fileno())


class MappedIndex:
    """Publish derived files from a worker and reuse their read mappings across recalls."""

    def __init__(self, db_path: Path, dimensions: int) -> None:
        """Configure a cache without creating files or opening the raw database."""
        self.root = cache_directory(db_path)
        self.dimensions = dimensions
        self.generation = ""
        self.scopes: dict[str, MappedScope] = {}
        self.quarantined: tuple[str, str] | None = None

    def close(self) -> None:
        """Release cached mappings before the owning store closes or changes generation."""
        for scope in self.scopes.values():
            scope.close()
        self.scopes.clear()
        self.generation = ""

    def error(self, generation: str) -> str | None:
        """Read a bounded, data-free quarantine marker without following a link."""
        if self.quarantined is not None and self.quarantined[0] == generation:
            return self.quarantined[1]
        path = self.root / f"{generation}.invalid"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                _regular_file(stream.fileno(), path, mutable=False)
                reason = stream.read(128).decode("ascii")
            return (
                reason
                if reason
                in {
                    "non_finite_scores",
                    "invalid_norms",
                    "invalid_sequences",
                    "authoritative_score_mismatch",
                    "checksum_mismatch",
                    "checksum_file_unavailable",
                }
                else "invalid_cache_marker"
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            return "invalid_cache_marker"

    def invalidate(self, state: CacheState, reason: str) -> None:
        """Quarantine only the observed generation, including from a read-only SQL connection."""
        self.quarantined = (state.generation, reason)
        path = self.root / f"{state.generation}.invalid"
        try:
            _directory(self.root)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                _regular_file(stream.fileno(), path, mutable=True)
                stream.write(reason.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            # A read-only filesystem still keeps this reader on the authoritative path.
            pass
        self.close()

    def view(self, conn: sqlite3.Connection, scopes: list[str]) -> tuple[CacheState, list[MappedScope]] | None:
        """Return authorized committed prefixes, or request an exact raw scan when unavailable."""
        try:
            check_deadline()
            state = _state(conn, self.dimensions)
            if (
                state is None
                or self.error(state.generation)
                or state.coarse_dimensions != coarse_dimensions(conn, self.dimensions)
            ):
                self.close()
                return None
            marks = ",".join("?" for _ in scopes)
            records = conn.execute(
                f"SELECT scope_id,file_key,row_count FROM vector_cache_scopes WHERE scope_id IN ({marks})", scopes
            ).fetchall()
            if not _intact(self.root, state, records):
                self.close()
                return None
            present = {row[0] for row in records}
            missing = [scope for scope in scopes if scope not in present]
            if missing:
                absent_marks = ",".join("?" for _ in missing)
                if conn.execute(
                    f"SELECT 1 FROM vector_records WHERE scope_id IN ({absent_marks}) AND seq<=? LIMIT 1",
                    (*missing, state.watermark),
                ).fetchone():
                    self.close()
                    return None
            if self.generation != state.generation:
                self.close()
                self.generation = state.generation
            # Keep handles bounded by this query's scopes, even when one reader visits many projects.
            for scope in self.scopes.keys() - present:
                self.scopes.pop(scope).close()
            for scope, key, count in records:
                check_deadline()
                previous = self.scopes.get(scope)
                if previous is None or previous.key != key or previous.count != count:
                    if previous is not None:
                        previous.close()
                    self.scopes[scope] = MappedScope(self.root, state, scope, key, count)
            return state, [self.scopes[scope] for scope in scopes if scope in present]
        except TimeoutError:
            raise
        except (OSError, MemoryError, ValueError, KeyError, sqlite3.Error):
            self.close()
            return None

    def _prepare(self, conn: sqlite3.Connection) -> CacheState:
        _directory(self.root, create=True)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vector_cache_scopes("
            "scope_id TEXT PRIMARY KEY,file_key TEXT NOT NULL UNIQUE,row_count INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vector_cache_pages("
            "page INTEGER PRIMARY KEY,file_key TEXT NOT NULL,first_row INTEGER NOT NULL,"
            "row_count INTEGER NOT NULL,digest TEXT NOT NULL,UNIQUE(file_key,first_row))"
        )
        desired = coarse_dimensions(conn, self.dimensions)
        try:
            current = _state(conn, self.dimensions)
        except (ValueError, KeyError):
            current = None
        records = conn.execute("SELECT scope_id,file_key,row_count FROM vector_cache_scopes").fetchall()
        if (
            current is not None
            and not self.error(current.generation)
            and current.coarse_dimensions == desired
            and _intact(self.root, current, records, complete=True)
        ):
            return current
        self.close()
        current = CacheState(uuid.uuid4().hex, self.dimensions, desired, 0, 0)
        conn.execute("DELETE FROM vector_cache_scopes")
        conn.execute("DELETE FROM vector_cache_pages")
        _metadata(
            conn,
            {
                "cache_format": FORMAT,
                "cache_generation": current.generation,
                "cache_coarse_dimensions": str(desired),
                "cache_dimensions": str(self.dimensions),
                "cache_rows": "0",
                "index_watermark": "0",
                "cache_verify_page": "0",
                "cache_verify_after": "0",
            },
        )
        return current

    def _write_scope(self, conn: sqlite3.Connection, state: CacheState, scope: str, rows, pending: set[Path]) -> None:
        prior = conn.execute("SELECT row_count FROM vector_cache_scopes WHERE scope_id=?", (scope,)).fetchone()
        count = int(prior[0]) if prior else 0
        key = _file_key(state.generation, scope)
        matrix = np.frombuffer(b"".join(row[2] for row in rows), dtype="<f4").reshape(-1, self.dimensions)
        arrays = {
            "raw.f32": matrix,
            "norms.f64": np.sqrt(np.einsum("ij,ij->i", matrix, matrix, dtype=np.float64, optimize=False)),
            "seqs.i64": np.asarray([row[0] for row in rows], dtype="<i8"),
        }
        if state.coarse_dimensions < self.dimensions:
            prefix = matrix[:, : state.coarse_dimensions].astype(np.float64)
            lengths = np.sqrt(np.einsum("ij,ij->i", prefix, prefix, optimize=False))
            np.divide(prefix, lengths[:, None], out=prefix, where=lengths[:, None] > 0)
            arrays["prefix.f32"] = prefix.astype("<f4")
        for suffix, dtype, width in state.layout():
            check_deadline()
            path = self.root / f"{key}.{suffix}"
            _append(
                path,
                count * width * np.dtype(dtype).itemsize,
                np.asarray(arrays[suffix], dtype=dtype),
                sync=False,
            )
            pending.add(path)
        pages = []
        for first in range(0, len(rows), PAGE_ROWS):
            count_rows = min(PAGE_ROWS, len(rows) - first)
            digest = hashlib.sha256()
            for suffix, dtype, _width in state.layout():
                values = np.ascontiguousarray(arrays[suffix][first : first + count_rows], dtype=dtype)
                digest.update(memoryview(values).cast("B"))
            pages.append((key, count + first, count_rows, digest.hexdigest()))
        conn.executemany(
            "INSERT INTO vector_cache_pages(file_key,first_row,row_count,digest) VALUES(?,?,?,?)",
            pages,
        )
        conn.execute(
            "INSERT INTO vector_cache_scopes VALUES(?,?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET file_key=excluded.file_key,row_count=excluded.row_count",
            (scope, key, count + len(rows)),
        )

    def _collect(self, conn: sqlite3.Connection) -> bool:
        """Recheck the published generation under the writer lock; bound retries and file work."""
        generation = str(conn.execute("SELECT value FROM vector_meta WHERE key='cache_generation'").fetchone()[0])
        pending, attempted = False, 0
        for path in self.root.iterdir():
            if (
                not _OWNED_FILE.fullmatch(path.name)
                or path.name.startswith(generation + "-")
                or path.name == generation + ".invalid"
            ):
                continue
            left = remaining_seconds()
            if attempted >= COLLECT_FILES or (left is not None and left <= 0.5):
                pending = True
                break
            attempted += 1
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    pending = True
                else:
                    path.unlink()
            except OSError:
                pending = True
        _metadata(
            conn,
            {
                "cache_collect_pending": str(int(pending)),
                "cache_collect_after": str(time.time() + COLLECT_RETRY_SECONDS if pending else 0),
            },
        )
        return pending

    def _page_digest(self, state: CacheState, key: str, first: int, count: int) -> str:
        """Hash one committed page using bounded reads and the publication layout order."""
        digest = hashlib.sha256()
        if not key.startswith(state.generation + "-") or not _OWNED_FILE.fullmatch(key + ".raw.f32"):
            raise CacheCorrupt("checksum_mismatch")
        if first < 0 or not 0 < count <= PAGE_ROWS:
            raise CacheCorrupt("checksum_mismatch")
        for suffix, dtype, width in state.layout():
            check_deadline()
            path = self.root / f"{key}.{suffix}"
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                _regular_file(stream.fileno(), path, mutable=False)
                stride = width * np.dtype(dtype).itemsize
                stream.seek(first * stride)
                left = count * stride
                while left:
                    check_deadline()
                    block = stream.read(min(left, 1024 * 1024))
                    if not block:
                        raise CacheCorrupt("checksum_mismatch")
                    digest.update(block)
                    left -= len(block)
        return digest.hexdigest()

    def _verify(self, conn: sqlite3.Connection, state: CacheState) -> tuple[int, str | None]:
        """Audit a bounded page batch and persist the cursor for an eventual full cycle."""
        meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
        if float(meta.get("cache_verify_after", 0)) > time.time():
            return 0, None
        pages = conn.execute(
            "SELECT page,file_key,first_row,row_count,digest FROM vector_cache_pages "
            "WHERE page>? ORDER BY page LIMIT ?",
            (int(meta.get("cache_verify_page", 0)), VERIFY_PAGES + 1),
        ).fetchall()
        verified = 0
        try:
            for _page, key, first, count, expected in pages[:VERIFY_PAGES]:
                if self._page_digest(state, key, first, count) != expected:
                    raise CacheCorrupt("checksum_mismatch")
                verified += 1
        except (OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, CacheCorrupt) else "checksum_file_unavailable"
            self.invalidate(state, reason)
            return verified, reason
        more = len(pages) > VERIFY_PAGES
        cursor = pages[verified - 1][0] if verified and more else 0
        _metadata(
            conn,
            {
                "cache_verify_page": str(cursor),
                "cache_verify_after": str(0 if more else time.time() + VERIFY_SECONDS),
            },
        )
        return verified, None

    def _stage_rows(self, conn: sqlite3.Connection, state: CacheState, max_rows: int) -> tuple[int, int, set[Path]]:
        """Append ignored tails in fixed memory, returning the batch bounds and unsynced files.

        Large imports traverse the covering scope/sequence index so each file
        is written together. Idle passes and short tails instead use the raw
        sequence range; walking the full scope index for one new row would
        make ordinary worker upkeep grow with the already indexed corpus.
        """
        total, watermark = conn.execute(
            "SELECT count(*),max(seq) FROM (SELECT seq FROM vector_records WHERE seq>? ORDER BY seq LIMIT ?)",
            (state.watermark, min(max_rows, INDEX_BATCH_ROWS)),
        ).fetchone()
        watermark = int(watermark) if watermark is not None else state.watermark
        pending: set[Path] = set()
        cursor = None
        if total:
            ordered = (
                "INDEXED BY idx_vector_records_scope WHERE seq>? AND seq<=? ORDER BY scope_id,seq"
                if total > INDEX_PAGE_ROWS
                else "WHERE seq>? AND seq<=? ORDER BY seq"
            )
            cursor = conn.execute(
                "SELECT seq,scope_id,vector FROM vector_records " + ordered, (state.watermark, watermark)
            )
        while cursor is not None:
            check_deadline()
            rows = cursor.fetchmany(INDEX_PAGE_ROWS)
            if not rows:
                break
            grouped: dict[str, list[Any]] = {}
            for row in rows:
                grouped.setdefault(row[1], []).append(row)
            for scope, members in grouped.items():
                self._write_scope(conn, state, scope, members, pending)
        return watermark, total, pending

    def maintain(self, conn: sqlite3.Connection, max_rows: int) -> dict[str, Any]:
        """Stream a bounded batch, syncing each file once before publishing its watermark.

        A partition may receive rows in every input page. Reopening and syncing
        it for every small page makes imported rows expensive to cache; only
        the batch's durable publication needs those syncs. Uncommitted tails
        are still ignored, including after a deadline or interrupted sync.
        """
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("positive index page required")
        check_deadline()
        seconds = remaining_seconds()
        conn.execute(f"PRAGMA busy_timeout={int(min(30.0, max(0.0, seconds if seconds is not None else 30.0)) * 1000)}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._prepare(conn)
            watermark, total, pending = self._stage_rows(conn, state, max_rows)
            for path in sorted(pending):
                check_deadline()
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                try:
                    _regular_file(descriptor, path, mutable=True)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            stale = conn.execute("SELECT seq FROM index_deletes ORDER BY seq LIMIT ?", (PAGE_ROWS,)).fetchall()
            conn.executemany("DELETE FROM index_deletes WHERE seq=?", stale)
            _metadata(
                conn,
                {
                    "index_watermark": str(watermark),
                    "index_retry_after": "0",
                    "cache_rows": str(state.rows + total),
                },
            )
            verified, error = self._verify(conn, state)
            collecting = self._collect(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {
            "outcome": "corrupt" if error else "indexed",
            "rows": total,
            "watermark": watermark,
            "deletes": len(stale),
            "verified_pages": verified,
            "collection_pending": collecting,
        }

    def diagnostics(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Report the engine and current derived layout without creating or rebuilding files."""
        desired = coarse_dimensions(conn, self.dimensions)
        ready = False
        try:
            state = _state(conn, self.dimensions)
            records = conn.execute("SELECT scope_id,file_key,row_count FROM vector_cache_scopes").fetchall()
            ready = (
                state is not None
                and not self.error(state.generation)
                and state.coarse_dimensions == desired
                and _intact(self.root, state, records, complete=True)
            )
        except (sqlite3.Error, OSError, ValueError, KeyError):
            state = None
        meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
        return {
            "cache_error": self.error(state.generation) if state is not None else None,
            "collection_pending": meta.get("cache_collect_pending") == "1",
            "collect_after": float(meta.get("cache_collect_after", 0)),
            "verify_after": float(meta.get("cache_verify_after", 0)),
            "verified_page": int(meta.get("cache_verify_page", 0)),
            "engine": ("numpy-prefix" if desired < self.dimensions else "numpy-exact") if ready else "numpy-raw",
            "raw_format": "float32-le",
            "coarse_dimensions": desired,
            "dimensions": self.dimensions,
            "candidate_target": MIN_CANDIDATES,
            "compute_threads": compute_threads(),
            "watermark": state.watermark if ready and state is not None else 0,
            "cache_ready": ready,
            "cached_rows": state.rows if ready and state is not None else 0,
        }


def cache_footprint(db_path: Path) -> dict[str, int]:
    """Count only regular owned derived files, including retired generations awaiting release."""
    size = files = 0
    root = cache_directory(db_path)
    try:
        _directory(root)
        for path in root.iterdir():
            if not _OWNED_FILE.fullmatch(path.name):
                continue
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
                files += 1
    except (OSError, ValueError):
        pass
    return {"cache_bytes": size, "cache_files": files}
