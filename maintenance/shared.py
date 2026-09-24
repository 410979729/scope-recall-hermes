"""Operator commands for a shared store, the one store every agent attaches to.

``init-shared`` makes the store's directory and manifest.  ``attach`` makes a
host's home an entry of it, carrying over the grants the home's own
installation had, and writes the runtime configs: the entry's, and the shared
worker's.  ``detach`` undoes that for one home.  ``adopt`` records the store's
new directory after the directory was copied elsewhere.  ``entries`` lists who
is attached and when each was last heard from.  ``import-entry`` copies what
an entry's own store held, moved aside at attach, into the shared store
(``shared_import.py``).

The store's directory holds everything memory needs, so moving to another
machine is copying that directory, ``adopt``, and attaching each agent again.
Every write command keeps a copy of each file it replaces and leaves a receipt
under the store's ``receipts/``.  Hermes homes attach with the grants their own
installation had; a local client (Codex, Claude Code) has none of its own and
attaches as the owner, with grants taken from Hermes entries' owner rows.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

from ..adapters.codex.config import CONFIG_FILENAME as CODEX_CONFIG_FILENAME
from ..adapters.hermes.installation import (
    CLIENT_HOSTS,
    ENTRY_HOSTS,
    MANIFEST_FILENAME,
    HermesIdentityError,
    attach_shared_entry,
    attach_shared_record,
    attachment_path,
    build_installation_manifest,
    client_entry_record,
    load_archived_installation,
    new_shared_payload,
    read_attachment,
    read_shared_payload,
    shared_entry_record,
    write_shared_payload,
)
from ..adapters.runtime_wiring import RUNTIME_CONFIG_FILENAME
from ..contracts import ContractError, InstanceBinding, TrustedContext
from ..core.storage import SQLiteStorage
from ..runtime.auxiliary import DEFAULT_LEDGER_NAME
from ..runtime.instance import RuntimeInstanceConfig
from ..runtime.model_budget import initialize_auxiliary_budget_ledger
from .install_hermes import DEFAULT_AGENT_WORKSPACE

RECEIPTS_DIRNAME = "receipts"
#: The shared worker's own names in its runtime config.  Hosts replace the
#: session with theirs; the worker keeps these.
WORKER_SESSION = "shared-background"
WORKER_OWNER = "shared-scope-recall-worker"
#: What a runtime config these commands read or write may weigh.  The shared worker's lists every scope of the
#: store twice (its binding and its allowed scopes), about 120 bytes a scope: the pilot's 221 scopes made 58 KB, and
#: one more instance passed the 64 KB this once was, so the next attach refused the store's own worker config.  At
#: MAX_SHARED_SCOPES, 1024 scopes, that is about 250 KB; the limit leaves room for longer scope ids.
_CONFIG_LIMIT = 1024 * 1024


class SharedStoreError(RuntimeError):
    """A shared store command refused; the message says why and what to do."""


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


def _encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _fits(config: dict[str, Any] | None, what: str) -> None:
    """Refuse, before anything is written, a runtime config these commands could not read back."""
    if config is not None and len(_encoded(config).encode("utf-8")) > _CONFIG_LIMIT:
        raise SharedStoreError(f"the {what} runtime config would pass {_CONFIG_LIMIT} bytes")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-",
                                         suffix=".json", delete=False)
    with handle:
        handle.write(_encoded(value))
    for attempt in range(40):
        try:
            os.replace(handle.name, path)
            return
        except PermissionError:
            # Windows refuses while a worker or host reads the file for a moment.
            if attempt == 39:
                Path(handle.name).unlink(missing_ok=True)
                raise
            time.sleep(0.05)


class _Run:
    """One write command: the copies it keeps and the receipt it leaves."""

    def __init__(self, root: Path, command: str, now: str) -> None:
        self.root, self.command, self.now = root, command, now
        stamp = now.replace("-", "").replace(":", "")
        self.folder = root / RECEIPTS_DIRNAME / f"{stamp}-{command}"
        self.backups: list[str] = []

    def keep(self, path: Path, label: str) -> None:
        if path.is_file():
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / f"{label}-{path.name}"
            shutil.copy2(path, target)
            self.backups.append(str(target))

    def receipt(self, body: dict[str, Any]) -> str:
        path = self.folder.with_suffix(".json")
        _write_json(path, {"command": self.command, "at": self.now, "backups": self.backups, **body})
        return str(path)


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


def _entry_config(raw: dict[str, Any], binding: InstanceBinding, *, home: Path, host: str, entry_id: str,
                  worker: dict[str, Any] | None) -> dict[str, Any]:
    """An entry's config: its model routes, bound to its scopes, searching the store's vector table.

    The table is the worker's: the routes may come from a store that named its
    table otherwise, and a query then searches a table the worker never fills
    (tianji's did, from its own 3.1 store, 2026-09-24).  The spend ledger lives
    beside the entry's pointer, where ``detach`` takes it from.  A client's routes
    come from another home, so the names its runtime reports are made its own.
    """
    out = _bound(raw, binding)
    table = (worker or {}).get("vector", {}).get("table_name") if isinstance((worker or {}).get("vector"), dict) else None
    if table and isinstance(out.get("vector"), dict):
        out["vector"]["table_name"] = table
    _ledger_in(out, attachment_path(home).parent)
    if host in CLIENT_HOSTS:
        out.update(host_adapter=host, session_id=f"{entry_id}-background", owner_id=f"{entry_id}-scope-recall",
                   project_id=None, branch_id=None)
    RuntimeInstanceConfig.from_mapping(out)
    return out


def _client_grants(payload: dict[str, Any], like: tuple[str, ...], capture_like: str) -> tuple[set[str], set[str], str]:
    """What a client entry reads, writes and captures into, taken from attached Hermes entries' owner rows.

    It reads what the owner reads through any of ``like`` (``("all",)``: every
    attached Hermes entry), may write where the owner may there, and captures
    where ``capture_like``'s owner captures, which must be a scope every owner
    row of every attached Hermes entry reads: what the owner says in the client
    reaches every agent, and no Hermes entry's grants change.
    """
    hermes = {entry["entry_id"]: entry for entry in payload["entries"]
              if entry["host"] == "hermes" and not entry.get("detached_at")}
    names = tuple(hermes) if like == ("all",) else like
    unknown = sorted(set(names) - set(hermes)) + ([capture_like] if capture_like not in hermes else [])
    if not names or unknown:
        raise SharedStoreError(f"not attached Hermes entries of this store: {', '.join(unknown) or '(none named)'}")

    def owner_rows(name: str) -> list[dict[str, Any]]:
        return [row for row in hermes[name]["audiences"] if row.get("kind") == "owner_private"]

    allowed = {scope for name in names for row in owner_rows(name) for scope in row["allowed_scope_ids"]}
    writable = {scope for name in names for row in owner_rows(name) for scope in row["writable_scope_ids"]}
    captures = {row["capture_scope_id"] for row in owner_rows(capture_like)}
    if len(captures) != 1:
        raise SharedStoreError(f"{capture_like}'s owner rows capture into {len(captures)} scopes; name an entry with one")
    capture = next(iter(captures))
    unread = sorted(f"{name}:{row['platform']}" for name in hermes for row in owner_rows(name)
                    if capture not in row["allowed_scope_ids"])
    if unread:
        raise SharedStoreError(f"{capture_like}'s capture scope is not read by these owner rows: {', '.join(unread)}; "
                               "what the owner says in the client would not reach them")
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


def _store_binding(payload: dict[str, Any], root: Path, scope_ids: frozenset[str]) -> InstanceBinding:
    return InstanceBinding(payload["agent_id"], payload["installation_id"], root, scope_ids,
                           payload["test_mode"], "shared")


def init_shared(*, root: Path, agent_id: str = "default", test_mode: bool = False, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SharedStoreError("root must be a new or empty directory")
    for parent in (root, *root.parents):
        if (parent / "scope-recall").is_dir() or (parent / CODEX_CONFIG_FILENAME).is_file():
            raise SharedStoreError(f"root is inside an agent's home ({parent}); put the shared store outside every agent")
    payload = new_shared_payload(root, agent_id=agent_id, test_mode=test_mode)
    write_shared_payload(root, payload)
    receipt = _Run(root, "init", now).receipt({"root": str(root), "installation_id": payload["installation_id"]})
    return {"status": "initialized", "root": str(root), "installation_id": payload["installation_id"], "receipt": receipt}


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
    payload = read_shared_payload(root)
    python = str(python_executable) if python_executable else None
    if host == "hermes":
        if grants_like or capture_like:
            raise SharedStoreError("--grants-like and --capture-like are for a client's entry; a Hermes home "
                                   "carries its own grants over")
        if (instance_root / "scope-recall" / MANIFEST_FILENAME).exists():
            raise SharedStoreError("this home still has its own store: move its scope-recall directory aside (for "
                                   "example to scope-recall.local-<date>) and pass that installation.json as --grants-from")
        source = (load_archived_installation(grants_from, instance_root) if grants_from is not None else
                  build_installation_manifest(instance_root, agent_id=payload["agent_id"],
                                              agent_workspace=DEFAULT_AGENT_WORKSPACE, test_mode=payload["test_mode"]))
        record = shared_entry_record(source, entry_id=entry_id, display_name=display_name, attached_at=now)
    elif host in CLIENT_HOSTS:
        if grants_from is not None:
            raise SharedStoreError("a client has no installation to carry grants over from; use --grants-like")
        if not grants_like or not capture_like:
            raise SharedStoreError("a client's entry needs --grants-like and --capture-like")
        if (instance_root / CODEX_CONFIG_FILENAME).exists() or (instance_root / "data" / "memory.sqlite3").exists():
            raise SharedStoreError(f"this home still has its own store: move {CODEX_CONFIG_FILENAME} and data aside "
                                   "(for example into local-<date>); the client's memory is then the store's")
        allowed, writable, capture = _client_grants(payload, grants_like, capture_like)
        record = client_entry_record(host=host, home=instance_root, entry_id=entry_id, display_name=display_name,
                                     attached_at=now, allowed_scope_ids=sorted(allowed), writable_scope_ids=sorted(writable),
                                     capture_scope_id=capture, python_executable=python)
    else:
        raise SharedStoreError(f"host must be one of {', '.join(ENTRY_HOSTS)}")
    scopes = frozenset(record["scope_ids"])
    union = frozenset(payload["scope_ids"]) | scopes
    worker_path = root / RUNTIME_CONFIG_FILENAME
    entry_path = attachment_path(instance_root).parent / RUNTIME_CONFIG_FILENAME
    worker_now = _read_json(worker_path) if worker_path.is_file() else None
    notes: list[str] = []
    entry_config = worker_config = None
    # Everything written below is built and checked first.
    if runtime_config_from is not None:
        routes = _read_json(runtime_config_from)
        entry_config = _entry_config(routes, _store_binding(payload, root, scopes), home=instance_root, host=host,
                                     entry_id=record["entry_id"], worker=worker_now)
        if worker_now is None:
            worker_config = _worker_config(routes, _store_binding(payload, root, union))
        elif _space(worker_now) != _space(routes):
            # A query vector from another model searches a directory the worker never fills.
            raise SharedStoreError("embedding_space_differs: this entry's embedding route addresses another space "
                                   "than the shared worker's; give both the same embedding model")
    else:
        notes.append("vector_recall_unavailable: no runtime config for this entry; its recall is lexical only")
    if worker_config is None and worker_now is not None:
        worker_config = _bound(worker_now, _store_binding(payload, root, union))
        if worker_config == worker_now:
            # Rewriting it unchanged would still restart the running worker (supervisor_config_changed).
            worker_config = None
    if worker_config is None and worker_now is None:
        notes.append("worker_unconfigured: pass --runtime-config-from to give the shared worker its routes")
    _fits(entry_config, "entry's")
    _fits(worker_config, "shared worker's")

    run = _Run(root, f"attach-{record['entry_id']}", now)
    run.keep(root / MANIFEST_FILENAME, "store")
    run.keep(worker_path, "worker")
    run.keep(attachment_path(instance_root), "entry")
    run.keep(entry_path, "entry")
    if host == "hermes":
        view = attach_shared_entry(root, source, entry_id=entry_id, display_name=display_name, now=now,
                                   python_executable=python)
    else:
        view = attach_shared_record(root, record, now=now)
    ledgers = []
    for path, config in ((entry_path, entry_config), (worker_path, worker_config)):
        if config is not None:
            _write_json(path, config)
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


def detach(*, instance_root: Path, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    attachment = read_attachment(instance_root)
    if attachment is None:
        raise SharedStoreError("this home is not attached to a shared store")
    root = attachment.root
    payload = read_shared_payload(root)
    record = next((entry for entry in payload["entries"] if entry["entry_id"] == attachment.entry_id), None)
    if record is None:
        raise SharedStoreError("the shared store has no record of this entry")
    run = _Run(root, f"detach-{attachment.entry_id}", now)
    entry_dir = attachment_path(instance_root).parent
    pointer, entry_config = attachment_path(instance_root), entry_dir / RUNTIME_CONFIG_FILENAME
    # The entry's spend ledger lives beside its pointer (attach made it); it is
    # a record of what the entry spent, so it moves out with the receipt, whole.
    ledger = getattr(RuntimeInstanceConfig.from_mapping(_read_json(entry_config)).auxiliary, "ledger_path", None) \
        if entry_config.is_file() else None
    ledgers = [path for path in ((ledger, Path(f"{ledger}-wal"), Path(f"{ledger}-shm")) if ledger is not None else ())
               if path.parent.resolve() == entry_dir.resolve() and path.is_file()]
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
    result = {"status": "detached", "root": str(root), "entry_id": attachment.entry_id, "home": str(instance_root),
              "home_directory_left": entry_dir.is_dir()}
    result["receipt"] = run.receipt(result)
    return result


def adopt(*, root: Path, now: str | None = None) -> dict[str, Any]:
    now = now or _now()
    payload = read_shared_payload(root)
    if not payload["scope_ids"]:
        raise SharedStoreError("the store has no entries yet; there is nothing to adopt")
    binding = _store_binding(payload, root, frozenset(payload["scope_ids"]))
    run = _Run(root, "adopt", now)
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
        raise SharedStoreError("the store is busy: pause the shared worker (autostart pause) and stop every "
                               "attached host, then adopt again") from exc
    payload["data_directory"] = str(root)
    write_shared_payload(root, payload)
    if worker_config is not None:
        _write_json(worker_path, worker_config)
    result = {
        "status": "adopted",
        "root": str(root),
        "previous_directory": previous,
        "next": ["register the shared worker again: autostart enable --config <root>/runtime-config.json",
                 "attach every agent again from its home"],
    }
    result["receipt"] = run.receipt(result)
    return result


def entries(*, root: Path) -> dict[str, Any]:
    payload = read_shared_payload(root)
    seen: dict[str, dict[str, str]] = {}
    store = "empty"
    if payload["scope_ids"] and (root / "memory.sqlite3").is_file():
        binding = _store_binding(payload, root, frozenset(payload["scope_ids"]))
        try:
            with SQLiteStorage(binding).read(TrustedContext(binding, "shared-store-entries", binding.scope_ids,
                                                            "host_generated")) as tx:
                seen = tx.entries()
            store = "ok"
        except ContractError as exc:
            store = f"{exc.code}:{exc.field}"
    rows = []
    for record in payload["entries"]:
        home = Path(record["home"])
        try:
            pointer = read_attachment(home)
            here = pointer is not None and pointer.entry_id == record["entry_id"] and pointer.root == root
        except HermesIdentityError:
            here = False
        found = seen.get(record["entry_id"], {})
        rows.append({
            "entry_id": record["entry_id"], "display_name": record["display_name"], "host": record["host"],
            "home": record["home"], "attached_at": record.get("attached_at"), "detached_at": record.get("detached_at"),
            "pointer_present": here, "first_seen": found.get("first_seen"), "last_seen": found.get("last_seen"),
        })
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
    join.add_argument("--grants-like", help="a client's entry: the attached Hermes entries whose owner grants it "
                      "gets, comma separated, or all")
    join.add_argument("--capture-like", help="a client's entry: the Hermes entry whose owner capture scope it "
                      "writes into; every owner row of the store must read that scope")
    join.add_argument("--runtime-config-from", help="the runtime-config.json with this entry's model routes")
    join.add_argument("--python", help="the interpreter this home's host runs, recorded for the operator")
    leave = sub.add_parser("detach", help="stop a home being an entry; its memories stay in the store")
    leave.add_argument("--instance-root", required=True)
    take = sub.add_parser("adopt", help="record the directory a copied shared store now lives in")
    take.add_argument("--root", required=True)
    listing = sub.add_parser("entries", help="list a shared store's entries and when each was last heard from")
    listing.add_argument("--root", required=True)
    bring = sub.add_parser("import-entry", help="copy an entry's own store, moved aside at attach, into the shared store")
    bring.add_argument("--root", required=True)
    bring.add_argument("--entry", required=True)
    bring.add_argument("--from", dest="source", required=True,
                       help="the entry's own store directory (scope-recall.local-<date>) or its memory.sqlite3")
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
                runtime_config_from=_absolute(args.runtime_config_from, "runtime_config_from") if args.runtime_config_from else None,
                python_executable=_absolute(args.python, "python") if args.python else None,
                grants_like=tuple(name.strip() for name in (args.grants_like or "").split(",") if name.strip()),
                capture_like=args.capture_like,
            )
        elif args.command == "detach":
            result = detach(instance_root=_absolute(args.instance_root, "instance_root"))
        elif args.command == "adopt":
            result = adopt(root=_absolute(args.root, "root"))
        elif args.command == "import-entry":
            from .shared_import import import_entry
            result = import_entry(root=_absolute(args.root, "root"), entry_id=args.entry,
                                  source=_absolute(args.source, "from"), dry_run=args.dry_run)
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
