"""Describe scope grants before confirmation without widening any registered entry."""

from __future__ import annotations

from pathlib import Path

from ..adapters.hermes.installation import build_installation_manifest
from ..adapters.hermes.shared_entries import read_shared_payload
from .install_common import InstallError


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
        return dict(
            policy="new private owner grant",
            source_entry=None,
            read_scopes=[scope],
            write_scopes=[scope],
            capture_scope=scope,
        )
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
