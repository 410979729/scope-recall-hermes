"""Describe scope grants before confirmation without widening any registered entry."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ..adapters.hermes.installation import build_installation_manifest
from ..adapters.hermes.shared_entries import read_shared_payload
from .install_common import InstallError
from .install_hermes import profile_identity

#: The local surfaces a new Hermes entry approves as the owner's own, beside the CLI (``adapters/audiences``).
#: Hermes names a session ``desktop`` in the Desktop app's chat panel, which reaches the agent from the app's own
#: backend on this machine: a loopback listener on a port of its own, or the gateway's local session transport, which
#: serves only the account the gateway runs as.  A session there that carries a dashboard login is a principal of its
#: own.  ``tui`` is not here: the browser dashboard's chat tab runs the TUI, and a dashboard on loopback can serve it
#: without a login to whatever reaches its port, a tunnel included; only the owner can say that nobody else does.
#: An existing entry keeps the grants it holds.
SETUP_LOCAL_PLATFORMS = ("desktop",)


def profile_repair(home: Path) -> dict | None:
    """Allow an old default setup identity to change only after checking its captured sources."""
    from ..adapters.shared_entries import read_attachment

    pointer = read_attachment(home)
    if pointer is None or pointer.host != "hermes":
        return None
    payload = read_shared_payload(pointer.root)
    record = next(item for item in payload["entries"] if item["entry_id"] == pointer.entry_id)
    identity = profile_identity(home)
    if record.get("agent_identity", payload["agent_id"]) == identity:
        return None
    if "agent_identity" in record or payload["agent_id"] != "scope-recall":
        raise InstallError("Hermes entry identity differs from its profile; preserve the existing binding")
    database = pointer.root / "memory.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        count = db.execute("SELECT count(*) FROM source_events WHERE entry_id=?", (pointer.entry_id,)).fetchone()[0]
    if count:
        raise InstallError(
            "Hermes entry identity repair refused: captured sources exist; preserve the existing binding"
        )
    return dict(entry_id=pointer.entry_id, agent_identity=identity, captured_sources=count)


def repair_profile(home: Path) -> dict | None:
    """Back up and correct an unused entry without rekeying the shared database or its grants."""
    from ..adapters.shared_entries import read_attachment, write_shared_payload
    from .shared_run import Run, home_lock, store_lock

    pointer = read_attachment(home)
    if pointer is None:
        return None
    from .shared import _now

    now = _now()
    with home_lock(home, "setup profile identity", now), store_lock(pointer.root, "setup profile identity", now):
        repair = profile_repair(home)
        if repair is None:
            return None
        payload = read_shared_payload(pointer.root)
        record = next(item for item in payload["entries"] if item["entry_id"] == pointer.entry_id)
        run = Run(pointer.root, "setup-profile-identity", now)
        run.keep(pointer.root / "installation.json", "store")
        record["agent_identity"] = repair["agent_identity"]
        write_shared_payload(pointer.root, payload)
        repair["receipt"] = run.receipt(repair)
        return repair


def scope_choice(home: Path, host: str, store: Path, *, share_with: str | None, fresh: bool) -> dict:
    """Require a named source when existing owner grants disagree; equivalent rows need no extra question."""
    if not fresh:
        return dict(policy="preserve existing grants", source_entry=None)
    payload = read_shared_payload(store) if (store / "installation.json").is_file() else None
    entries = [entry for entry in (payload or {}).get("entries", []) if not entry.get("detached_at")]
    if not entries or host == "hermes":
        agent = payload["agent_id"] if payload else "scope-recall"
        manifest = build_installation_manifest(home, agent_id=agent, agent_workspace="hermes")
        scope = manifest.audience_scopes["owner_private"]
        choice = dict(
            policy="new private owner grant",
            source_entry=None,
            read_scopes=[scope],
            write_scopes=[scope],
            capture_scope=scope,
        )
        if host == "hermes":
            choice["local_platforms"] = list(SETUP_LOCAL_PLATFORMS)
        return choice
    choices = {}
    for entry in entries:
        owners = [row for row in entry["audiences"] if row["kind"] == "owner_private"]
        read = sorted({scope for row in owners for scope in row["allowed_scope_ids"]})
        write = sorted({scope for row in owners for scope in row["writable_scope_ids"]})
        capture = {row["capture_scope_id"] for row in owners}
        if len(capture) == 1:
            choices[entry["entry_id"]] = dict(
                policy="copy approved owner grants",
                source_entry=entry["entry_id"],
                read_scopes=read,
                write_scopes=write,
                capture_scope=next(iter(capture)),
            )
    if share_with:
        if share_with not in choices:
            raise InstallError("--share-with must name an active entry with one owner capture scope")
        selected = choices[share_with]
        if any(selected["capture_scope"] not in item["read_scopes"] for item in choices.values()):
            raise InstallError("selected capture scope must be readable by every existing owner entry")
        return selected
    signatures = {
        (tuple(row["read_scopes"]), tuple(row["write_scopes"]), row["capture_scope"]) for row in choices.values()
    }
    if len(signatures) != 1:
        raise InstallError("owner grants differ; select --share-with from: " + ", ".join(sorted(choices)))
    return choices[sorted(choices)[0]]
