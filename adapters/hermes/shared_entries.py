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
        if record.get("recovery_pending"):
            raise HermesIdentityError("restored entry needs an explicit attach")
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
    already registers (``maintenance/shared.py`` takes them from the owner rows of the
    entries already attached), so no other entry's binding changes; the store's first
    client brings the owner scope of a fresh installation, as a Hermes home does its own.
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
    # Another home's pointer is read here without that home's lock.  A pointer is written to name this store only by
    # an attach to it, which holds the store's lock as this one does (``maintenance/shared.py``), and is removed from it
    # only by a detach holding that lock too: while this one runs, another command can only point such a home away from
    # this store.  Read before that, the pointer refuses this attach, which a run again lets through; two homes never
    # come to hold one entry.
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


# -- an attached entry's grants ---------------------------------------------------------
#
# An entry keeps the grants it was attached with until the operator changes them here: its record's
# owner principals, the capture scope of each audience kind, and its exact audience rows.  ``entry_grants`` gives
# them as a document to edit, ``regranted`` checks an edited one as attach checks a record, and the
# command writes the manifest it returns (``maintenance/shared.py``).  What the store holds keeps its scopes; only
# who reads and writes them changes.

GRANTS_SCHEMA = "scope-recall.entry-grants/1"
_GRANT_FIELDS = ("owner_principals", "audience_scopes", "audiences")


def attached_again(
    payload: dict[str, Any],
    *,
    entry_id: str,
    host: str,
    home: Path,
    display_name: str,
    attached_at: str,
    python_executable: str | None = None,
) -> dict[str, Any] | None:
    """The record an attach writes for ``home`` when the store whose manifest ``payload`` is holds it attached as
    ``entry_id`` already, for ``host``; ``None`` when it does not.

    The entry keeps the grants it holds: an attach run again finishes or repeats the first, or points the home at a
    store moved elsewhere, and grants taken anew, from an installation or like other entries, would undo a change made
    since (``regranted``).  Its name, moment and interpreter are this attach's.
    """
    for entry in payload["entries"]:
        if (
            entry["entry_id"] == entry_id
            and entry["host"] == host
            and not entry.get("detached_at")
            and (_same_path(entry["home"], home) or entry.get("recovery_pending") is True)
        ):
            record = {
                name: value for name, value in entry.items() if name not in {"python_executable", "recovery_pending"}
            }
            record["home"] = str(home.resolve())
            record["display_name"] = _display_name(display_name)
            record["attached_at"] = bounded_text(attached_at, field="attached_at")
            if python_executable is not None:
                record["python_executable"] = bounded_text(python_executable, field="python_executable")
            return record
    return None


def _attached(hermes_home: Path | str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """The store a home is attached to, its manifest, and the home's entry record; refused for any other home."""
    home = Path(str(hermes_home)).expanduser().resolve()
    attachment = read_attachment(home)
    if attachment is None:
        raise HermesIdentityError("this home is not attached to a shared store")
    payload = read_shared_payload(attachment.root)
    record = next((entry for entry in payload["entries"] if entry["entry_id"] == attachment.entry_id), None)
    if record is None or record.get("detached_at") or not _same_path(record["home"], home):
        raise HermesIdentityError("the shared store holds no attached entry of this home")
    return attachment.root, payload, record


def entry_grants(hermes_home: Path | str) -> dict[str, Any]:
    """The grants of the entry ``hermes_home`` is attached as, as a document to edit and hand to ``regranted``."""
    _store, _payload, record = _attached(hermes_home)
    return {
        "schema": GRANTS_SCHEMA,
        "entry_id": record["entry_id"],
        **{name: json.loads(json.dumps(record[name])) for name in _GRANT_FIELDS},
    }


def regranted(
    hermes_home: Path | str, grants: object
) -> tuple[Path, InstallationManifest, dict[str, Any], dict[str, Any], dict[str, Any], InstallationManifest]:
    """The shared store with the entry of ``hermes_home`` holding ``grants`` (an edited ``entry_grants``) in place of
    its own, checked as a whole as ``attach_shared_record`` checks a record: (store, the entry's view now, record
    now, record after, manifest after, the entry's view after).  Nothing is written.

    Rows stay exact (``audience_entry``).  An owner row whose user is no owner principal grants nothing; it is
    allowed, as an installation may hold one (``doctor``'s ``audience_owner_unverified``), so that the grants before a
    change always apply back, and the command names it.  Only scopes the store holds are named, and none is left
    held by no entry (what is in it would leave every binding, the worker's included; ``detach`` keeps an entry's
    scopes on record): the store's own scopes never change here, so the worker's binding stays and the grants before
    a change can always be applied back.  A client's own route must
    still read and capture: the caller checks that against the route the client binds with
    (``maintenance/shared.py``), which this module cannot import."""
    store, payload, record = _attached(hermes_home)
    if not isinstance(grants, dict) or grants.get("schema") != GRANTS_SCHEMA:
        raise HermesIdentityError(f"not an entry grants document ({GRANTS_SCHEMA}): start from the entry's own")
    if grants.get("entry_id") != record["entry_id"]:
        raise HermesIdentityError("the grants document is another entry's")
    if set(grants) != {"schema", "entry_id", *_GRANT_FIELDS}:
        raise HermesIdentityError("an entry grants document holds schema, entry_id, " + ", ".join(_GRANT_FIELDS))
    check = dict(REQUIRED_FIELDS)
    rows = [dict(row) for row in check["audiences"](grants["audiences"])]
    principals = [dict(item) for item in check["owner_principals"](grants["owner_principals"])]
    kinds = check["audience_scopes"](grants["audience_scopes"])
    mapped = sorted({scope_id for row in rows for scope_id in row["allowed_scope_ids"]})
    if not set(kinds.values()) <= set(mapped):
        raise HermesIdentityError("audience_scopes names a scope no row of the entry reads")
    unknown = set(mapped) - set(payload["scope_ids"])
    if unknown:
        raise HermesIdentityError(
            f"{len(unknown)} scope(s) the store does not hold: grants change who reads and writes the store's scopes, "
            "and a scope comes into the store with an entry that holds it (attach)"
        )
    after = {**record, "scope_ids": mapped, "owner_principals": principals, "audience_scopes": kinds, "audiences": rows}
    entries = [after if entry["entry_id"] == record["entry_id"] else entry for entry in payload["entries"]]
    held = {scope_id for entry in entries for scope_id in entry["scope_ids"]}
    orphaned = set(payload["scope_ids"]) - held
    if orphaned:
        raise HermesIdentityError(
            f"{len(orphaned)} scope(s) would be held by no entry, and what is in them would leave every binding: keep "
            "each on a row of some entry (one that only reads will do), or detach the entry instead"
        )
    updated = _checked_shared_payload({**payload, "entries": entries, "scope_ids": sorted(held)})
    return store, _entry_view(store, payload, record), record, after, updated, _entry_view(store, updated, after)
