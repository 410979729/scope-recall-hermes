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
        return self.call_many(operation, [(request, refs)], config, lambda missing: [compute()])[0]

    def call_many(self, operation: str, requests: list[tuple[dict, dict]], config: dict, compute: Callable) -> list:
        """Cache each input separately and compute only missing positions in one call.

        Keys match single calls. Cache-only misses fail before compute, and a
        response count mismatch writes nothing from the batch.
        """
        envelopes = [
            {"operation": operation, "request": remap(request, refs), "model": config, "contract": CONTRACT}
            for request, refs in requests
        ]
        keys = [digest(envelope) for envelope in envelopes]
        with sqlite3.connect(self.path) as connection:
            rows = [connection.execute("SELECT response FROM responses WHERE key=?", (key,)).fetchone() for key in keys]
        missing = [index for index, row in enumerate(rows) if row is None]
        self.hits += len(rows) - len(missing)
        self.misses += len(missing)
        self.operations[operation]["hits"] += len(rows) - len(missing)
        self.operations[operation]["misses"] += len(missing)
        if missing and self.only:
            raise RuntimeError(f"cache_miss:{operation}:{keys[missing[0]]}")
        results = [
            remap(json.loads(row[0]), {logical: local for local, logical in refs.items()}) if row else None
            for row, (_, refs) in zip(rows, requests, strict=True)
        ]
        if not missing:
            return results
        computed = list(compute(missing))
        if len(computed) != len(missing):
            raise ValueError("embedding_batch_shape")
        with sqlite3.connect(self.path) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO responses VALUES (?,?,?)",
                [
                    (keys[index], canonical(envelopes[index]), canonical(remap(response, requests[index][1])))
                    for index, response in zip(missing, computed, strict=True)
                ],
            )
        for index, response in zip(missing, computed, strict=True):
            results[index] = response
        return results

    def raise_if_missing(self) -> None:
        """Fail the run even when a production port caught the original cache exception."""
        if self.only and self.misses:
            raise RuntimeError(f"cache_miss: {self.misses} required responses were absent")
