"""Going back to a ``scope-recall backup`` snapshot while keeping every deletion made since.

A snapshot still holds what was forgotten after it was taken, and copied back by itself it brings those words back.
``restore`` copies it back with the deletion ledger of the store it replaces (``core/restore.py``), replayed over it
before any host may open the store again:

1. The snapshot is opened read-only inside one read transaction, held until it is copied, and as a file, which its
   digest is read from: what is copied is what was checked and what the digest describes, since SQLite keeps any
   other SQLite writer of the file out meanwhile and the file read is the one opened.
2. With no restore pending, the ledger is exported from the store as it is, the store is backed up into
   ``--backup-dir`` and admission is closed (``begin_restore``): the ledger and then the marker are written beside
   the store, each whole, and every normal open refuses (``RESTORE_UNVERIFIED``).
3. The snapshot's pages are written over the store by SQLite's backup API on a connection to the store itself: one
   transaction of the store's, in which SQLite keeps it in WAL mode with ``memory.sqlite3-wal`` and ``-shm`` its own,
   and which leaves the store as it was if it is cut short.
4. The ledger is replayed (``replay_deletion_ledger``), which truncates the write-ahead log that held the snapshot's
   pages before it reopens admission.

Everything runs holding the store (``backup.exclusive_store``), once the running-code records show no host, server
or worker that opened it still running.  A run that stops after step 2 leaves the store closed, and running the
command again finishes it, with this snapshot or another: the ledger kept beside the marker, or the store itself while
nothing has replaced it, holds the ledger the store was closed on (``pending_restore_ledger``).  A snapshot at an
older schema than this release's gets step 2 here and the rest from a release that reads that schema.

The vector companion and the retained files are left as they are.  They are reached only through SQLite, the replay
queues the purge of every replayed delete again, and the worker's next pass removes those vectors and attachments
once more.  An attachment only the replaced store refers to stays in ``retained``, where that store, restored from
``--backup-dir`` in turn, finds it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BufferedReader
from pathlib import Path
from typing import Any

from scope_recall.adapters.clients.config import CodexConfigError
from scope_recall.adapters.hermes.audiences import HermesIdentityError
from scope_recall.adapters.hermes.shared_entries import read_shared_payload
from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core.restore import (
    LEDGER_FORMAT,
    LEGACY_LEDGER_FORMAT,
    InstallationMaintenance,
    begin_restore,
    export_deletion_ledger,
    ledger_digest,
    pending_restore_ledger,
    replay_deletion_ledger,
    restore_pending,
)
from scope_recall.core.schema import APPLICATION_ID, SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.truth_connection import connect_truth_database
from scope_recall.maintenance.shared import store_binding
from scope_recall.runtime.running_code import live_records

from .backup import BackupError, backup_sqlite, exclusive_store, safe_path
from .disaster_restore import DisasterRestoreError, apply_disaster_restore, plan_disaster_restore
from .doctor import load_binding
from .install_common import HostChoice
from .shared_run import SharedStoreError, encoded, store_lock

#: The oldest schema read by a release that carries this command.  A snapshot at an older one could be put back only
#: by a release that cannot replay the ledger, so it is refused.
RESTORE_SCHEMA_FLOOR = 1110
#: How long one of the store's own transactions waits for SQLite's lock, and the replay for readers to leave the
#: write-ahead log; the writer lease is held all along.
_STORE_SECONDS = 10.0
#: What to do when the core refuses with one of these ``RESTORE_UNVERIFIED`` fields.
_HINTS = {
    "wal_busy": "a reader is still on an older snapshot of the store, whose write-ahead log holds the snapshot's "
    "pages: stop every process that reads the store, then run restore again",
    "ledger_missing": "the store was replaced while it was closed and no ledger is kept beside it; the backup taken "
    "before the copy holds the store as it was",
    "marker_damaged": "the marker is damaged, and the ledger kept beside it is newer than the store: the store was "
    "replaced after it was closed, so the marker is not written again from the store",
}


class RestoreError(RuntimeError):
    """A refusal, named by ``code``, with what to do about it."""

    def __init__(self, code: str, hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.hint = hint


def store_to_restore(*, host: HostChoice | None, instance_root: Path | None, root: Path | None) -> InstanceBinding:
    """The binding of the store to restore, as its installation records it: a shared store's manifest, or the record
    of a Hermes or Codex installation with a store of its own."""
    if root is not None:
        payload = read_shared_payload(root)
        if not payload["scope_ids"]:
            raise RestoreError("store_empty", "this shared store has no entries yet; there is nothing to restore")
        return store_binding(payload, root.resolve(), frozenset(payload["scope_ids"]))
    if host is None or instance_root is None:
        raise RestoreError("store_unnamed", "name the store: --host and --instance-root, or --root")
    binding, _data_directory = load_binding(host, instance_root)
    if binding.installation_kind != "local":
        raise RestoreError("shared_store_entry", "this home is an entry of a shared store: restore it with --root")
    return binding


@dataclass(frozen=True)
class _Checked:
    """What a plan and an apply both rest on, read without writing anything."""

    storage: SQLiteStorage
    authority: InstallationMaintenance
    #: The snapshot, read-only inside the read transaction its checks ran in, held until it is copied.
    source: sqlite3.Connection
    snapshot_schema: int
    #: Read from a handle on the file ``source`` reads, whatever file the snapshot's path names by then.
    snapshot_sha256: str
    ledger: dict[str, Any]
    #: The ledger is the one kept beside a pending restore's marker.  Otherwise it was read from the store, which is
    #: then still as it was when the ledger was taken, and is backed up before anything replaces it.
    kept: bool


def _store_file(binding: InstanceBinding, snapshot: Path) -> Path:
    """The store the binding names, a file no other name refers to, of which ``snapshot`` is no part under any name.

    Another name for the store's file is no backup: it changes with the store, and a process that opens the store
    through it keeps a write-ahead log of its own and never sees the marker.  A link on the store's own path is the
    Core's to refuse, on its first read, before anything is written.
    """
    store = binding.data_directory / "memory.sqlite3"
    if store.is_symlink() or not store.is_file():
        raise RestoreError("store_missing", f"no store at {store}")
    for part in (store, store.with_name(store.name + "-wal"), store.with_name(store.name + "-shm")):
        try:
            same = os.path.samefile(snapshot, part)
        except FileNotFoundError:
            continue  # A part that is not there is not the snapshot.
        except (OSError, NotImplementedError) as exc:
            hint = f"the file system cannot say whether --snapshot is the store's {part.name}"
            raise RestoreError("file_identity_unknown", f"{hint} ({type(exc).__name__}: {exc})") from None
        if same:
            raise RestoreError("snapshot_is_the_store", f"--snapshot is the store's {part.name} under another name")
    # The count is the file system's.  One without hard links, as FAT and exFAT are, reports 1, which is true there.
    links = store.stat().st_nlink
    if links > 1:
        raise RestoreError("store_links", f"the store's file has {links} names; a backup is a copy, not another name")
    return store


@contextmanager
def _checked(binding: InstanceBinding, snapshot: Path) -> Iterator[_Checked]:
    _store_file(binding, snapshot)
    # A plan writes nothing, so a store at an older schema is refused rather than brought forward by the read.
    storage = SQLiteStorage(binding, timeout_seconds=_STORE_SECONDS, upgrade_on_open=False)
    authority = InstallationMaintenance(TrustedContext(binding, "restore", binding.scope_ids, "host_generated"))
    with _held(snapshot) as (source, stream):
        schema = _snapshot_schema(storage, source)
        ledger, kept = _ledger(storage, authority, schema)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        yield _Checked(storage, authority, source, schema, digest, ledger, kept)


@contextmanager
def _held(snapshot: Path) -> Iterator[tuple[sqlite3.Connection, BufferedReader]]:
    """The snapshot held open twice until it is copied: read-only inside a read transaction, which its checks and the
    copy read, and as a file, which its digest is read from.  It is refused unless it is an SQLite file out of WAL mode.

    SQLite opens the snapshot by its path after the file is opened: a file moved over the path in between, and still
    there once SQLite's lock is taken, is refused (``snapshot_replaced``).
    """
    with snapshot.open("rb") as stream:
        header = stream.read(20)
        if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
            raise RestoreError("snapshot_not_a_backup", "--snapshot is not an SQLite file")
        if 2 in header[18:20]:
            # A file in WAL mode is a store's own file, or a copy of one: what its log held is not in it.
            raise RestoreError("snapshot_not_a_backup", "--snapshot is a live store's file; take one with backup")
        try:
            source = connect_truth_database(snapshot, mode="ro", isolation_level=None)
        except sqlite3.DatabaseError as exc:
            raise RestoreError("snapshot_damaged", f"{type(exc).__name__}: {exc}") from None
        with closing(source):
            try:
                source.execute("BEGIN")
                # The first read takes the shared lock the transaction then holds.
                source.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlite3.DatabaseError as exc:
                raise RestoreError("snapshot_damaged", f"{type(exc).__name__}: {exc}") from None
            if not os.path.samestat(os.fstat(stream.fileno()), os.stat(snapshot)):
                raise RestoreError("snapshot_replaced", "--snapshot was replaced as it was opened: run restore again")
            yield source, stream


def _snapshot_schema(storage: SQLiteStorage, source: sqlite3.Connection) -> int:
    """The snapshot's schema, once it is a whole ``backup`` snapshot of this very store, with its scopes and entries."""
    try:
        if str(source.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            raise RestoreError("snapshot_not_a_backup", "--snapshot is a live store's file; take one with backup")
        if source.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise RestoreError("snapshot_not_a_backup", "--snapshot is not a Scope Recall 3 store")
        quick = str(source.execute("PRAGMA quick_check").fetchone()[0])
        if quick.lower() != "ok":
            raise RestoreError("snapshot_damaged", f"quick_check: {quick[:200]}")
        schema = int(source.execute("PRAGMA user_version").fetchone()[0])
        if not RESTORE_SCHEMA_FLOOR <= schema <= SCHEMA_VERSION:
            raise RestoreError("snapshot_schema", _schema_hint(schema))
        try:
            storage.verify(source, expected_schema=schema)
        except ContractError as exc:
            hint = f"the snapshot is not this store's ({exc.code}:{exc.field})"
            raise RestoreError("snapshot_identity", hint) from None
        scopes = frozenset(row[0] for row in source.execute("SELECT scope_id FROM instance_scopes"))
        registered = frozenset(row[0] for row in source.execute("SELECT entry_id FROM entries"))
    except sqlite3.DatabaseError as exc:
        raise RestoreError("snapshot_damaged", f"{type(exc).__name__}: {exc}") from None
    if scopes != storage.binding.scope_ids:
        raise RestoreError("snapshot_scopes", "the snapshot is from before an entry brought a scope of its own")
    if storage.binding.installation_kind == "shared":
        # A shared store accepts a capture only through an entry it registered, which attach does.
        entries = read_shared_payload(storage.binding.data_directory)["entries"]
        unknown = sorted({entry["entry_id"] for entry in entries if not entry.get("detached_at")} - registered)
        if unknown:
            names = ", ".join(unknown)
            raise RestoreError("snapshot_entries", f"the snapshot is from before {names} was attached: detach it first")
    return schema


def _schema_hint(schema: int) -> str:
    if schema > SCHEMA_VERSION:
        return f"the snapshot is at schema {schema}, which a newer release writes: restore it with that release"
    return (
        f"the snapshot is at schema {schema}, and no release that reads it carries restore: going back to it would"
        " bring back what was forgotten since"
    )


def _ledger(
    storage: SQLiteStorage, authority: InstallationMaintenance, snapshot_schema: int
) -> tuple[dict[str, Any], bool]:
    """The ledger to replay, and whether it is the one kept beside a pending restore's marker."""
    if not restore_pending(storage.binding.data_directory):
        # Releases reading pre-management schemas understand the monotone
        # ledger only. They preserve deletions but may retain an older mute.
        ledger_format = LEGACY_LEDGER_FORMAT if snapshot_schema < 1111 else LEDGER_FORMAT
        return export_deletion_ledger(storage, authority, ledger_format=ledger_format), False
    return pending_restore_ledger(storage, authority)


def _described(checked: _Checked, snapshot: Path) -> dict[str, Any]:
    ledger = checked.ledger
    return dict(
        store=str(checked.storage.path),
        installation_kind=checked.storage.binding.installation_kind,
        snapshot=str(snapshot),
        snapshot_schema=checked.snapshot_schema,
        snapshot_sha256=checked.snapshot_sha256,
        ledger_sha256=ledger_digest(ledger),
        ledger_schema=ledger["schema_version"],
        ledger_format=ledger["format"],
        ledger_operations=len(ledger["operations"]),
        ledger_kept=checked.kept,
    )


def plan_restore(binding: InstanceBinding, snapshot: Path) -> dict[str, Any]:
    """What ``apply_restore`` does, checked as it checks; nothing is written."""
    with _checked(binding, snapshot) as checked:
        return dict(
            _described(checked, snapshot),
            status="planned",
            action="restore" if checked.snapshot_schema == SCHEMA_VERSION else "prepare",
            backup_required=not checked.kept,
            requires_apply=True,
        )


def apply_restore(binding: InstanceBinding, snapshot: Path, *, backup_dir: Path | None, now: datetime) -> dict:
    """Restore ``snapshot`` over the store of ``binding``, or, for an older schema's snapshot, close the store and
    keep its ledger for the release that reads that schema."""
    _store_file(binding, snapshot)
    with ExitStack() as held:
        if binding.installation_kind == "shared":
            held.enter_context(store_lock(binding.data_directory, "restore", now.isoformat()))
        held.enter_context(exclusive_store(binding.data_directory, busy=_busy))
        running = live_records(binding.data_directory)
        if running:
            names = ", ".join(sorted(f"{record.host_adapter or 'process'} {record.pid}" for record in running))
            raise RestoreError("store_open", f"still running with this store open: {names}; stop them first")
        checked = held.enter_context(_checked(binding, snapshot))
        receipt = _described(checked, snapshot)
        if not checked.kept:
            if backup_dir is None:
                raise RestoreError(
                    "backup_required", "give --backup-dir: the store is kept there before it is replaced"
                )
            receipt["backup"] = _backed_up(checked.storage.path, backup_dir, now)
            begin_restore(
                checked.storage,
                checked.authority,
                expected_ledger_sha256=ledger_digest(checked.ledger),
                ledger_format=checked.ledger["format"],
            )
        if checked.snapshot_schema != SCHEMA_VERSION:
            return dict(
                receipt,
                status="prepared",
                next=[
                    f"install a release that reads schema {checked.snapshot_schema} and run restore again with this "
                    "snapshot; every host refuses the store until then",
                    "or keep the store as it was: run restore with the backup above as --snapshot",
                ],
            )
        return dict(receipt, **_restored(checked))


def _restored(checked: _Checked) -> dict[str, Any]:
    """Copy the snapshot checked over the store, then replay the ledger, which reopens the store."""
    storage = checked.storage
    with closing(
        connect_truth_database(storage.path, mode="rw", timeout=_STORE_SECONDS, isolation_level=None)
    ) as store:
        checked.source.backup(store)
    replayed = replay_deletion_ledger(storage, checked.authority, checked.ledger, remaining_seconds=_STORE_SECONDS)
    deletes = sum(entry["operation"]["mode"] == "delete" for entry in checked.ledger["operations"])
    return dict(
        status="restored",
        affected_objects=replayed["affected_objects"],
        purges_queued=deletes,
        next=[
            "start the store's worker again: its next pass removes the vectors and attachments of the replayed "
            "deletes once more",
            "then start its hosts",
        ],
    )


def _backed_up(store: Path, backup_dir: Path, now: datetime) -> dict[str, Any]:
    target = safe_path(backup_dir) / f"memory-{SCHEMA_VERSION}-{now.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    manifest = target.with_suffix(".json")
    copy = backup_sqlite(store, target, manifest=manifest)
    return {"path": copy["backup_path"], "sha256": copy["backup_sha256"], "manifest": str(manifest)}


def _busy(what: str) -> RestoreError:
    return RestoreError("store_busy", f"another process holds the store ({what}): stop it, then run restore again")


def _absolute(parser: argparse.ArgumentParser, value: Path | None, flag: str) -> Path | None:
    if value is not None and not value.is_absolute():
        parser.error(f"{flag} must be an absolute path")
    return value


def main(argv: list[str]) -> int:
    if any(arg == "--bundle" or arg.startswith("--bundle=") for arg in argv):
        bundle_parser = argparse.ArgumentParser(
            prog="scope-recall restore",
            description="Restore a full backup bundle to a fresh destination directory. "
            "Without --apply nothing is written.",
        )
        bundle_parser.add_argument("--bundle", type=Path, required=True, help="backup bundle zip file")
        bundle_parser.add_argument("--to", type=Path, required=True, help="fresh destination directory")
        bundle_parser.add_argument("--mirror", type=Path, help="optional deletion mirror jsonl path")
        bundle_parser.add_argument(
            "--apply", action="store_true", help="write to destination; without this only validates"
        )
        bargs = bundle_parser.parse_args(argv)
        _absolute(bundle_parser, bargs.bundle, "--bundle")
        _absolute(bundle_parser, bargs.to, "--to")
        if bargs.mirror:
            _absolute(bundle_parser, bargs.mirror, "--mirror")
        try:
            if bargs.apply:
                bresult = apply_disaster_restore(bargs.bundle, bargs.to, mirror=bargs.mirror)
            else:
                bresult = plan_disaster_restore(bargs.bundle, bargs.to, mirror=bargs.mirror)
            bcode = 0
        except (DisasterRestoreError, BackupError, ValueError, OSError) as exc:
            bresult, bcode = {"status": "error", "error": type(exc).__name__, "detail": str(exc)}, 2
        sys.stdout.write(encoded(bresult))
        return bcode

    parser = argparse.ArgumentParser(
        prog="scope-recall restore",
        description="Go back to a snapshot `scope-recall backup` took of a store, keeping every deletion made since. "
        "Without --apply nothing is written.",
    )
    store = parser.add_mutually_exclusive_group(required=True)
    store.add_argument("--instance-root", type=Path, help="with --host: a Hermes or Codex home with a store of its own")
    store.add_argument("--root", type=Path, help="a shared store's directory")
    parser.add_argument(
        "--host", choices=("hermes", "codex", "claude-code", "workbuddy", "dsh"), help="the host of --instance-root"
    )
    parser.add_argument("--snapshot", type=Path, required=True, help="a snapshot of this store that backup took")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="a directory for a backup of the store as it is, taken before the snapshot replaces it; --apply asks "
        "for it whenever the store is still as it was",
    )
    parser.add_argument("--apply", action="store_true", help="restore; first stop every host and worker of the store")
    args = parser.parse_args(argv)
    if (args.host is None) != (args.instance_root is None):
        parser.error("--host goes with --instance-root, and only with it")
    for value, flag in ((args.instance_root, "--instance-root"), (args.root, "--root"), (args.snapshot, "--snapshot")):
        _absolute(parser, value, flag)
    backup_dir = _absolute(parser, args.backup_dir, "--backup-dir")
    binding: InstanceBinding | None = None
    result: dict[str, Any]
    try:
        binding = store_to_restore(host=args.host, instance_root=args.instance_root, root=args.root)
        snapshot = safe_path(args.snapshot, must_exist=True)
        if args.apply:
            result = apply_restore(binding, snapshot, backup_dir=backup_dir, now=datetime.now(UTC))
        else:
            result = plan_restore(binding, snapshot)
        code = 0
    except RestoreError as exc:
        result, code = {"status": "error", "error": exc.code, "hint": exc.hint}, 2
    except ContractError as exc:
        result, code = {"status": "error", "error": exc.code, "field": exc.field}, 2
        if exc.field in _HINTS:
            result["hint"] = _HINTS[exc.field]
    except (BackupError, SharedStoreError, HermesIdentityError, CodexConfigError, OSError, sqlite3.Error) as exc:
        result, code = {"status": "error", "error": type(exc).__name__, "detail": str(exc)}, 2
    if binding is not None:
        # Closed until a run finishes it: then every host refuses the store, and restore run again finishes it.
        result["restore_pending"] = restore_pending(binding.data_directory)
    sys.stdout.write(encoded(result))
    return code
