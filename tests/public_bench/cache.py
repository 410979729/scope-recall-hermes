"""Content-address model-port requests, preserving provenance and remapping refs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

CONTRACT = "public-bench/1"


def canonical(value: object) -> str:
    """Encode complete JSON values deterministically; array order remains meaningful."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    """Hash a canonical JSON value without relying on process-randomized hash()."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def remap(value: object, refs: dict[str, str], field: str | None = None) -> object:
    """Replace complete ref values and ref@revision tokens, leaving quoted prose intact."""
    if isinstance(value, dict):
        return {key: remap(item, refs, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [remap(item, refs, field) for item in value]
    if isinstance(value, str) and field not in {"content", "quote", "value_text"}:
        base, separator, revision = value.partition("@")
        if base in refs:
            return refs[base] + (separator + revision if separator else "")
    return value


class RequestCache:
    """Persist exact port inputs and outputs; cache-only mode refuses every miss.

    Each request contains sources (role, timestamps, revision, content, chunk
    identity), candidate state and model configuration. A ref map replaces
    database-local ids with stable corpus ids, and reverses that map on reads.
    Query and document embeddings use different operation namespaces.
    """

    def __init__(self, path: Path, *, only: bool = False) -> None:
        self.path, self.only = path, only
        self.hits = self.misses = 0
        self.operations = defaultdict(lambda: {"hits": 0, "misses": 0})
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS responses(key TEXT PRIMARY KEY, request TEXT, response TEXT)"
            )

    def call(self, operation: str, request: dict, config: dict, refs: dict[str, str], compute: Callable) -> object:
        """Return a cached response or invoke compute once, with no hidden retries."""
        normalized = remap(request, refs)
        envelope = {"operation": operation, "request": normalized, "model": config, "contract": CONTRACT}
        key = digest(envelope)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT response FROM responses WHERE key=?", (key,)).fetchone()
        if row is not None:
            self.hits += 1
            self.operations[operation]["hits"] += 1
            return remap(json.loads(row[0]), {logical: local for local, logical in refs.items()})
        self.misses += 1
        self.operations[operation]["misses"] += 1
        if self.only:
            raise RuntimeError(f"cache_miss:{operation}:{key}")
        response = compute()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO responses VALUES (?,?,?)",
                (key, canonical(envelope), canonical(remap(response, refs))),
            )
        return response

    def raise_if_missing(self) -> None:
        """Fail the run even when a production port caught the original cache exception."""
        if self.only and self.misses:
            raise RuntimeError(f"cache_miss: {self.misses} required responses were absent")
