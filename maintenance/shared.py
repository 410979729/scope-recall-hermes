"""Operator commands for a shared store, the one store every agent attaches to.

``init-shared`` makes the store's directory and manifest.  ``attach`` makes a
host's home an entry of it, carrying over the grants the home's own
installation had, and writes the runtime configs: the entry's, and the shared
worker's.  ``grants`` shows an attached entry's grants and changes them from
an edited copy.  ``detach`` undoes ``attach`` for one home.  ``adopt`` records the store's
new directory after the directory was copied elsewhere.  ``entries`` lists who
is attached and when each was last heard from.  ``import-entry`` copies what
an entry's own store held, moved aside at attach, into the shared store
(``shared_import.py``).

The store's directory holds everything memory needs, so moving to another
machine is copying that directory, ``adopt``, and attaching each agent again.
Every write command keeps a copy of each file it replaces and leaves a receipt
under the store's ``receipts/``.  Hermes homes attach with the grants their own
installation had; a local client (Codex, Claude Code, WorkBuddy, dsh) has none
of its own and attaches as the owner, with grants taken from the owner rows of
entries already attached, or, as the store's first entry, a fresh installation's.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..adapters.clients.config import CONFIG_FILENAME as CODEX_CONFIG_FILENAME
from ..adapters.hermes.audiences import LOCAL_USER_ID
from ..adapters.hermes.identity import HermesRuntimeScope, resolve_runtime_audience
from ..adapters.hermes.installation import (
    MANIFEST_FILENAME,
    HermesIdentityError,
    InstallationManifest,
    build_installation_manifest,
    load_archived_installation,
)
from ..adapters.hermes.shared_entries import (
    CLIENT_HOSTS,
    ENTRY_HOSTS,
    GRANTS_SCHEMA,
    Attachment,
    attach_shared_entry,
    attach_shared_record,
    attached_again,
    attachment_path,
    client_entry_record,
    entry_grants,
    new_shared_payload,
    read_attachment,
    read_shared_payload,
    regranted,
    shared_entry_record,
    write_shared_payload,
)
from ..contracts import ContractError, InstanceBinding, TrustedContext
from ..core.storage import SQLiteStorage
from ..runtime.auxiliary import DEFAULT_LEDGER_NAME
from ..runtime.instance_config import RuntimeInstanceConfig
from ..runtime.model_budget import initialize_auxiliary_budget_ledger
from ..runtime.worker_launch import RUNTIME_CONFIG_FILENAME
from .install_common import RUNTIME_CONFIG_LIMIT
from .install_hermes import DEFAULT_AGENT_WORKSPACE
from .shared_import import import_entry
from .shared_run import Run, SharedStoreError, encoded, home_lock, replace_file, store_lock, write_json

#: The shared worker's own names in its runtime config.  Hosts replace the
#: session with theirs; the worker keeps these.
WORKER_SESSION = "shared-background"
WORKER_OWNER = "shared-scope-recall-worker"
#: What a runtime config these commands read or write may weigh.
_CONFIG_LIMIT = RUNTIME_CONFIG_LIMIT


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _absolute(value: str | Path, field: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise SharedStoreError(f"{field} must be an absolute path")
    if os.path.lexists(path) and (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise SharedStoreError(f"{field} must not be a link")
    return path.resolve()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > _CONFIG_LIMIT:
        raise SharedStoreError(f"{path} is missing or too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SharedStoreError(f"{path} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise SharedStoreError(f"{path} is not a JSON object")
    return value


def _fits(config: dict[str, Any] | None, what: str) -> None:
    """Refuse, before anything is written, a runtime config these commands could not read back."""
    if config is not None and len(encoded(config).encode("utf-8")) > _CONFIG_LIMIT:
        raise SharedStoreError(f"the {what} runtime config would pass {_CONFIG_LIMIT} bytes")


def _space(raw: dict[str, Any]) -> str:
    """The embedding space a runtime config's routes address: the vectors' directory name."""
    return RuntimeInstanceConfig.from_mapping(raw).embedding_space_id()


def _bound(raw: dict[str, Any], binding: InstanceBinding) -> dict[str, Any]:
    """``raw`` bound to ``binding``: its scopes, and its vectors in the store's directory."""
    space = _space(raw)
    out = copy.deepcopy(raw)
    out["binding"] = {
        "agent_id": binding.agent_id,
        "installation_id": binding.installation_id,
        "data_directory": str(binding.data_directory),
        "scope_ids": sorted(binding.scope_ids),
        "test_mode": binding.test_mode,
        "installation_kind": binding.installation_kind,
    }
    out["allowed_scope_ids"] = sorted(binding.scope_ids)
    if isinstance(out.get("vector"), dict):
        out["vector"]["storage_dir"] = str(binding.data_directory / "vectors" / space)
    RuntimeInstanceConfig.from_mapping(out)
    return out


def _ledger_in(out: dict[str, Any], directory: Path) -> None:
    """Point a config's spend ledger, when its routes name one, into ``directory``."""
    auxiliary = out.get("auxiliary")
    if isinstance(auxiliary, dict) and (auxiliary.get("installation_dir") or auxiliary.get("ledger_path")):
        auxiliary["installation_dir"] = str(directory)
        auxiliary["ledger_path"] = str(directory / DEFAULT_LEDGER_NAME)


def _worker_config(raw: dict[str, Any], binding: InstanceBinding) -> dict[str, Any]:
    """The shared worker's config: an entry's model routes, bound to every scope of the store."""
    out = _bound(raw, binding)
    out.update(session_id=WORKER_SESSION, owner_id=WORKER_OWNER, host_adapter="hermes")
    # The spend ledger is the store's, not the entry's the routes came from.
    _ledger_in(out, binding.data_directory)
    RuntimeInstanceConfig.from_mapping(out)
    return out


def _entry_config(
    raw: dict[str, Any],
    binding: InstanceBinding,
    *,
    home: Path,
    host: str,
    entry_id: str,
    worker: dict[str, Any] | None,
    preserve_ledger: bool = False,
) -> dict[str, Any]:
    """An entry's config: its model routes, bound to its scopes, searching the store's vector table.

    The table is the worker's: the routes may come from a store that named its
    table otherwise (a store of an older release can), and a query would then search a
    table the worker never fills.  The spend ledger lives
    beside the entry's pointer, where ``detach`` takes it from.  A client's routes
    come from another home, so the names its runtime reports are made its own.
    """
    out = _bound(raw, binding)
    table = (
        (worker or {}).get("vector", {}).get("table_name") if isinstance((worker or {}).get("vector"), dict) else None
    )
    if table and isinstance(out.get("vector"), dict):
        out["vector"]["table_name"] = table
    if not preserve_ledger:
        _ledger_in(out, attachment_path(home).parent)
    if host in CLIENT_HOSTS:
        out.update(
            host_adapter=host,
            session_id=f"{entry_id}-background",
            owner_id=f"{entry_id}-scope-recall",
            project_id=None,
            branch_id=None,
        )
    RuntimeInstanceConfig.from_mapping(out)
    return out


def _fresh_installation(home: Path, payload: dict[str, Any]) -> InstallationManifest:
    """The grants of a home with no installation to carry over: a fresh installation's, the CLI's owner row alone.

    Its owner scope is the one an installation of the store's agent made with the default workspace holds: that
    installation's entry and an entry given these grants read, write and capture into the same scope.
    """
    return build_installation_manifest(
        home, agent_id=payload["agent_id"], agent_workspace=DEFAULT_AGENT_WORKSPACE, test_mode=payload["test_mode"]
    )


def _client_grants(
    payload: dict[str, Any], home: Path, entry_id: str, like: tuple[str, ...], capture_like: str | None
) -> tuple[set[str], set[str], str]:
    """What a client entry reads, writes and captures into, taken from the owner rows of the store's other attached
    entries, Hermes homes and clients alike.

    It reads what the owner reads through any of ``like`` (``("all",)``: every other attached entry), may write where
    the owner may there, and captures where ``capture_like``'s owner captures, which must be a scope every owner row
    of every other attached entry reads: what the owner says in the client reaches every agent, and no other entry's
    grants change.  With no other entry attached, the client is the store's first and there is nothing to be like: it
    gets the owner grants of a fresh installation, as a Hermes home attached without ``--grants-from`` does, and
    brings their owner scope into the store.  The client's own record, when it is attached again, is not one of the
    others: running the same attach again finishes or repeats it.
    """
    others = {
        entry["entry_id"]: [row for row in entry["audiences"] if row.get("kind") == "owner_private"]
        for entry in payload["entries"]
        if not entry.get("detached_at") and entry["entry_id"] != entry_id
    }
    if not others:
        if like or capture_like:
            raise SharedStoreError(
                "no other entry is attached to this store to take grants like: attach its first client without "
                "--grants-like and --capture-like, and it brings the owner's scope"
            )
        owner = _fresh_installation(home, payload).audience_scopes["owner_private"]
        return {owner}, {owner}, owner
    if not like or not capture_like:
        raise SharedStoreError(
            "a client's entry needs --grants-like and --capture-like: the store has entries attached"
        )
    names = tuple(others) if like == ("all",) else like
    unknown = sorted(set(names) - set(others)) + ([capture_like] if capture_like not in others else [])
    if unknown:
        raise SharedStoreError(f"not other entries attached to this store: {', '.join(unknown)}")
    allowed = {scope for name in names for row in others[name] for scope in row["allowed_scope_ids"]}
    writable = {scope for name in names for row in others[name] for scope in row["writable_scope_ids"]}
    captures = {row["capture_scope_id"] for row in others[capture_like]}
    if len(captures) != 1:
        raise SharedStoreError(
            f"{capture_like}'s owner rows capture into {len(captures)} scopes; name an entry with one"
        )
    capture = next(iter(captures))
    unread = sorted(
        f"{name}:{row['platform']}"
        for name, rows in others.items()
        for row in rows
        if capture not in row["allowed_scope_ids"]
    )
    if unread:
        raise SharedStoreError(
            f"{capture_like}'s capture scope is not read by these owner rows: {', '.join(unread)}; "
            "what the owner says in the client would not reach them"
        )
    return allowed | {capture}, writable | {capture}, capture


def _ledger_made(raw: dict[str, Any]) -> list[str]:
    """Create the spend ledger a written config names, when it does not exist yet.

    A ledger is only ever made on purpose (``runtime/model_budget.py``), and
    every model request reserves in it first: a config naming a missing one
    refuses every embedding and consolidation with ``ledger_not_initialized``.
    The entry's moved aside with its old store, and the shared worker's is new.
    """
    auxiliary = RuntimeInstanceConfig.from_mapping(raw).auxiliary
    ledger = getattr(auxiliary, "ledger_path", None)
    if ledger is None or ledger.exists():
        return []
    initialize_auxiliary_budget_ledger(ledger, auxiliary.budget)
    return [str(ledger)]


def store_binding(payload: dict[str, Any], root: Path, scope_ids: frozenset[str]) -> InstanceBinding:
    return InstanceBinding(
        payload["agent_id"], payload["installation_id"], root, scope_ids, payload["test_mode"], "shared"
    )


def init_shared(
    *, root: Path, agent_id: str = "default", test_mode: bool = False, now: str | None = None
) -> dict[str, Any]:
    now = now or _now()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SharedStoreError("root must be a new or empty directory")
    for parent in (root, *root.parents):
        if (parent / "scope-recall").is_dir() or (parent / CODEX_CONFIG_FILENAME).is_file():
            raise SharedStoreError(
                f"root is inside an agent's home ({parent}); put the shared store outside every agent"
            )
    with store_lock(root, "init-shared", now):
        if (root / MANIFEST_FILENAME).exists():
            raise SharedStoreError("root holds a shared store already")
        payload = new_shared_payload(root, agent_id=agent_id, test_mode=test_mode)
        write_shared_payload(root, payload)
        receipt = Run(root, "init", now).receipt({"root": str(root), "installation_id": payload["installation_id"]})
    return {
        "status": "initialized",
        "root": str(root),
        "installation_id": payload["installation_id"],
        "receipt": receipt,
    }


def _refuse_misfit(
    host: str, home: Path, grants_from: Path | None, grants_like: tuple[str, ...], capture_like: str | None
) -> None:
    """Refuse a host this command does not attach, grant options ``host`` takes none of, and a home whose own store is
    still in place."""
    if host == "hermes":
        if grants_like or capture_like:
            raise SharedStoreError(
                "--grants-like and --capture-like are for a client's entry; a Hermes home carries its own grants over"
            )
        if (home / "scope-recall" / MANIFEST_FILENAME).exists():
            raise SharedStoreError(
                "this home still has its own store: move its scope-recall directory aside (for "
                "example to scope-recall.local-<date>) and pass that installation.json as --grants-from"
            )
    elif host in CLIENT_HOSTS:
        if grants_from is not None:
            raise SharedStoreError(
                "a client has no installation to carry grants over from: it takes them like entries already "
                "attached (--grants-like), or as a store's first entry brings the owner's"
            )
        if (home / CODEX_CONFIG_FILENAME).exists() or (home / "data" / "memory.sqlite3").exists():
            raise SharedStoreError(
                f"this home still has its own store: move {CODEX_CONFIG_FILENAME} and data aside "
                "(for example into local-<date>); the client's memory is then the store's"
            )
    else:
        raise SharedStoreError(f"host must be one of {', '.join(ENTRY_HOSTS)}")


def _runtime_configs(
    routes_from: Path | None, payload: dict[str, Any], root: Path, record: dict[str, Any], home: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """What an attach of ``record`` writes of runtime configs, built and checked before anything is written: the
    entry's, from ``routes_from`` bound to its scopes, and the shared worker's, from the same routes when the store has
    none, else its own bound to every scope of the store with the entry's (``None`` when that leaves it as it is);
    then the worker's as it stands, and notes on what is not configured."""
    scopes = frozenset(record["scope_ids"])
    union = frozenset(payload["scope_ids"]) | scopes
    worker_path = root / RUNTIME_CONFIG_FILENAME
    worker_now = _read_json(worker_path) if worker_path.is_file() else None
    entry_config = worker_config = None
    notes = []
    if routes_from is not None:
        routes = _read_json(routes_from)
        recovering = any(
            entry["entry_id"] == record["entry_id"] and entry.get("recovery_pending") is True
            for entry in payload["entries"]
        )
        recovered_routes = root / "entries" / record["entry_id"] / RUNTIME_CONFIG_FILENAME
        entry_config = _entry_config(
            routes,
            store_binding(payload, root, scopes),
            home=home,
            host=record["host"],
            entry_id=record["entry_id"],
            worker=worker_now,
            preserve_ledger=recovering and routes_from.resolve() == recovered_routes.resolve(),
        )
        if worker_now is None:
            worker_config = _worker_config(routes, store_binding(payload, root, union))
        elif _space(worker_now) != _space(routes):
            # A query vector from another model searches a directory the worker never fills.
            raise SharedStoreError(
                "embedding_space_differs: this entry's embedding route addresses another space "
                "than the shared worker's; give both the same embedding model"
            )
    else:
        notes.append("vector_recall_unavailable: no runtime config for this entry; its recall is lexical only")
    if worker_config is None and worker_now is not None:
        worker_config = _bound(worker_now, store_binding(payload, root, union))
        if worker_config == worker_now:
            # Rewriting it unchanged would still restart the running worker (supervisor_config_changed).
            worker_config = None
    if worker_config is None and worker_now is None:
        notes.append("worker_unconfigured: pass --runtime-config-from to give the shared worker its routes")
    _fits(entry_config, "entry's")
    _fits(worker_config, "shared worker's")
    return entry_config, worker_config, worker_now, notes


def attach(
    *,
    host: str,
    instance_root: Path,
    root: Path,
    entry_id: str,
    display_name: str,
    grants_from: Path | None = None,
    runtime_config_from: Path | None = None,
    python_executable: Path | None = None,
    grants_like: tuple[str, ...] = (),
    capture_like: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or _now()
    python = str(python_executable) if python_executable else None
    shape = f"attach --host {host} --entry {entry_id}"
    _refuse_misfit(host, instance_root, grants_from, grants_like, capture_like)
    # The entry's grants are taken from this manifest and checked against it, the manifest is written over and the
    # home's pointer written: no other command writes the store or points the home elsewhere in between.
    with home_lock(instance_root, shape, now), store_lock(root, shape, now):
        payload = read_shared_payload(root)
        source, notes = None, []
        # A home attached as the entry already keeps the grants the entry holds, and takes none of the options.
        record = attached_again(
            payload,
            entry_id=entry_id,
            host=host,
            home=instance_root,
            display_name=display_name,
            attached_at=now,
            python_executable=python,
        )
        if record is not None:
            if grants_from is not None or grants_like or capture_like:
                notes.append(
                    f"grants_kept: {entry_id} is attached from this home already and keeps the grants it holds; "
                    "the grant options given are not used (scope-recall grants changes grants)"
                )
        elif host == "hermes":
            source = (
                load_archived_installation(grants_from, instance_root)
                if grants_from is not None
                else _fresh_installation(instance_root, payload)
            )
            record = shared_entry_record(source, entry_id=entry_id, display_name=display_name, attached_at=now)
        else:
            allowed, writable, capture = _client_grants(payload, instance_root, entry_id, grants_like, capture_like)
            record = client_entry_record(
                host=host,
                home=instance_root,
                entry_id=entry_id,
                display_name=display_name,
                attached_at=now,
                allowed_scope_ids=sorted(allowed),
                writable_scope_ids=sorted(writable),
                capture_scope_id=capture,
                python_executable=python,
            )
        scopes = frozenset(record["scope_ids"])
        union = frozenset(payload["scope_ids"]) | scopes
        worker_path = root / RUNTIME_CONFIG_FILENAME
        entry_path = attachment_path(instance_root).parent / RUNTIME_CONFIG_FILENAME
        entry_config, worker_config, worker_now, configured = _runtime_configs(
            runtime_config_from, payload, root, record, instance_root
        )
        notes += configured

        run = Run(root, f"attach-{record['entry_id']}", now)
        run.keep(root / MANIFEST_FILENAME, "store")
        run.keep(worker_path, "worker")
        run.keep(attachment_path(instance_root), "entry")
        run.keep(entry_path, "entry")
        if source is not None:
            view = attach_shared_entry(
                root, source, entry_id=entry_id, display_name=display_name, now=now, python_executable=python
            )
        else:
            view = attach_shared_record(root, record, now=now)
        ledgers = []
        for path, config in ((entry_path, entry_config), (worker_path, worker_config)):
            if config is not None:
                write_json(path, config)
                ledgers += _ledger_made(config)
        result = {
            "status": "attached",
            "root": str(root),
            "entry_id": view.entry_id,
            "display_name": view.entry_name,
            "home": str(instance_root),
            "entry_scopes": len(scopes),
            "store_scopes": len(union),
            "new_scopes": len(union) - len(payload["scope_ids"]),
            "entry_runtime_config": str(entry_path) if entry_config is not None else None,
            "worker_runtime_config": str(worker_path) if worker_config is not None or worker_now is not None else None,
            "worker_runtime_config_written": worker_config is not None,
            "grants_from": str(grants_from) if grants_from is not None else None,
            "grants_like": list(grants_like) or None,
            "capture_scope_like": capture_like,
            "ledgers_created": ledgers,
            "notes": notes,
        }
        result["receipt"] = run.receipt(result)
    return result


@contextmanager
def _attached_home(instance_root: Path, command: str, now: str) -> Iterator[Attachment]:
    """The home's pointer, held: the home's lock, then the lock of the store the pointer names.  Read again under both,
    the pointer must still name that store and entry; one written meanwhile by something that takes neither lock, such
    as an edit by hand, is refused, so that nothing is written to a store the home no longer points at, nor the pointer
    of another store removed."""
    attachment = read_attachment(instance_root)
    if attachment is None:
        raise SharedStoreError("this home is not attached to a shared store")
    with home_lock(instance_root, command, now), store_lock(attachment.root, command, now):
        if read_attachment(instance_root) != attachment:
            raise SharedStoreError(
                "this home's pointer changed while the command waited for its locks: nothing was written; run it again"
            )
        yield attachment


def detach(*, instance_root: Path, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    with _attached_home(instance_root, f"detach --instance-root {instance_root}", now) as attachment:
        root = attachment.root
        payload = read_shared_payload(root)
        record = next((entry for entry in payload["entries"] if entry["entry_id"] == attachment.entry_id), None)
        if record is None:
            raise SharedStoreError("the shared store has no record of this entry")
        run = Run(root, f"detach-{attachment.entry_id}", now)
        entry_dir = attachment_path(instance_root).parent
        pointer, entry_config = attachment_path(instance_root), entry_dir / RUNTIME_CONFIG_FILENAME
        # The entry's spend ledger lives beside its pointer (attach made it); it is
        # a record of what the entry spent, so it moves out with the receipt, whole.
        ledger = (
            getattr(RuntimeInstanceConfig.from_mapping(_read_json(entry_config)).auxiliary, "ledger_path", None)
            if entry_config.is_file()
            else None
        )
        ledgers = [
            path
            for path in ((ledger, Path(f"{ledger}-wal"), Path(f"{ledger}-shm")) if ledger is not None else ())
            if path.parent.resolve() == entry_dir.resolve() and path.is_file()
        ]
        run.keep(root / MANIFEST_FILENAME, "store")
        run.keep(pointer, "entry")
        run.keep(entry_config, "entry")
        # The record stays, detached: the store's rows still name this entry, and
        # a capture it left in the inbox is still checked against its grants.
        record["detached_at"] = now
        write_shared_payload(root, payload)
        for path in ledgers:
            run.folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(run.folder / f"entry-{path.name}"))
            run.backups.append(str(run.folder / f"entry-{path.name}"))
        for path in (pointer, entry_config):
            path.unlink(missing_ok=True)
        if entry_dir.is_dir() and not any(entry_dir.iterdir()):
            entry_dir.rmdir()
        result = {
            "status": "detached",
            "root": str(root),
            "entry_id": attachment.entry_id,
            "home": str(instance_root),
            "home_directory_left": entry_dir.is_dir(),
        }
        result["receipt"] = run.receipt(result)
    return result


def adopt(*, root: Path, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    with store_lock(root, "adopt", now):
        payload = read_shared_payload(root)
        if not payload["scope_ids"]:
            raise SharedStoreError("the store has no entries yet; there is nothing to adopt")
        binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
        run = Run(root, "adopt", now)
        worker_path = root / RUNTIME_CONFIG_FILENAME
        worker_now = _read_json(worker_path) if worker_path.is_file() else None
        worker_config = _bound(worker_now, binding) if worker_now is not None else None
        if worker_config is not None and isinstance(worker_config.get("auxiliary"), dict):
            auxiliary = worker_config["auxiliary"]
            if auxiliary.get("installation_dir") or auxiliary.get("ledger_path"):
                auxiliary["installation_dir"] = str(root)
                auxiliary["ledger_path"] = str(root / DEFAULT_LEDGER_NAME)
            RuntimeInstanceConfig.from_mapping(worker_config)
        run.keep(root / MANIFEST_FILENAME, "store")
        run.keep(worker_path, "worker")
        try:
            previous = SQLiteStorage(binding).adopt()
        except sqlite3.OperationalError as exc:
            raise SharedStoreError(
                "the store is busy: pause the shared worker (autostart pause) and stop every "
                "attached host, then adopt again"
            ) from exc
        payload["data_directory"] = str(root)
        write_shared_payload(root, payload)
        if worker_config is not None:
            write_json(worker_path, worker_config)
        result = {
            "status": "adopted",
            "root": str(root),
            "previous_directory": previous,
            "next": [
                "register the shared worker again: autostart enable --config <root>/runtime-config.json",
                "attach every agent again from its home",
            ],
        }
        result["receipt"] = run.receipt(result)
    return result


#: What an entry grants document these commands read may weigh: an entry migrated from 2.x has about a hundred rows.
_GRANTS_LIMIT = 4 * 1024 * 1024


#: What names a row's route, in the order the change shows it.
_ROUTE_FIELDS = ("platform", "user_id", "chat_type", "chat_id", "thread_id", "gateway_session_key", "agent_workspace")
#: What an entry's record holds of its grants (``shared_entries.entry_grants``).
_GRANT_FIELDS = ("owner_principals", "audience_scopes", "audiences")


def _row_shown(row: dict[str, Any]) -> str:
    """One row as the change shows it: its route, its kind and the scopes it reads, writes and captures into."""
    shown = {field: row[field] for field in _ROUTE_FIELDS}
    shown.update(
        kind=row["kind"],
        read=sorted(row["allowed_scope_ids"]),
        write=sorted(row["writable_scope_ids"]),
        capture=row["capture_scope_id"],
    )
    return encoded(shown)


def _owners(record: dict[str, Any]) -> set[tuple[str, str]]:
    return {(item["platform"], item["user_id"]) for item in record["owner_principals"]}


def _in_order(rows: list[str], common: Counter[str]) -> list[str]:
    """``rows`` in their order, keeping only those both sides hold, as many times as both do."""
    left, kept = Counter(common), []
    for row in rows:
        if left[row] > 0:
            left[row] -= 1
            kept.append(row)
    return kept


def _routes_changed(before: InstallationManifest, after: InstallationManifest) -> list[dict[str, Any]]:
    """Each route a row names, before or after, that the change lets read, write or capture otherwise, as its
    session binds (``resolve_runtime_audience``) before and after.  Rows alone do not say it: of the rows a session
    matches, the first that captures into a scope it may write decides, so two rows on one route, swapped, move a
    chat's captures with no row, scope or principal changed; an owner principal wakes or silences owner rows.  A
    route resolved with its own session key stands for every session it matches, unpinned rows included."""
    routes = {tuple(row[field] for field in _ROUTE_FIELDS) for row in (*before.audiences, *after.audiences)}
    changed = []
    for route in sorted(routes):
        scope = HermesRuntimeScope(
            **dict(zip(_ROUTE_FIELDS, route, strict=True)),
            agent_identity=after.agent_id,
            agent_context="primary",
            entry_id=str(after.entry_id),
        )
        was, now = (resolve_runtime_audience(view, scope) for view in (before, after))
        shown = [
            {
                "read": sorted(audience.allowed_scope_ids),
                "write": sorted(audience.writable_scope_ids),
                "capture": audience.capture_scope_id,
            }
            for audience in (was, now)
        ]
        if shown[0] != shown[1]:
            changed.append({"route": "/".join(route), "before": shown[0], "after": shown[1]})
    return changed


def _grants_change(
    before: dict[str, Any], after: dict[str, Any], views: tuple[InstallationManifest, InstallationManifest]
) -> dict[str, Any]:
    """What a grants change does: the rows that go and the rows that come, each whole (two rows may share a route,
    and both count), whether the rows both hold changed order, each route that binds otherwise, the owner principals,
    the scopes the entry holds, and the owner rows that grant nothing.  Routes and scope ids only.  Whether anything
    changes is read from the whole records, not from this summary."""
    shown = [list(map(_row_shown, record["audiences"])) for record in (before, after)]
    old, new = Counter(shown[0]), Counter(shown[1])
    owners = _owners(after)
    return {
        "entry_id": before["entry_id"],
        "rows_added": [json.loads(row) for row in sorted((new - old).elements())],
        "rows_removed": [json.loads(row) for row in sorted((old - new).elements())],
        "rows_reordered": _in_order(shown[0], old & new) != _in_order(shown[1], old & new),
        "routes_changed": _routes_changed(*views),
        "principals_added": sorted("/".join(item) for item in owners - _owners(before)),
        "principals_removed": sorted("/".join(item) for item in _owners(before) - owners),
        "audience_scopes_changed": before["audience_scopes"] != after["audience_scopes"],
        "entry_scopes_added": sorted(set(after["scope_ids"]) - set(before["scope_ids"])),
        "entry_scopes_removed": sorted(set(before["scope_ids"]) - set(after["scope_ids"])),
        "owner_rows_granting_nothing": sorted(
            "/".join(row[field] for field in _ROUTE_FIELDS)
            for row in after["audiences"]
            if row["kind"] == "owner_private" and (row["platform"], row["user_id"]) not in owners
        ),
        "unchanged": all(encoded({"value": before[name]}) == encoded({"value": after[name]}) for name in _GRANT_FIELDS),
    }


def grants(
    *,
    instance_root: Path,
    grants_from: Path | None = None,
    out: Path | None = None,
    apply: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Show an attached entry's grants, or change them from an edited copy.

    Without ``grants_from`` the entry's grants come back as a document, written to ``out`` when one is named.  With
    it the edited document is checked as attach checks a record and the change is shown; with ``apply`` as well it
    is written: the store's manifest, and the entry's runtime config when the scopes it binds are not the entry's.
    The store's own scopes never change (``regranted``), so the shared worker's config does not either.  A copy of
    each file replaced and the entry's grants before the change (``grants-before.json``, which applied the same way
    puts them back) go with the receipt.  A write that fails puts the copies back; run again, the command also
    rebinds an entry config a failed run left behind.  Memories keep their scopes; nothing else is written.  The
    grants are read, and a change planned and written, holding the home's lock and the store's (``_attached_home``),
    so no other command writes the store or points the home elsewhere in between.
    """
    if grants_from is None and apply:
        raise SharedStoreError("--apply needs --grants-from, the entry's grants as edited")
    if grants_from is not None and out is not None:
        raise SharedStoreError("--out writes the entry's grants to edit; with --grants-from nothing is written there")
    now = now or _now()
    shape = f"grants --instance-root {instance_root}{' --apply' if apply else ''}"
    with _attached_home(instance_root, shape, now):
        if grants_from is None:
            return _shown_grants(instance_root, out)
        plan = _planned_grants(instance_root, grants_from)
        if not plan.manifest_changes and plan.entry_config is None:
            return {"status": "unchanged", **plan.change}
        if not apply:
            return {
                "status": "preview",
                **plan.change,
                "next": "nothing is written: stop this entry's host, run again with --apply, then start it again",
            }
        return _applied_grants(plan, now)


def _shown_grants(instance_root: Path, out: Path | None) -> dict[str, Any]:
    document = entry_grants(instance_root)
    if out is not None:
        if os.path.lexists(out):
            raise SharedStoreError(f"{out} exists; name a new file")
        write_json(out, document)
    return {
        "status": "shown",
        "entry_id": document["entry_id"],
        "grants": document,
        "written": str(out) if out is not None else None,
    }


@dataclass(frozen=True)
class _GrantsPlan:
    """A checked grants change: the store's manifest after it, and the entry's runtime config when it must follow."""

    store: Path
    before: dict[str, Any]
    updated: dict[str, Any]
    change: dict[str, Any]
    entry_path: Path
    entry_config: dict[str, Any] | None

    @property
    def manifest_changes(self) -> bool:
        return not self.change["unchanged"]


def _planned_grants(instance_root: Path, grants_from: Path) -> _GrantsPlan:
    if not grants_from.is_file() or grants_from.stat().st_size > _GRANTS_LIMIT:
        raise SharedStoreError(f"{grants_from} is missing or too large")
    try:
        document = json.loads(grants_from.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SharedStoreError(f"{grants_from} is not readable JSON") from exc
    store, view_before, before, after, updated, view = regranted(instance_root, document)
    if view.entry_host in CLIENT_HOSTS:
        _client_route_holds(view)
    change = _grants_change(before, after, (view_before, view))
    # The entry's runtime config binds the entry's scopes; compared with the file itself, so a run that failed after
    # the manifest was written is finished by the next one.
    entry_path = attachment_path(instance_root).parent / RUNTIME_CONFIG_FILENAME
    entry_config = None
    if entry_path.is_file():
        now_config = _read_json(entry_path)
        entry_config = _bound(now_config, store_binding(updated, store, frozenset(after["scope_ids"])))
        if entry_config == now_config:
            entry_config = None
    change["entry_runtime_config_rebound"] = entry_config is not None
    _fits(entry_config, "entry's")
    return _GrantsPlan(store, before, updated, change, entry_path, entry_config)


def _client_route_holds(view: InstallationManifest) -> None:
    """Refuse grants under which a client entry's own route, bound as the client binds it (``load_shared_client``),
    has no owner row of an owner principal that reads and captures.  The client takes whoever types into it for the
    owner: a conversation row in its owner row's place, or its owner principal replaced, would still bind it, as no
    owner.  So only the owner rows are counted."""
    scope = HermesRuntimeScope(
        platform=view.entry_host,
        user_id=LOCAL_USER_ID,
        chat_type="private",
        chat_id=LOCAL_USER_ID,
        thread_id="main",
        agent_identity=view.agent_id,
        agent_workspace="default",
        agent_context="primary",
        entry_id=str(view.entry_id),
    )
    owner_rows = tuple(row for row in view.audiences if str(row.get("kind") or "conversation") == "owner_private")
    audience = resolve_runtime_audience(replace(view, audiences=owner_rows), scope)
    if not audience.includes_owner_private or not audience.allowed_scope_ids or audience.capture_scope_id is None:
        raise SharedStoreError(
            f"a {view.entry_host} entry needs an owner row on its own route, of an owner principal, that reads and "
            "captures: under these grants the client would not bind as the owner"
        )


def _applied_grants(plan: _GrantsPlan, now: str) -> dict[str, Any]:
    run = Run(plan.store, f"grants-{plan.before['entry_id']}", now)
    replaced = {plan.store / MANIFEST_FILENAME: "store", plan.entry_path: "entry"}
    for path, label in replaced.items():
        run.keep(path, label)
    rollback = None
    if plan.manifest_changes:
        rollback = run.folder / "grants-before.json"
        previous = {name: plan.before[name] for name in _GRANT_FIELDS}
        write_json(rollback, {"schema": GRANTS_SCHEMA, "entry_id": plan.before["entry_id"], **previous})
    try:
        if plan.manifest_changes:
            write_shared_payload(plan.store, plan.updated)
        if plan.entry_config is not None:
            write_json(plan.entry_path, plan.entry_config)
        result = {
            "status": "applied",
            **plan.change,
            "rollback": str(rollback) if rollback is not None else None,
            "next": "start this entry's host again: it binds the new grants when it starts (doctor checks them)",
        }
        # The receipt is part of the change: one that failed once the change was written would leave the grants
        # changed and the command failed, with no word of the grants as they were.
        result["receipt"] = run.receipt(result)
    except BaseException as exc:
        # Half a change binds the entry to scopes its grants do not name, or the other way round.
        left = _put_back(run, replaced)
        if left and isinstance(exc, Exception):
            raise SharedStoreError(f"{exc}; and the change could not be undone: copy back {'; '.join(left)}") from exc
        raise
    return result


def _put_back(run: Run, replaced: dict[Path, str]) -> list[str]:
    """Each file a run kept a copy of, as the copy has it, replaced in one step, and retried while a reader holds it,
    as the worker reading the manifest does for a moment: tried once, a change would be left half undone.  Each file is
    tried; what could not be put back comes back as ``copy -> file``, to copy by hand."""
    left = []
    for path, label in replaced.items():
        kept = run.folder / f"{label}-{path.name}"
        if kept.is_file():
            staged = path.with_name(f".{path.name}.restore")
            try:
                shutil.copy2(kept, staged)
                replace_file(staged, path)
            except OSError:
                left.append(f"{kept} -> {path}")
    return left


def _pointer_of(home: Path, now: str) -> Attachment | None:
    """A home's pointer, read holding the home's lock, so that no command is midway through changing it.  The lock is
    held for that read alone, never beside another home's; a home that is no directory has no pointer, and gets no
    lock file."""
    if not home.is_dir():
        return None
    with home_lock(home, "entries", now):
        return read_attachment(home)


def entries(*, root: Path, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    payload = read_shared_payload(root)
    seen: dict[str, dict[str, str]] = {}
    store = "empty"
    if payload["scope_ids"] and (root / "memory.sqlite3").is_file():
        binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
        try:
            with SQLiteStorage(binding).read(
                TrustedContext(binding, "shared-store-entries", binding.scope_ids, "host_generated")
            ) as tx:
                seen = tx.registry.entries()
            store = "ok"
        except ContractError as exc:
            store = f"{exc.code}:{exc.field}"
    rows = []
    for record in payload["entries"]:
        home = Path(record["home"])
        try:
            pointer = _pointer_of(home, now)
            here = pointer is not None and pointer.entry_id == record["entry_id"] and pointer.root == root
        except HermesIdentityError:
            here = False
        found = seen.get(record["entry_id"], {})
        rows.append(
            {
                "entry_id": record["entry_id"],
                "display_name": record["display_name"],
                "host": record["host"],
                "home": record["home"],
                "attached_at": record.get("attached_at"),
                "detached_at": record.get("detached_at"),
                "pointer_present": here,
                "first_seen": found.get("first_seen"),
                "last_seen": found.get("last_seen"),
            }
        )
    return {
        "status": "ok" if store in {"ok", "empty"} else "degraded",
        "root": str(root),
        "installation_id": payload["installation_id"],
        "store": store,
        "store_scopes": len(payload["scope_ids"]),
        "worker_runtime_config": (root / RUNTIME_CONFIG_FILENAME).is_file(),
        "entries": rows,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-shared", help="create a shared store's directory and manifest")
    init.add_argument("--root", required=True)
    init.add_argument("--agent-id", default="default")
    init.add_argument("--test-mode", action="store_true")
    join = sub.add_parser("attach", help="make a host's home an entry of a shared store")
    join.add_argument("--host", required=True, choices=ENTRY_HOSTS)
    join.add_argument("--instance-root", required=True)
    join.add_argument("--root", required=True)
    join.add_argument("--entry", required=True, help="2 to 32 lowercase letters, digits or hyphens")
    join.add_argument("--display-name", required=True, help="the name a reader is shown, at most 32 characters")
    join.add_argument("--grants-from", help="the installation.json of the home's own store, moved aside")
    join.add_argument(
        "--grants-like",
        help="a client's entry: the attached entries, Hermes or client, whose owner grants it gets, comma separated, "
        "or all; with no other entry attached, leave it and --capture-like out: the client brings the owner's scope",
    )
    join.add_argument(
        "--capture-like",
        help="a client's entry: the attached entry whose owner capture scope it "
        "writes into; every owner row of the store must read that scope",
    )
    join.add_argument("--runtime-config-from", help="the runtime-config.json with this entry's model routes")
    join.add_argument("--python", help="the interpreter this home's host runs, recorded for the operator")
    leave = sub.add_parser("detach", help="stop a home being an entry; its memories stay in the store")
    leave.add_argument("--instance-root", required=True)
    grant = sub.add_parser("grants", help="show an attached entry's grants, or change them from an edited copy")
    grant.add_argument("--instance-root", required=True)
    grant.add_argument("--out", help="write the entry's grants to this new file, to edit")
    grant.add_argument("--grants-from", help="the entry's grants as edited; without --apply the change is only shown")
    grant.add_argument("--apply", action="store_true", help="write the change (stop the entry's host first)")
    take = sub.add_parser("adopt", help="record the directory a copied shared store now lives in")
    take.add_argument("--root", required=True)
    listing = sub.add_parser("entries", help="list a shared store's entries and when each was last heard from")
    listing.add_argument("--root", required=True)
    bring = sub.add_parser(
        "import-entry", help="copy an entry's own store, moved aside at attach, into the shared store"
    )
    bring.add_argument("--root", required=True)
    bring.add_argument("--entry", required=True)
    bring.add_argument(
        "--from",
        dest="source",
        required=True,
        help="the entry's own store directory (scope-recall.local-<date>) or its memory.sqlite3",
    )
    bring.add_argument("--dry-run", action="store_true", help="run the whole import, then roll it back")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-shared":
            result = init_shared(root=_absolute(args.root, "root"), agent_id=args.agent_id, test_mode=args.test_mode)
        elif args.command == "attach":
            result = attach(
                host=args.host,
                instance_root=_absolute(args.instance_root, "instance_root"),
                root=_absolute(args.root, "root"),
                entry_id=args.entry,
                display_name=args.display_name,
                grants_from=_absolute(args.grants_from, "grants_from") if args.grants_from else None,
                runtime_config_from=_absolute(args.runtime_config_from, "runtime_config_from")
                if args.runtime_config_from
                else None,
                python_executable=_absolute(args.python, "python") if args.python else None,
                grants_like=tuple(name.strip() for name in (args.grants_like or "").split(",") if name.strip()),
                capture_like=args.capture_like,
            )
        elif args.command == "detach":
            result = detach(instance_root=_absolute(args.instance_root, "instance_root"))
        elif args.command == "grants":
            result = grants(
                instance_root=_absolute(args.instance_root, "instance_root"),
                grants_from=_absolute(args.grants_from, "grants_from") if args.grants_from else None,
                out=_absolute(args.out, "out") if args.out else None,
                apply=args.apply,
            )
        elif args.command == "adopt":
            result = adopt(root=_absolute(args.root, "root"))
        elif args.command == "import-entry":
            result = import_entry(
                root=_absolute(args.root, "root"),
                entry_id=args.entry,
                source=_absolute(args.source, "from"),
                dry_run=args.dry_run,
            )
        else:
            result = entries(root=_absolute(args.root, "root"))
    except ContractError as exc:
        result, code = {"status": "error", "error": f"{exc.code}:{exc.field}"}, 2
    except (SharedStoreError, HermesIdentityError, ValueError, OSError) as exc:
        result, code = {"status": "error", "error": str(exc)}, 2
    else:
        code = 0
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return code
