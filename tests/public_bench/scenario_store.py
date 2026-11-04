"""Synthetic shared installation exercised through shipped client and Hermes adapters."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.identity import trusted_context
from scope_recall.adapters.clients.mcp_server import CodexMCPServer
from scope_recall.adapters.hermes.installation import build_installation_manifest
from scope_recall.adapters.hermes.shared_entries import (
    attach_shared_entry,
    attach_shared_record,
    client_entry_record,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.core import CoreConfig, MemoryCore

HOSTS = ("codex", "claude-code", "workbuddy", "dsh")


class Clock:
    """Move business time explicitly while keeping real deadlines and durations."""

    def __init__(self):
        self.now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def utc_now(self):
        """Return this fixture's current business time."""
        return self.now

    def monotonic(self):
        """Keep admission and I/O deadlines on real elapsed time."""
        return time.monotonic()

    def advance(self, seconds):
        """Advance scheduling time without waiting or editing queue rows."""
        self.now = (datetime.fromisoformat(self.now) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def contains(packet, text):
    """Find fixture text in a serialized public adapter envelope."""
    return text in json.dumps(packet, ensure_ascii=False, default=str)


def host_text(packet):
    """Extract only text actually delivered to the host, excluding diagnostics."""
    return packet.get("hookSpecificOutput", {}).get("additionalContext", "")


def request_context(session=None):
    """Supply reserved Codex thread metadata, never payload-controlled identity."""
    return SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": session or str(uuid4())}))


class ScenarioStore:
    """Four independent real client bindings share only this synthetic store."""

    def __init__(self, root: Path):
        self.root, self.store, self.clock = root, root / "shared", Clock()
        root.mkdir(parents=True, exist_ok=False)
        write_shared_payload(self.store, new_shared_payload(self.store, agent_id="TEST-scenarios"))
        home = root / "hermes-home"
        home.mkdir()
        manifest = build_installation_manifest(
            home, agent_id="TEST-scenarios", user_id="TEST-owner", agent_workspace="TEST-project"
        )
        attach_shared_entry(self.store, manifest, entry_id="hermes", display_name="Hermes", now=self.clock.now)
        self.hermes_home = home
        owner = next(
            row for row in read_shared_payload(self.store)["entries"][0]["audiences"] if row["kind"] == "owner_private"
        )
        self.scope = owner["capture_scope_id"]
        self.owner_audience = owner
        self.hooks, self.mcp, self.configs = {}, {}, {}
        for host in HOSTS:
            self.attach(host, owner)

    def attach(self, host, owner, *, entry=None):
        """Attach one disposable home with the same explicit owner audience."""
        entry = entry or host
        client = self.root / f"{entry}-home"
        attach_shared_record(
            self.store,
            client_entry_record(
                host=host,
                home=client,
                entry_id=entry,
                display_name=host,
                attached_at=self.clock.now,
                allowed_scope_ids=owner["allowed_scope_ids"],
                writable_scope_ids=owner["writable_scope_ids"],
                capture_scope_id=self.scope,
            ),
            now=self.clock.now,
        )
        config = load_shared_client(client, host=host)
        core = MemoryCore(CoreConfig(config.to_binding()), clock=self.clock)
        core.initialize()
        self.configs[entry] = config
        self.hooks[entry] = CodexHookHandler(config, core=core, clock=self.clock)
        self.mcp[entry] = CodexMCPServer(config, workspace=self.root, core=core)

    def context(self, host="codex", session=None):
        """Resolve the same trusted context the real MCP entry uses."""
        return trusted_context(
            self.configs[host],
            self.mcp[host].audience,
            session_id=session or str(uuid4()),
            actor_origin="human_direct",
            project_root=str(self.root),
        )

    @property
    def core(self):
        """The sender core; other entries retain their own core instances."""
        return self.hooks["codex"].core

    def hook(self, host, prompt, *, session=None, event="UserPromptSubmit", **fields):
        """Submit a fresh host envelope and return output, elapsed ms and diagnostics."""
        session = session or str(uuid4())
        identifier = str(uuid4())
        value = dict(
            hook_event_name=event,
            session_id=session,
            turn_id=identifier,
            prompt_id=identifier,
            generation_id=identifier,
            cwd=str(self.root),
            prompt=prompt,
            **fields,
        )
        started = time.perf_counter()
        result = self.hooks[host].handle_payload(value)
        return dict(
            output=result,
            ms=(time.perf_counter() - started) * 1000,
            session=session,
            diagnostics=asdict(self.hooks[host].diagnostics),
        )

    def recall(self, host, query, *, mode="current", session=None, budget=4096):
        """Use the real MCP recall facade and preserve the complete packet."""
        started = time.perf_counter()
        value = self.mcp[host].recall(
            request_context(session), "1.1", query, mode, 10, budget, request_id=f"TEST-{uuid4().hex}"
        )
        return dict(output=value, ms=(time.perf_counter() - started) * 1000)

    def rows(self, sql="SELECT * FROM source_events", args=()):
        """Read fixture truth for provenance checks; SQL is authored by tests only."""
        with sqlite3.connect(self.store / "memory.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, args)]

    def source(self, text):
        """Read the unique captured fixture source rather than constructing evidence."""
        row = self.rows("SELECT event_id,source_revision FROM source_events WHERE content=?", (text,))[-1]
        return self.core.source(self.context(), row["event_id"], row["source_revision"])

    def close(self):
        """Flush and close only resources created by this installation."""
        for hook in self.hooks.values():
            hook.close()
        for server in self.mcp.values():
            diagnostics = getattr(server.core, "recall_diagnostics", None)
            if callable(getattr(diagnostics, "close", None)):
                diagnostics.close()


def rendered_contains(text, value):
    """Accept Unicode literals or JSON-escaped Unicode inside delivered objects."""
    return value in text or json.dumps(value, ensure_ascii=True)[1:-1] in text
