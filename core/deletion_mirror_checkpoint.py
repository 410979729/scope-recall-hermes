"""Durable journal inventory used to resume bounded deletion-mirror catch-up."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from typing import BinaryIO

from .delete_storage import canonical
from .deletion_mirror_validation import decode_mirror_record


def validate_mutable_file(stream: BinaryIO) -> None:
    """Bind mutation to an opened regular file with exactly one filesystem name."""
    opened = os.fstat(stream.fileno())
    current = os.lstat(stream.name)
    if not stat.S_ISREG(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400:
        raise ValueError("mutable mirror files cannot use links or reparse points")
    if opened.st_nlink != 1 or current.st_nlink != 1:
        raise ValueError("mutable mirror files cannot have multiple hard links")
    if not os.path.samestat(opened, current):
        raise ValueError("opened mirror file identity changed")


def record_key(item: dict) -> tuple[str, str, str]:
    """Stable identity of one installation-qualified journal record."""
    return item["installation_id"], item["kind"], item["record_id"]


def remember_record(conn, item: dict) -> None:
    """Index committed intent and reject conflicting complete records."""
    key = record_key(item)
    digest = hashlib.sha256(canonical(item).encode()).hexdigest()
    found = conn.execute(
        "SELECT digest FROM mirror_cache.records WHERE installation_id=? AND kind=? AND record_id=?", key
    ).fetchone()
    if found is not None and found[0] != digest:
        raise ValueError("conflicting deletion mirror record")
    source_key = item["data"]["object_ref"] if item["kind"] == "absence" else item["record_id"]
    conn.execute("INSERT OR IGNORE INTO mirror_cache.records VALUES (?,?,?,?,?)", (*key, digest, source_key))


def _signature(stream: BinaryIO) -> str:
    info = os.fstat(stream.fileno())
    return canonical([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def save_inventory(conn, stream: BinaryIO, *, validate: Callable[[], None] = lambda: None) -> None:
    """Persist the scan offset only after journal bytes reach durable storage."""
    validate_mutable_file(stream)
    validate()
    stream.flush()
    os.fsync(stream.fileno())
    conn.execute("INSERT OR REPLACE INTO mirror_cache.checkpoint VALUES (1,?,?)", (_signature(stream), stream.tell()))
    conn.commit()


def scan_inventory(
    conn, stream: BinaryIO, check: Callable[[], None], *, validate: Callable[[], None] = lambda: None
) -> None:
    """Resume validation from a durable byte offset; external changes reset it.

    This cache is expendable. Its signature binds it to the exact journal; a
    missing cache scans from the start without trusting a lost checkpoint.
    """
    check()
    validate_mutable_file(stream)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mirror_cache.records (
            installation_id TEXT, kind TEXT, record_id TEXT, digest TEXT, source_key TEXT,
            PRIMARY KEY(installation_id,kind,record_id));
        CREATE INDEX IF NOT EXISTS mirror_cache.by_source ON records(installation_id,kind,source_key);
        CREATE TABLE IF NOT EXISTS mirror_cache.checkpoint (
            singleton INTEGER PRIMARY KEY, signature TEXT, offset INTEGER);
    """)
    prior = conn.execute("SELECT signature,offset FROM mirror_cache.checkpoint WHERE singleton=1").fetchone()
    if prior is None or prior[0] != _signature(stream):
        conn.execute("DELETE FROM mirror_cache.records")
        offset = 0
    else:
        offset = prior[1]
    stream.seek(offset)
    try:
        while True:
            check()
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                validate_mutable_file(stream)
                stream.seek(offset)
                stream.truncate()
                break
            try:
                validate()
                remember_record(conn, decode_mirror_record(line))
            except ValueError:
                stream.seek(offset)
                raise
    finally:
        save_inventory(conn, stream, validate=validate)
