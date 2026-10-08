"""A home's attachment to a shared store, the store's record of its entries, and attaching an entry to it."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scope_recall.contracts import ENTRY_ID, InstanceBinding, TrustedContext
from scope_recall.core.storage import SQLiteStorage

from .audiences import (
    LOCAL_USER_ID,
    HermesIdentityError,
    audience_entry,
)
from .installation import (
    MANIFEST_FILENAME,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    InstallationManifest,
    bounded_text,
    load_installation_manifest,
    scope_id_list,
    validate_archive_fields,
)

ATTACHMENT_FILENAME = "attachment.json"


ATTACHMENT_SCHEMA = "scope-recall.attachment/1"


SHARED_SCHEMA_VERSION = "scope-recall.shared-installation.v1"


SHARED_ID = re.compile(r"shared-install:[0-9a-f]{32}")


#: Every entry's audience rows; an instance migrated from 2.x brings about a hundred.
_MAX_SHARED_MANIFEST_BYTES = 4 * 1024 * 1024


_MAX_ATTACHMENT_BYTES = 4096


_MAX_DISPLAY_NAME = 32


#: The local coding assistants a shared store takes as entries beside Hermes homes.  Such an
#: entry has no installation of its own to carry grants over from: it is the owner at this
#: machine, with one owner row on the platform named after the client (``client_entry_record``).
CLIENT_HOSTS = ("codex", "claude-code", "workbuddy", "dsh")


ENTRY_HOSTS = ("hermes", *CLIENT_HOSTS)


@dataclass(frozen=True)
class Attachment:
    """The pointer that makes a home an entry of a shared store."""

    root: Path
    entry_id: str
    display_name: str
    host: str = "hermes"


def _read_json(path: Path, *, limit: int, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise HermesIdentityError(f"{what} is required")
    if path.stat().st_size > limit:
        raise HermesIdentityError(f"{what} exceeds bounded size")
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError as exc:
            # Windows refuses a read while an attach replaces the file; the
            # replace takes milliseconds, and a session switch should not fail on it.
            if attempt == 2:
                raise HermesIdentityError(f"{what} is invalid") from exc
            time.sleep(0.02)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HermesIdentityError(f"{what} is invalid") from exc
    if not isinstance(payload, dict):
        raise HermesIdentityError(f"{what} is invalid")
    return payload


def _entry_id(value: object) -> str:
    if type(value) is not str or not ENTRY_ID.fullmatch(value):
        raise HermesIdentityError(
            "entry_id must be 2 to 32 lowercase letters, digits or hyphens, starting with a letter"
        )
    return value


def _display_name(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value.strip()) > _MAX_DISPLAY_NAME:
        raise HermesIdentityError("display_name is required and at most 32 characters")
    return value.strip()


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def attachment_path(hermes_home: Path) -> Path:
    return hermes_home / "scope-recall" / ATTACHMENT_FILENAME


def _points_here(hermes_home: Path | str, store: Path, entry_id: str) -> bool:
    """Whether a home's pointer still names this store and entry; an unreadable one counts as yes."""
    try:
        attachment = read_attachment(hermes_home)
    except HermesIdentityError:
        return True
    return attachment is not None and attachment.entry_id == entry_id and _same_path(attachment.root, store)


def read_attachment(hermes_home: Path | str) -> Attachment | None:
    """This home's pointer to a shared store, or ``None`` when it has none."""
    path = attachment_path(Path(str(hermes_home)).expanduser().resolve())
    if not os.path.lexists(path):
        return None
    payload = _read_json(path, limit=_MAX_ATTACHMENT_BYTES, what="shared store attachment")
    if payload.get("schema") != ATTACHMENT_SCHEMA:
        raise HermesIdentityError("unsupported shared store attachment schema")
    if payload.get("host") not in ENTRY_HOSTS:
        raise HermesIdentityError("shared store attachment names an unknown host")
    root = Path(str(payload.get("root") or ""))
    if not root.is_absolute():
        raise HermesIdentityError("shared store attachment root must be absolute")
    return Attachment(
        root.resolve(), _entry_id(payload.get("entry_id")), _display_name(payload.get("display_name")), payload["host"]
    )


def read_shared_payload(root: Path | str) -> dict[str, Any]:
    """A shared store's manifest, checked as a whole."""
    store = Path(str(root)).expanduser().resolve()
    return _checked_shared_payload(
        _read_json(store / MANIFEST_FILENAME, limit=_MAX_SHARED_MANIFEST_BYTES, what="shared store manifest")
    )


def _checked_shared_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != SHARED_SCHEMA_VERSION or payload.get("installation_kind") != "shared":
        raise HermesIdentityError("not a shared store manifest")
    if type(payload.get("installation_id")) is not str or not SHARED_ID.fullmatch(payload["installation_id"]):
        raise HermesIdentityError("shared store installation_id invalid")
    bounded_text(payload.get("agent_id"), field="agent_id")
    if type(payload.get("test_mode")) is not bool:
        raise HermesIdentityError("shared store test_mode must be a boolean")
    entries = payload.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise HermesIdentityError("shared store entries invalid")
    ids = [_entry_id(entry.get("entry_id")) for entry in entries]
    if len(set(ids)) != len(ids):
        raise HermesIdentityError("shared store entries repeat an entry_id")
    carried: set[str] = set()
    for entry in entries:
        carried |= scope_id_list(entry.get("scope_ids"))
    scope_ids = payload.get("scope_ids")
    if (
        not isinstance(scope_ids, list)
        or any(type(scope_id) is not str for scope_id in scope_ids)
        or set(scope_ids) != carried
        or len(set(scope_ids)) != len(scope_ids)
    ):
        raise HermesIdentityError("shared store scope_ids must be exactly its entries' scopes")
    return payload


def shared_entry_manifest(root: Path | str, entry_id: str, *, hermes_home: Path | None = None) -> InstallationManifest:
    """One entry's view of a shared store: its own grants, the store's identity and directory.

    With ``hermes_home`` the view is for that home binding: the entry must be
    attached from it and not detached, so a pointer copied into another home
    binds nothing.  Without it the view is for re-checking a capture the entry
    made, which a detached entry's captures still get.
    """
    store = Path(str(root)).expanduser().resolve()
    payload = read_shared_payload(store)
    record = next((entry for entry in payload["entries"] if entry["entry_id"] == entry_id), None)
    if record is None:
        raise HermesIdentityError("shared store has no such entry")
    if hermes_home is not None:
        if record.get("detached_at"):
            raise HermesIdentityError("shared store entry is detached")
        if not _same_path(bounded_text(record.get("home"), field="home"), hermes_home):
            raise HermesIdentityError("shared store entry belongs to another home")
    return _entry_view(store, payload, record)


def _entry_view(store: Path, payload: dict[str, Any], record: Mapping[str, Any]) -> InstallationManifest:
    if record.get("host") not in ENTRY_HOSTS:
        raise HermesIdentityError("shared store entry names an unknown host")
    home = Path(bounded_text(record.get("home"), field="home"))
    if not home.is_absolute():
        raise HermesIdentityError("shared store entry home must be absolute")
    fields = {name: check(record.get(name)) for name, check in REQUIRED_FIELDS}
    manifest = InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=payload["installation_id"],
        agent_id=payload["agent_id"],
        data_directory=store,
        test_mode=payload["test_mode"],
        hermes_home=home.resolve(),
        installation_kind="shared",
        entry_id=_entry_id(record.get("entry_id")),
        entry_name=_display_name(record.get("display_name")),
        entry_host=record["host"],
        **fields,
    )
    validate_archive_fields(manifest, present=False, test_mode=payload["test_mode"])
    return manifest


def load_binding_for_home(hermes_home: Path | str) -> InstallationManifest:
    """The manifest a Hermes home binds with.

    A home holding a pointer is an entry of that shared store; any other home is
    its own installation, read exactly as ``load_installation_manifest`` reads it.
    """
    home = Path(str(hermes_home)).expanduser().resolve()
    attachment = read_attachment(home)
    if attachment is None:
        return load_installation_manifest(home)
    if os.path.lexists(home / "scope-recall" / MANIFEST_FILENAME):
        raise HermesIdentityError("home holds both its own installation and a shared store attachment")
    manifest = shared_entry_manifest(attachment.root, attachment.entry_id, hermes_home=home)
    if attachment.host != "hermes" or manifest.entry_host != "hermes":
        raise HermesIdentityError("this home is attached to a shared store as another host's entry")
    return manifest


def new_shared_payload(root: Path | str, *, agent_id: str = "default", test_mode: bool = False) -> dict[str, Any]:
    """A shared store with no entries yet.  Its id is drawn once and never changes;
    ``data_directory`` only records where it was created, and ``adopt`` rewrites it."""
    return {
        "schema_version": SHARED_SCHEMA_VERSION,
        "installation_kind": "shared",
        "installation_id": f"shared-install:{secrets.token_hex(16)}",
        "agent_id": bounded_text(agent_id, field="agent_id"),
        "data_directory": str(Path(str(root)).expanduser().resolve()),
        "test_mode": bool(test_mode),
        "scope_ids": [],
        "entries": [],
    }


def _replace_file(directory: Path, target: Path, text: str) -> None:
    """Write ``target`` in one step: a reader sees the old file or the new one."""
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=f".{target.stem}-", suffix=target.suffix, delete=False
    )
    with handle:
        handle.write(text)
    staged = Path(handle.name)
    for attempt in range(40):
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            # Windows refuses while a reader holds the file open for a moment.
            if attempt == 39:
                staged.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def write_shared_payload(root: Path | str, payload: dict[str, Any]) -> Path:
    store = Path(str(root)).expanduser().resolve()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_SHARED_MANIFEST_BYTES:
        raise HermesIdentityError("shared store manifest exceeds bounded size")
    store.mkdir(parents=True, exist_ok=True)
    _replace_file(store, store / MANIFEST_FILENAME, encoded)
    return store / MANIFEST_FILENAME


def shared_entry_record(
    source: InstallationManifest,
    *,
    entry_id: str,
    display_name: str,
    attached_at: str,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """An entry's grants, carried over from the local installation it was.

    The audience rows are the ones the owner approved for that installation,
    unchanged, so every chat reaches what it reached before and the owner's
    chats meet in the scopes the installations already share.  Archive and
    retained scopes stay with the old store: a shared store starts empty, and
    ``import-entry`` brings the old store's memories in.
    """
    if source.installation_kind != "local":
        raise HermesIdentityError("an entry is carried over from a local installation")
    rows = [dict(row) for row in source.audiences]
    mapped = sorted({scope_id for row in rows for scope_id in row["allowed_scope_ids"]})
    record: dict[str, Any] = {
        "entry_id": _entry_id(entry_id),
        "display_name": _display_name(display_name),
        "host": "hermes",
        "home": str(source.hermes_home),
        "attached_at": bounded_text(attached_at, field="attached_at"),
        "scope_ids": mapped,
        "owner_principals": [dict(item) for item in source.owner_principals],
        "audience_scopes": {kind: scope for kind, scope in source.audience_scopes.items() if scope in mapped},
        "audiences": rows,
    }
    if python_executable is not None:
        record["python_executable"] = bounded_text(python_executable, field="python_executable")
    return record


def client_entry_record(
    *,
    host: str,
    home: Path | str,
    entry_id: str,
    display_name: str,
    attached_at: str,
    allowed_scope_ids: Sequence[str],
    writable_scope_ids: Sequence[str],
    capture_scope_id: str,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """A local client's entry: the owner at this machine, reading and writing what it is given.

    A client names no user, like the Hermes CLI, so attaching it is the operator's
    statement that whoever types into it here is the owner: the owner principal
    ``(host, "local")`` and one owner row on that route.  The scopes are ones the store
    already registers (``maintenance/shared.py`` takes them from the Hermes entries'
    owner rows), so no other entry's binding changes.
    """
    if host not in CLIENT_HOSTS:
        raise HermesIdentityError(f"a client entry's host is one of {', '.join(CLIENT_HOSTS)}")
    row = audience_entry(
        platform=host,
        user_id=LOCAL_USER_ID,
        chat_type="private",
        chat_id=LOCAL_USER_ID,
        thread_id="main",
        gateway_session_key="",
        agent_workspace="default",
        allowed_scope_ids=sorted(set(allowed_scope_ids)),
        writable_scope_ids=sorted(set(writable_scope_ids)),
        capture_scope_id=capture_scope_id,
        kind="owner_private",
    )
    record: dict[str, Any] = {
        "entry_id": _entry_id(entry_id),
        "display_name": _display_name(display_name),
        "host": host,
        "home": str(Path(str(home)).expanduser().resolve()),
        "attached_at": bounded_text(attached_at, field="attached_at"),
        "scope_ids": list(row["allowed_scope_ids"]),
        "owner_principals": [dict(platform=host, user_id=LOCAL_USER_ID)],
        "audience_scopes": {"owner_private": capture_scope_id},
        "audiences": [row],
    }
    if python_executable is not None:
        record["python_executable"] = bounded_text(python_executable, field="python_executable")
    return record


def attach_shared_entry(
    root: Path | str,
    source: InstallationManifest,
    *,
    entry_id: str,
    display_name: str,
    now: str,
    python_executable: str | None = None,
) -> InstallationManifest:
    """Make ``source``'s home an entry of the shared store at ``root``, with ``source``'s grants."""
    payload = read_shared_payload(root)
    if (source.agent_id, source.test_mode) != (payload["agent_id"], payload["test_mode"]):
        raise HermesIdentityError("installation agent_id or test_mode differs from the shared store's")
    record = shared_entry_record(
        source, entry_id=entry_id, display_name=display_name, attached_at=now, python_executable=python_executable
    )
    return attach_shared_record(root, record, now=now)


def attach_shared_record(root: Path | str, record: dict[str, Any], *, now: str) -> InstallationManifest:
    """Make ``record``'s home an entry of the shared store at ``root``.

    Everything is checked before anything is written.  Then the store (its
    scopes, then the entry), the store's manifest, and last the pointer: stopped
    anywhere, running it again finishes the job, and until the pointer exists
    the home binds nothing new.  Returns the entry's view.
    """
    store = Path(str(root)).expanduser().resolve()
    payload = read_shared_payload(store)
    home = Path(record["home"])
    for entry in payload["entries"]:
        if (
            entry["entry_id"] == record["entry_id"]
            and not _same_path(entry["home"], home)
            and _points_here(entry["home"], store, entry["entry_id"])
        ):
            # A home that no longer points here gives its id up: the store was
            # copied to another machine and adopted, or the home was detached.
            raise HermesIdentityError("entry_id is already attached from another home")
        if entry["entry_id"] != record["entry_id"] and _same_path(entry["home"], home) and not entry.get("detached_at"):
            raise HermesIdentityError("home is already attached as another entry")
    before = frozenset(payload["scope_ids"])
    after = before | frozenset(record["scope_ids"])
    updated = _checked_shared_payload(
        {
            **payload,
            "entries": [entry for entry in payload["entries"] if entry["entry_id"] != record["entry_id"]] + [record],
            "scope_ids": sorted(after),
        }
    )
    view = _entry_view(store, updated, record)

    def binding(scope_ids: frozenset[str]) -> InstanceBinding:
        return InstanceBinding(
            payload["agent_id"], payload["installation_id"], store, scope_ids, payload["test_mode"], "shared"
        )

    if not (store / "memory.sqlite3").exists():
        SQLiteStorage(binding(after)).initialize()
        before = after
    storage = SQLiteStorage(binding(before or after))
    context = TrustedContext(storage.binding, "shared-store-attach", storage.binding.scope_ids, "host_generated")
    with storage.write(context) as tx:
        tx.registry.register_scopes(after)
        tx.registry.register_entry(view.entry_id, view.entry_name, view.entry_host, now=now)
    write_shared_payload(store, updated)
    pointer = {
        "schema": ATTACHMENT_SCHEMA,
        "root": str(store),
        "entry_id": view.entry_id,
        "display_name": view.entry_name,
        "host": view.entry_host,
        "attached_at": now,
    }
    path = attachment_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(path.parent, path, json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return view
