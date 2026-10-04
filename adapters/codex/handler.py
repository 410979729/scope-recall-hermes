"""Dispatch Codex (and Claude Code) hook events through the single MemoryCore boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Protocol, cast

from scope_recall.contracts import ContractError, Origin, RecallRequest, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.capture_inbox import DELETED_KEY
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS
from scope_recall.core.secret_patterns import contains_secret_like_text
from scope_recall.runtime.instance import RuntimeInstanceConfig
from ..runtime_wiring import _strict_hook_budget, render_host_recall_context

from . import transcript
from .boundary import (
    without_lone_surrogates,
    assistant_stop_source_event,
    authorized_attachment_refs,
    host_source_key,
    is_codex_suggestions_prompt,
    is_codex_suggestions_reply,
    is_task_notification,
    is_workbuddy_agent_run,
    is_workbuddy_notice,
    lifecycle_source_event,
    recorded_source_event,
    tool_use_source_event,
    turn_id_from_payload,
    user_prompt_source_event,
    workbuddy_person_text,
)
from .config import CodexConfigError, CodexInstallationConfig, SharedClientConfig, load_codex_config, load_shared_client
from .identity import resolve_runtime_audience, trusted_context
from .runtime_wiring import (
    GAP_UNCONFIGURED,
    GAP_WORKER_LAUNCH_FAILED,
    TrustedHostRuntime,
    attach_trusted_host_runtime,
)


_MAX_STDIN_BYTES = 65536
_CAPTURE_TIMEOUT_S = 1.0
#: The owner's own message may wait longer for the writer lease: another agent's long reply can hold it 1-2 s
#: while it is matched against the candidates, and in the work computer's first day 12 of its 55 prompts
#: waited their one second and were not stored.  It takes at most half of what the hook has left, and never
#: less than the one second every other capture waits, so a 6 s budget waits 2 s and a 2 s one still 1 s.
_PROMPT_CAPTURE_TIMEOUT_S = 2.0
#: The longest query a recall request carries (``contracts/recall_request.schema.json``).  A longer prompt is
#: searched by its first part, as Hermes does: whole, it failed the request and the turn had no recall at all.
_RECALL_QUERY_CHARS = 8192
_TOTAL_BUDGET_S = 2.0
#: Attaching the trusted runtime after a capture needs this much budget left.
_RUNTIME_ATTACH_MIN_S = 0.3
#: When a prompt hook that asked the entry's server (``resident_recall``) recalls as well, if the server has not
#: answered: with this much left.  The helper the hook started at its own start is ready by then.
_LOCAL_RECALL_RESERVE_S = 1.5
#: The least a server is asked with: in less it could not be found, prove itself and answer.
_RESIDENT_MIN_S = 1.0
#: An embedding call's failures that its connection or its worker made, not the provider (``_server_own_vector_fault``).
_CONNECTION_FAULTS = frozenset({"network_error", "http_protocol"})
#: What a server's recall may report of how it ended, besides its vector gap and its error.
_RESIDENT_REASONS = frozenset({"deadline_exceeded", "recall_exception", "recall_incomplete"})
#: Capture refusals a second attempt meets again.
_SETTLED_CAPTURE_CODES = frozenset({"SECRET_DETECTED", "INPUT_INVALID", "VERSION_CONFLICT"})
#: How a capture says it refused a message as holding a credential: as a code, or as the rejection it returns.
_SECRET_REFUSALS = frozenset({"SECRET_DETECTED", "plaintext_secret_rejected"})
_CAPTURE_ERROR_CODES = frozenset({
    "ACCESS_DENIED", "IDENTITY_UNBOUND", "INPUT_INVALID", "VERSION_CONFLICT",
    "DEADLINE_EXCEEDED", "STORAGE_UNAVAILABLE", "SOURCE_MISSING", "SECRET_DETECTED",
})
_SUPPORTED_EVENTS = frozenset(
    {"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "Interrupt", "SessionEnd"}
)
#: Where each client's hooks differ.  Claude Code's were the model for Codex's and send the
#: same fields, except that a turn is named by ``prompt_id``.  Its tool output is not recorded:
#: a tool result never becomes a memory, and a coding session's tool traffic would be most of
#: the store for an embedding each.
_TURN_FIELD = {"codex": "turn_id", "claude-code": "prompt_id", "workbuddy": "generation_id", "dsh": "turn_id"}
#: Clients whose prompt hook may run the entry's ``hook_processing_seconds`` (at most 6 s) from the
#: start.  Both wait 15 s for a prompt's hook (``maintenance/install_claude_code.py``,
#: ``maintenance/install_codex.py``), and recall on the pilot's shared store took 2.7-5.7 s: with 2 s most
#: automatic recalls came back empty, as Codex's did until 3.4.0rc5.  The budget bounds the work; the hook
#: answers as soon as it is done.  WorkBuddy waits 60 s unless its hook says otherwise, and a prompt hook
#: that runs past its wait blocks the prompt: the budget is what keeps it inside.
_CONFIGURED_PROMPT_BUDGET = frozenset({"claude-code", "codex", "workbuddy", "dsh"})
#: Clients whose Stop and SessionEnd also read the session record (``transcript``): what the person said,
#: whatever the prompt hook could not write, and what the model said while it worked.  Claude Code waits
#: 10 s for these hooks; a turn's lines take well under a second, and a long backlog is read over several
#: turns, at most ``_RECORD_READ_S`` each, so the end of a turn is not held up.
#: dsh has no record a hook can read (its session log is compressed); its plugin sends the turn's messages with the Stop
#: as the lines a remote client sends (``transcript.dsh_lines``).
_READS_RECORD = frozenset({"claude-code", "workbuddy", "dsh"})
_RECORD_READ_S = 3.0
#: A capture is started only with this much of the reading time left.
_RECORD_CAPTURE_MIN_S = 0.5
#: A hook's copy of a message and the record's are the same message when the words match and the moments are this close.
_RECORD_SAME_MESSAGE_S = 120.0
_HOST_EVENTS = {"codex": _SUPPORTED_EVENTS,
                "claude-code": frozenset({"UserPromptSubmit", "Stop", "SessionEnd"}),
                "workbuddy": frozenset({"UserPromptSubmit", "Stop", "SessionEnd"}),
                # dsh's plugin (``distribution/dsh``) sends a prompt hook before a turn's first step and a Stop at its
                # end; dsh has no session end.
                "dsh": frozenset({"UserPromptSubmit", "Stop"})}


class HookClock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemHookClock:
    def utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class HookDiagnostics:
    last_event: str | None = None
    last_reason: str | None = None
    capability_gaps: tuple[str, ...] = ()
    capture_stage: str | None = None
    capture_disposition: str | None = None
    capture_durability: str | None = None
    capture_error_type: str | None = None
    capture_error_code: str | None = None
    #: The code before the frozen allowlist collapsed it to CAPTURE_ERROR.
    #: ``capture_error_code`` is a contract the host reads and may only carry
    #: one of ``_CAPTURE_ERROR_CODES``; this keeps the original for the local
    #: stderr diagnostic line so a collapsed code is still diagnosable.  It
    #: never reaches the host.
    capture_error_detail: str | None = None
    capture_elapsed_ms: int | None = None
    #: What stopped an automatic recall (``recall_exception``): the exception's class and, for a contract error,
    #: its code.  Without it the server's log said only that a recall had failed.
    recall_error_detail: str | None = None
    #: Why the recall ran without its vector search (``recall_without_vectors``), when it did.  The packet carried
    #: that to the model and nowhere else: Claude Code and Codex recalled without their vector search for as long as
    #: anyone could tell, and no log showed it.
    recall_vector_gap: str | None = None
    #: Whether the recall ran its vector search: what a hook asks of its server's answer (``_resident_answer``).
    recall_vectors: bool | None = None
    #: How long attaching the runtime (vector store, embedding worker) took, when this hook attached it.
    runtime_attach_ms: int | None = None
    #: How long all of this hook's captures took: a Stop writes each session-record line (``capture_elapsed_ms`` is
    #: the last one's).
    capture_total_ms: int | None = None

    @property
    def capture_settled(self) -> bool:
        """No capture, or one stored, queued, or refused in a way no retry changes (a secret, an invalid message,
        its id already taken, its id's message deleted, or taken from the inbox by a pass that stored it).
        Otherwise the store was busy or away, and the same hook sent again may store it."""
        return (self.capture_stage is None or self.capture_durability in ("persisted", "queued")
                or self.capture_disposition in ("rejected", "conflict", "cancelled")
                or self.capture_error_code in _SETTLED_CAPTURE_CODES)


@dataclass
class RecordLines:
    """Lines a client on another machine read from its own session record, from ``start``.

    Each is the offset just past the line and what it shows being said (``transcript.said``); a line that
    shows nothing may be left out, as long as the last offset the client read is present.  The handler sets
    ``through`` to the offset every stored line reaches, which is where that client's cursor may move.
    """

    start: int
    lines: list[tuple[int, "transcript.Said | None"]]
    through: int | None = None


class CodexHookHandler:
    """Stateless per-process handler; durable idempotence lives in core SQLite."""

    def __init__(
        self,
        config: CodexInstallationConfig,
        *,
        core: MemoryCore | None = None,
        host_runtime: TrustedHostRuntime | None = None,
        clock: HookClock | None = None,
        hook_started_at: float | None = None,
    ) -> None:
        self.config = config
        self.host = config.host if isinstance(config, SharedClientConfig) else "codex"
        self._host_runtime = host_runtime
        if host_runtime is not None:
            if core is not None and core is not host_runtime.core:
                raise CodexConfigError("injected core mismatch")
            self.core = host_runtime.core
        elif core is None:
            self.core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
        else:
            if core.config.binding != config.to_binding():
                raise CodexConfigError("injected core binding mismatch")
            self.core = core
        self.clock = clock if clock is not None else SystemHookClock()
        if hook_started_at is not None and (
            type(hook_started_at) not in (int, float) or not math.isfinite(hook_started_at)
        ):
            raise CodexConfigError("invalid hook start time")
        self._hook_started_at = hook_started_at
        self._prompt_budget: float | None = None
        self.diagnostics = HookDiagnostics(
            capability_gaps=host_runtime.capability_gaps if host_runtime is not None else (GAP_UNCONFIGURED,)
        )
        self._persisted_this_call = False
        self._queued_this_call = False
        self._pending_runtime_config_path: str | None = None
        self._runtime_attach_attempted = host_runtime is not None
        #: The entry's running MCP server, asked for a prompt's recall (``local_endpoint.Recaller``): given the
        #: payload, the stored refs, the gaps and the seconds it may take, the result and its diagnostics, or None.
        self.resident_recall: Callable[..., tuple[dict[str, Any], dict[str, Any]] | None] | None = None
        #: How a prompt's recall went with the server, when the hook decided it (``_resident_answer``): ``slow``,
        #: ``late``, ``without_vectors:<gap>`` or ``failed:<reason>``.  The hook's stderr says this, or else what
        #: ``resident_recall`` says of itself.
        self.resident_outcome: str | None = None
        #: The turn a WorkBuddy Stop closed and the words of its reply, for the read of the record after it.
        self._closed_reply: tuple[str, str] | None = None
        #: Whether this hook may open the session record its payload names (``handle_payload``).
        self._local_record = True
        #: A client on another machine's word that its Stop's reply is an error its record marks (``handle_payload``).
        self._client_error_reply = False

    @classmethod
    def from_config_path(
        cls,
        config_path: str,
        *,
        core: MemoryCore | None = None,
        host_runtime: TrustedHostRuntime | None = None,
        clock: HookClock | None = None,
        trusted_runtime_config_path: str | None = None,
        hook_started_at: float | None = None,
    ) -> "CodexHookHandler":
        config = load_codex_config(config_path)
        if host_runtime is None and core is None:
            # Capture uses a basic Core first.  Trusted runtime attach
            # (Lance/aux/worker) waits until after a durable Source commit.
            core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
            handler = cls(config, core=core, clock=clock, hook_started_at=hook_started_at)
            handler._pending_runtime_config_path = trusted_runtime_config_path
            return handler
        if host_runtime is None:
            host_runtime = attach_trusted_host_runtime(
                config_path=trusted_runtime_config_path,
                expected_binding=config.to_binding(),
                session_id=f"codex-runtime:{config.installation_id}",
                allowed_scope_ids=config.scope_ids,
                core=core,
                clock=clock,
            )
        return cls(config, core=core, host_runtime=host_runtime, clock=clock, hook_started_at=hook_started_at)

    @classmethod
    def from_home(
        cls,
        home: str,
        host: str,
        *,
        clock: HookClock | None = None,
        event_clock: HookClock | None = None,
        trusted_runtime_config_path: str | None = None,
        hook_started_at: float | None = None,
    ) -> "CodexHookHandler":
        """A client attached to a shared store; its runtime config is the entry's, beside its pointer.

        ``event_clock``, when given, dates the hook's own events (a remote client's moment), while the store keeps
        ``clock``'s time for when it stored them.
        """
        config = load_shared_client(home, host)
        core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
        handler = cls(config, core=core, clock=event_clock if event_clock is not None else clock,
                      hook_started_at=hook_started_at)
        handler._pending_runtime_config_path = trusted_runtime_config_path or str(config.runtime_config_path)
        if host in _CONFIGURED_PROMPT_BUDGET:
            handler._prompt_budget = _configured_budget(handler._pending_runtime_config_path)
        return handler

    # -- diagnostics and budget ------------------------------------------

    def _merge_runtime_gaps(self, gaps: tuple[str, ...] = ()) -> None:
        runtime_gaps = self._host_runtime.capability_gaps if self._host_runtime is not None else ()
        merged = tuple(dict.fromkeys((*self.diagnostics.capability_gaps, *gaps, *runtime_gaps)))
        if merged:
            self.diagnostics.capability_gaps = merged

    def _diag(self, reason: str, *, gaps: tuple[str, ...] = ()) -> None:
        self.diagnostics.last_reason = reason
        if gaps:
            self._merge_runtime_gaps(gaps)

    def _note_capture_error(self, code: object) -> None:
        self.diagnostics.capture_error_code = code if code in _CAPTURE_ERROR_CODES else "CAPTURE_ERROR"
        self.diagnostics.capture_error_detail = _error_detail(code)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self.clock.monotonic())

    def _hook_budget(self) -> float:
        """Read only the verified host runtime budget; payloads cannot tune it."""
        if self._host_runtime is None:
            return self._prompt_budget or _TOTAL_BUDGET_S
        return self._host_runtime.hook_processing_seconds

    def _hook_deadline(self, budget: float) -> float:
        """Use the earliest controlled entry timestamp when provided."""
        started = self._hook_started_at
        if started is None:
            started = self.clock.monotonic()
        return started + budget

    def _captured_this_call(self) -> bool:
        return self._persisted_this_call or self._queued_this_call

    def _refused_this_call(self) -> bool:
        """The capture refused its message (a credential, or nothing left to store), rather than failing to write it."""
        diagnostics = self.diagnostics
        return diagnostics.capture_disposition == "rejected" or diagnostics.capture_error_detail in _SECRET_REFUSALS

    # -- trusted runtime -------------------------------------------------

    def _context(self, audience, session_id: str, origin: Origin) -> TrustedContext:
        return trusted_context(self.config, audience, session_id=session_id, actor_origin=origin)

    def _ensure_host_runtime(self, audience=None) -> None:
        """Attach Lance/worker runtime only after Source persist, or for wakeup."""
        if self._host_runtime is not None or self._runtime_attach_attempted:
            return
        self._runtime_attach_attempted = True
        session_id = f"{self.host}-runtime:{self.config.installation_id}"
        started = time.monotonic()
        try:
            partition = self._context(audience, session_id, "host_generated") if audience is not None else None
            host_runtime = attach_trusted_host_runtime(
                config_path=self._pending_runtime_config_path,
                expected_binding=self.config.to_binding(),
                session_id=session_id,
                allowed_scope_ids=self.config.scope_ids,
                host_adapter=self.host,
                core=self.core,
                clock=self.clock,
                project_id=partition.project_id if partition is not None else None,
                branch_id=partition.branch_id if partition is not None else None,
            )
        except Exception:
            self._diag("runtime_attach_failed", gaps=("capability_gap:trusted_runtime_invalid",))
            return
        finally:
            self.diagnostics.runtime_attach_ms = round((time.monotonic() - started) * 1000)
        self._host_runtime = host_runtime
        self.core = host_runtime.core
        self._merge_runtime_gaps()

    def _maybe_launch_owned_worker(self, session_id: str, audience, *, require_persisted: bool = True) -> None:
        if self._host_runtime is None or not self._host_runtime.configured:
            if self._captured_this_call():
                self._merge_runtime_gaps()
            return
        if require_persisted and not self._captured_this_call():
            return
        try:
            launch = getattr(self._host_runtime, "maybe_launch_bounded_worker", None)
            if not callable(launch):
                worker_gaps = (GAP_WORKER_LAUNCH_FAILED,)
            else:
                partition = self._context(audience, session_id, "host_generated")
                worker_gaps = cast(Callable[..., tuple[str, ...]], launch)(
                    session_id=session_id,
                    allowed_scope_ids=audience.allowed_scope_ids,
                    project_id=partition.project_id,
                    branch_id=partition.branch_id,
                )
        except Exception:
            worker_gaps = (GAP_WORKER_LAUNCH_FAILED,)
        if worker_gaps:
            self._diag("runtime_worker", gaps=worker_gaps)

    def _wake_after_capture(self, session_id: str, audience, deadline: float) -> None:
        """After a Stop/SessionEnd capture, attach the runtime and wake the owned worker."""
        if self._captured_this_call() and self._remaining(deadline) >= _RUNTIME_ATTACH_MIN_S:
            self._ensure_host_runtime(audience)
        self._maybe_launch_owned_worker(session_id, audience)

    @property
    def runtime_ready(self) -> bool:
        """Whether this handler has its trusted runtime attached from a config it could read.  One kept for later
        prompts (``local_endpoint.KeptRecaller``) never attaches again, so without it the handler is made anew: a
        config read at a bad moment (a sharing violation) left every later recall without its vector search, where a
        handler of its own read it again (review of rc12)."""
        return self._host_runtime is not None and bool(getattr(self._host_runtime, "configured", False))

    def warm_vectors(self, seconds: float) -> None:
        """Attach the runtime and warm its vector store now, for a handler kept across prompts
        (``local_endpoint.KeptRecaller.warm``).  It writes nothing."""
        self._ensure_host_runtime()
        warm = getattr(getattr(self._host_runtime, "_runtime", None), "warm_vector_store", None)
        if callable(warm):
            warm(seconds)

    def warm_embedding(self, seconds: float) -> None:
        """Ask the runtime's query embedding route for one vector now, for a server's start
        (``local_endpoint.KeptRecaller.warm``; its keep-warm searches do not).  It writes nothing."""
        self._ensure_host_runtime()
        warm = getattr(getattr(self._host_runtime, "_runtime", None), "warm_query_embedding", None)
        if callable(warm):
            warm(seconds)

    def close(self) -> None:
        if self._host_runtime is not None:
            # A short hook must return without synchronously killing the
            # already-owned bounded watchdog.  The watchdog owns cleanup and
            # removes its ephemeral trusted config when the drain exits.
            self._host_runtime.close(detach_worker=True)

    # -- payload dispatch ------------------------------------------------

    def _session_id(self, payload: dict[str, Any]) -> str | None:
        session_id = payload.get("session_id")
        # A shared entry's stored session carries the entry (``identity.stored_session_id``).
        limit = 240 - len(self.config.entry_id) - 1 if isinstance(self.config, SharedClientConfig) else 240
        if type(session_id) is not str or not session_id.strip() or len(session_id.strip()) > limit:
            self._diag("invalid_session")
            return None
        return session_id.strip()

    def _audience(self, payload: dict[str, Any]):
        audience = resolve_runtime_audience(self.config, payload.get("cwd"))
        if not audience.allowed_scope_ids:
            self._diag("no_audience", gaps=audience.capability_gaps)
            return None
        return audience

    def handle_payload(self, payload: dict[str, Any], *, record: RecordLines | None = None,
                       local_record: bool = True, error_reply: bool = False) -> dict[str, Any]:
        payload = without_lone_surrogates(payload)
        self._persisted_this_call = False
        self._queued_this_call = False
        self._closed_reply = None
        self._local_record = record is None and local_record  # a client on another machine sends its record's lines
        self._client_error_reply = error_reply  # what that client judged from its own record
        self.diagnostics = HookDiagnostics(capability_gaps=self.diagnostics.capability_gaps)
        event = payload.get("hook_event_name")
        self.diagnostics.last_event = str(event) if event is not None else None
        if event not in _HOST_EVENTS[self.host]:
            self._diag("unsupported_event")
            return {}
        if self.host == "workbuddy" and is_workbuddy_agent_run(payload):
            # A subagent's prompt is the agent that started it speaking, and its end is not the session's: like a
            # task notification, none of it is the person's.  WorkBuddy 5.3.14 sends these hooks for the main session
            # only; this keeps a later version that sends them from storing a subagent under the person's session.
            self._diag("agent_run")
            return {}
        session_id = self._session_id(payload)
        if session_id is None:
            return {}
        audience = self._audience(payload)
        if audience is None:
            return {}
        if self._host_runtime is not None:
            self._host_runtime.rebind_session(session_id, audience.allowed_scope_ids)
            self._merge_runtime_gaps()
        # The extended trusted budget is for the auto recall path and for a client's
        # read of its session record.  The other hooks keep their short processing cap.
        budget = self._hook_budget() if event == "UserPromptSubmit" or self._reads_record(event) else _TOTAL_BUDGET_S
        deadline = self._hook_deadline(budget)
        if event == "SessionStart":
            if isinstance(self.config, SharedClientConfig):
                # An entry starts no worker, and its binding was checked when the config loaded.  The
                # status a local installation reads here counts the whole store: 7-8 s on the pilot's
                # shared store of 277,000 sources, past Codex's 2 s hook timeout at every session start.
                return {}
            if self._session_start(session_id, audience, deadline) and self._remaining(deadline) >= _RUNTIME_ATTACH_MIN_S:
                self._ensure_host_runtime(audience)
                self._maybe_launch_owned_worker(session_id, audience, require_persisted=False)
            return {}
        if event == "UserPromptSubmit":
            return self._user_prompt_submit(session_id, audience, payload, deadline)
        if self.host == "codex" and _suggestions_thread(self.config, session_id, ended=event == "SessionEnd"):
            # The rest of a thread Codex opened to ask the model for suggestions: its tool calls, its answer and its
            # end are Codex's own activity.  On the pilot one thread left four tool outputs of 2-11 kB and an end
            # marker after its request and answer had been kept out.
            self._diag("host_generated_thread")
            return {}
        if event == "Interrupt":
            return self._interrupt(session_id, audience, payload, deadline)
        if event == "PostToolUse":
            return self._post_tool_use(session_id, audience, payload, deadline)
        if self.host == "dsh" and record is None:
            # The turn's messages as dsh's plugin kept them (it has no record a hook could open), read as a remote
            # client's lines; the answer says how many are stored, so the plugin drops those and sends the rest again.
            record = RecordLines(start=0, lines=transcript.dsh_lines(payload.get("record")))
        capture = self._stop if event == "Stop" else self._session_end
        result = capture(session_id, audience, payload, deadline)
        # A server for a client on another machine reads only the lines that client sent (``local_record``
        # False): the payload's transcript_path names a file over there, and a path from a request is never
        # opened here.
        if self._reads_record(event) and (record is not None or local_record):
            self._read_record(session_id, audience, payload, deadline, remote=record)
        if self.host == "workbuddy" and event == "SessionEnd":
            _forget_turns(self.config, session_id)
        self._wake_after_capture(session_id, audience, deadline)
        if self.host == "dsh" and record is not None:
            result = {**result, "through": record.through or 0}
        return result

    def handle_bytes(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > _MAX_STDIN_BYTES:
            self._diag("input_too_large")
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            # Nested past what the parser takes, a tool's output ended the hook with no answer (review of rc11).
            self._diag("invalid_json")
            return {}
        if type(payload) is not dict:
            self._diag("invalid_root")
            return {}
        return self.handle_payload(payload)

    # -- capture ---------------------------------------------------------

    def _capture(self, context, audience, event, *, deadline: float, gaps: tuple[str, ...] = (),
                 via_inbox: bool = True, wait: float = _CAPTURE_TIMEOUT_S) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Record one host event; returns the committed source refs and the accumulated gaps.

        ``via_inbox=False`` is for a message read from the session record, which keeps it until it is
        written: one write instead of the inbox's two.
        """
        if event is None:
            return (), gaps
        started = time.monotonic()
        self.diagnostics.capture_stage = "ingress"
        self.diagnostics.capture_durability = "not_persisted"
        if self._remaining(deadline) <= 0:
            self.diagnostics.capture_error_code = "DEADLINE_EXCEEDED"
            self.diagnostics.capture_elapsed_ms = 0
            self._diag("deadline_exceeded", gaps=gaps)
            return (), gaps
        try:
            if via_inbox:
                receipt = self.core.record_host_event(
                    context,
                    event,
                    scope_id=audience.capture_scope_id,
                    host_scope=audience.host_scope,
                    remaining_seconds=min(wait, self._remaining(deadline)),
                )
            else:
                receipt = self.core.record_event(context, event, scope_id=audience.capture_scope_id,
                                                 remaining_seconds=min(wait, self._remaining(deadline)))
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.diagnostics.capture_durability = "unknown"
            self.diagnostics.capture_error_type = type(exc).__name__
            if isinstance(exc, ContractError):
                self._note_capture_error(exc.code)
                if (exc.code, exc.field) == DELETED_KEY:
                    # A copy of a deleted message under its key, written straight from the session record: refused for
                    # good, as the inbox cancels one.  Taken as unsettled, every later Stop stopped at that line
                    # (review of rc13).
                    self.diagnostics.capture_disposition = "cancelled"
            gaps = (*gaps, "capture_gap:write_exception")
            self._diag("capture_exception", gaps=gaps)
            return (), gaps
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            self.diagnostics.capture_elapsed_ms = elapsed
            self.diagnostics.capture_total_ms = (self.diagnostics.capture_total_ms or 0) + elapsed
        self.diagnostics.capture_disposition = receipt.disposition
        self.diagnostics.capture_durability = receipt.durability
        if receipt.error_code:
            self._note_capture_error(receipt.error_code)
        if receipt.durability == "queued":
            self._queued_this_call = True
            self.diagnostics.capture_stage = "durable_inbox"
            gaps = (*gaps, "capture_gap:durable_ingress_pending")
            self._diag("capture_queued", gaps=gaps)
            return (), gaps
        if receipt.durability != "persisted":
            gaps = (*gaps, f"capture_gap:{receipt.disposition}")
            self._diag("capture_unavailable", gaps=gaps)
            return (), gaps
        self._persisted_this_call = True
        self.diagnostics.capture_stage = "source_committed"
        refs = tuple(f"{write.ref}@{write.revision}" for write in receipt.event_refs)
        return refs, (*gaps, *receipt.gaps)

    def _reads_record(self, event: object) -> bool:
        return (event in ("Stop", "SessionEnd") and self.host in _READS_RECORD
                and isinstance(self.config, SharedClientConfig))

    def _read_record(self, session_id: str, audience, payload: dict[str, Any], deadline: float, *,
                     remote: RecordLines | None = None) -> None:
        """Record what the session record shows was said since the last read (see ``transcript``).

        What a hook already stored is recognised by its words and moment and skipped.  A capture that
        cannot be written now ends the read there; the next Stop starts again from that message.  A client
        on another machine reads its record there and sends the lines (``remote``); the offset reached goes
        back in ``remote.through`` for that client's own cursor.
        """
        cursor = None
        workbuddy = self.host == "workbuddy"
        if remote is not None:
            start, lines = remote.start, remote.lines
        else:
            record = (transcript.workbuddy_record_path(payload.get("transcript_path"), session_id,
                                                       record_id=payload.get("agent_id"))
                      if workbuddy else transcript.record_path(payload.get("transcript_path"), session_id))
            if record is None:
                self._diag("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
            cursor = transcript.Cursor(self.config.home, session_id, record)
            start = cursor.load()
            try:
                lines = transcript.read(record, start, rows=transcript.workbuddy_said if workbuddy else transcript.said)
            except OSError:
                self._diag("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
        said = [entry for _end, entry in lines if entry is not None]
        # WorkBuddy's record names no turn: a person's message there is the turn its prompt hook kept for the same words,
        # and the model's message after it with the words of the Stop's reply is that Stop's turn: held when the Stop
        # stored it, stored from here when the Stop took it for the previous reply repeated (``_close_turn``).
        turns = _record_turns(self.config, session_id, said) if workbuddy else {}
        replied = _replied_entry(said, self._closed_reply) if workbuddy else None
        held: tuple[bool, ...] = ()
        if said:
            try:
                held = self.core.said_in_session(
                    self._context(audience, session_id, "host_generated"), audience.capture_scope_id,
                    [(entry.role, entry.text, entry.occurred_at, self._record_key(session_id, entry, turns, replied))
                     for entry in said],
                    window_seconds=_RECORD_SAME_MESSAGE_S,
                    remaining_seconds=max(0.0, self._remaining(deadline)))
            except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
                # Named, so that a store that fails otherwise than busy says what failed (review of rc13).
                self.diagnostics.capture_error_type = type(exc).__name__
                self._diag("session_record_check_failed")
                return
        known = {entry.entry_id for entry, stored in zip(said, held) if stored}
        until = min(deadline, self.clock.monotonic() + _RECORD_READ_S)
        position = start
        for end, entry in lines:
            if entry is not None and entry.entry_id not in known:
                if self._remaining(until) < _RECORD_CAPTURE_MIN_S:
                    break
                event = recorded_source_event(
                    installation_id=self.config.installation_id,
                    host=self.host,
                    session_id=session_id,
                    entry_id=entry.entry_id,
                    role=entry.role,
                    text=entry.text,
                    occurred_at=entry.occurred_at,
                    recorded_at=self.clock.utc_now(),
                )
                origin = "human_direct" if entry.role == "user" else "assistant_visible"
                if not self._captured_for_good(self._context(audience, session_id, origin), audience, event, until):
                    break
            position = end
        if remote is not None:
            remote.through = position
        elif position != start:
            cursor.save(position)

    def _record_key(self, session_id: str, entry: "transcript.Said", turns: dict[str, str],
                    replied: tuple[str, str] | None) -> str | None:
        """The key a hook stored a record message under, when the record or a kept turn names it."""
        if entry.role == "user" and (turns.get(entry.entry_id) or entry.prompt_id):
            kind, event_id = "user", turns.get(entry.entry_id) or entry.prompt_id
        elif replied is not None and entry.entry_id == replied[0]:
            kind, event_id = "assistant", replied[1]
        else:
            return None
        return host_source_key(host=self.host, installation_id=self.config.installation_id, session_id=session_id,
                               event_kind=kind, event_id=event_id)

    def _captured_for_good(self, context, audience, event, deadline: float) -> bool:
        """Capture one record message; False when it may succeed later and the read must stop here."""
        diagnostics = self.diagnostics
        diagnostics.capture_disposition = diagnostics.capture_error_code = diagnostics.capture_error_type = None
        self._capture(context, audience, event, deadline=deadline, via_inbox=False)
        return diagnostics.capture_settled

    def _session_start(self, session_id: str, audience, deadline: float) -> bool:
        context = self._context(audience, session_id, "host_generated")
        try:
            self.core.status(context)
        except ContractError:
            self._diag("binding_unavailable")
            return False
        return True

    def _session_end(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        reason = payload.get("reason")
        label = reason if type(reason) is str and reason.strip() else "unknown"
        event = lifecycle_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            event_kind="session_end",
            event_id=session_id,
            content=f"session_end:{label}",
            recorded_at=self.clock.utc_now(),
        )
        self._capture(self._context(audience, session_id, "host_generated"), audience, event, deadline=deadline)
        return {}

    def _interrupt(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
        if turn_id is None:
            gaps = (*gaps, "outcome_gap:interrupt_without_turn")
        event = lifecycle_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            event_kind="interrupt",
            event_id=turn_id or session_id,
            content="interrupt:turn_stopped",
            recorded_at=self.clock.utc_now(),
            gaps=gaps,
        )
        self._capture(self._context(audience, session_id, "host_generated"), audience, event, deadline=deadline, gaps=gaps)
        return {}

    def _user_prompt_submit(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self.host == "workbuddy":
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self._diag("missing_prompt", gaps=("capability_gap:missing_prompt",))
                return {}
            # Only the person's words are stored and recalled for, never what WorkBuddy wraps around them.
            prompt = workbuddy_person_text(prompt)
            notice = is_workbuddy_notice(prompt)
            # A turn is kept for a notice too, so that the reply to it is stored under a turn of its own.
            turn_id, gaps = _open_turn(self.config, session_id, payload, None if notice else prompt,
                                       self.clock.utc_now()), ()
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
            if turn_id is None:
                self._diag("missing_turn_id", gaps=gaps)
                return {}
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self._diag("missing_prompt", gaps=(*gaps, "capability_gap:missing_prompt"))
                return {}
            notice = self.host == "claude-code" and is_task_notification(prompt)
        if notice:
            # Claude Code's own notice that a background task finished: recorded as the owner's words it
            # became a message they never wrote, and a recall on it answers nothing they asked.  WorkBuddy
            # hands its model the same notice (its ``BackgroundTaskNotifier``), and a Stop hook's or a goal's
            # request to go on.
            self._diag("task_notification")
            return {}
        if self.host == "codex" and is_codex_suggestions_prompt(prompt):
            # Codex asking the model what the owner might do next, through the hook a message comes by: not their
            # words, and nothing for a recall to answer.  The thread is marked, so that its later hooks, each a
            # process of its own, keep the rest of it out too.
            _mark_suggestions_thread(self.config, session_id)
            self._diag("host_generated_prompt")
            return {}
        if self.host == "codex":
            # The owner speaking in a marked thread makes the rest of it theirs.
            _suggestions_thread(self.config, session_id, ended=True)
        attachment_refs, attachment_gaps = authorized_attachment_refs(payload)
        gaps = (*gaps, *attachment_gaps)
        if attachment_gaps:
            self._diag("attachment_gap", gaps=attachment_gaps)
        event = user_prompt_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            turn_id=turn_id,
            prompt=prompt,
            recorded_at=self.clock.utc_now(),
            gaps=gaps,
        )
        if event is not None and attachment_refs:
            event["artifact_refs"] = attachment_refs
        context = self._context(audience, session_id, "human_direct")
        wait = min(_PROMPT_CAPTURE_TIMEOUT_S, max(_CAPTURE_TIMEOUT_S, self._remaining(deadline) / 2))
        current_refs, capture_gaps = self._capture(context, audience, event, deadline=deadline, gaps=gaps,
                                                   wait=wait)
        # The vector search comes with the runtime.  A prompt the store was too busy to take is recalled by
        # meaning as well: six on the work computer's two entries in one night were recalled by words alone.  One
        # the capture refused, or that holds a credential however the capture ended, goes without it, so that
        # nothing of it reaches an embedding provider.
        vectors = ((self._captured_this_call()
                    or (event is not None and not self._refused_this_call() and not contains_secret_like_text(prompt)))
                   and self._remaining(deadline) >= _RUNTIME_ATTACH_MIN_S)
        if vectors:
            self._ensure_host_runtime(audience)
            if self._queued_this_call:
                self._maybe_launch_owned_worker(session_id, audience)
        if not prompt.strip():
            return {}
        if event is not None and not current_refs and not self._queued_this_call:
            self._diag("capture_failed", gaps=capture_gaps)
        # The turn is recalled whether or not its message was stored; skipping here left it without memory
        # whenever the store was busy, which is when a writer holds the lease.  A message that failed or still
        # waits in the inbox is not among the sources a recall reads, so there is nothing of this turn to fence
        # out.  One refused (a credential) attached no runtime above, so its recall has no vector channel and
        # nothing of it goes to an embedding provider.
        request_id = f"{self.host}-auto:{session_id}:{turn_id}"
        if vectors and self.resident_recall is not None:
            return self._resident_answer(payload, context, prompt, request_id, current_refs, deadline, capture_gaps)
        return self._auto_recall(context, prompt, request_id, current_refs, deadline, capture_gaps)

    def _resident_answer(self, payload: dict[str, Any], context, prompt: str, request_id: str,
                         current_refs: tuple[str, ...], deadline: float, gaps: tuple[str, ...]) -> dict[str, Any]:
        """This prompt's recall from the entry's MCP server, with its vector search warm, or the hook's own.

        The server is asked from a thread and given all of the hook's time but its answer's way back.  One that has
        not answered when ``_LOCAL_RECALL_RESERVE_S`` are left is recalled alongside, with the helper this hook
        started at its own start: given only what the hook did not keep back, a recall that needed most of the time
        had none (review of rc11).  The hook then uses the answer that ran its vector search, the server's when both
        or neither did; one whose own went without it waits for the server until its own time is up.  An answer that
        failed, ran out of time or came back empty because its read did not finish (``_RESIDENT_REASONS``), or none,
        leaves the hook's own.  One without its vector search is used as it is unless what failed was the server's
        own (``_server_own_vector_fault``) and this hook has a vector search: the hook then recalls as well (a server
        that lost its key recalled every prompt by words alone).  The server writes nothing: the prompt was stored
        here, so an answer that comes after the hook is done costs the turn nothing but its warm vectors."""
        remaining = self._remaining(deadline)
        if remaining < _RESIDENT_MIN_S:
            return self._auto_recall(context, prompt, request_id, current_refs, deadline, gaps)
        answers: list[Any] = []
        answered = threading.Event()

        def ask() -> None:
            try:
                answers.append(self.resident_recall(payload, current_refs, gaps, remaining))
            except Exception:  # noqa: BLE001 - the hook's own recall is always there to fall back on
                answers.append(None)
            finally:
                answered.set()

        threading.Thread(target=ask, name="scope-recall-resident", daemon=True).start()
        answered.wait(max(0.0, self._remaining(deadline) - _LOCAL_RECALL_RESERVE_S))
        read = answered.is_set()
        server = self._resident_taken(answers[0]) if read else None
        if server is not None and (server[1].get("recall_vectors") is True or not self._vector_route()
                                   or not _server_own_vector_fault(server[1].get("recall_vector_gap"))):
            return self._resident_used(server)
        reason = self.diagnostics.last_reason
        own = self._auto_recall(context, prompt, request_id, current_refs, deadline, gaps)
        own_vectors = self.diagnostics.recall_vectors is True
        if not read and not own_vectors:
            # The hook's own went without its vector search: the server's, warm, is worth what time is left.
            answered.wait(self._remaining(deadline))
        if not read and answered.is_set():
            server, read = self._resident_taken(answers[0]), True
        if server is not None and (server[1].get("recall_vectors") is True or not own_vectors):
            self.diagnostics.last_reason = reason  # what the hook's own recall said is not what answered
            return self._resident_used(server)
        if server is not None:
            gap = _error_detail(server[1].get("recall_vector_gap"))
            self.resident_outcome = "without_vectors" + (f":{gap}" if gap else "")
        elif not read:
            self.resident_outcome = "slow" if own_vectors else "late"
        return own

    def _resident_taken(self, answered: tuple[dict[str, Any], dict[str, Any]] | None
                        ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The server's answer and its diagnostics, or None when there is none to take (it ran out of time or
        failed, which the hook's stderr then says)."""
        if answered is None:
            return None
        result, fields = answered
        reason = fields.get("last_reason")
        if reason in _RESIDENT_REASONS:
            # Said on the hook's stderr: a server whose recalls kept failing looked healthy there (review of rc11).
            detail = _error_detail(fields.get("recall_error_detail"))
            self.resident_outcome = f"failed:{reason}" + (f":{detail}" if detail else "")
            return None
        return answered

    def _resident_used(self, answered: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        """The server's answer as this hook's, its diagnostics taken with it."""
        result, fields = answered
        for name in ("recall_vector_gap", "recall_error_detail"):
            value = fields.get(name)
            if value is None or type(value) is str:
                setattr(self.diagnostics, name, _error_detail(value) if value else None)
        return result if isinstance(result, dict) else {}

    def _vector_route(self) -> bool:
        """Whether this hook's own runtime has a vector search to recall with."""
        runtime = self._host_runtime.runtime if self._host_runtime is not None else None
        return runtime is not None and getattr(runtime.config, "vector", None) is not None

    def resident_recall_for(self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...],
                            remaining: float) -> dict[str, Any]:
        """A prompt's automatic recall and nothing else, for the hook that stored the prompt itself
        (``local_endpoint``): the identity, audience and recall ``_user_prompt_submit`` gives it, in ``remaining``
        seconds.  It writes nothing."""
        self.diagnostics = HookDiagnostics(capability_gaps=self.diagnostics.capability_gaps)
        self.diagnostics.last_event = "UserPromptSubmit"
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return {}
        session_id = self._session_id(payload)
        audience = self._audience(payload) if session_id is not None else None
        if audience is None:
            return {}
        turn_id, _gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
        prompt = payload.get("prompt")
        if self.host == "workbuddy":
            if type(prompt) is not str or is_workbuddy_agent_run(payload):
                return {}
            # The turn the hook kept for these words moments ago (``_open_turn``), or one of this recall's own.
            prompt = workbuddy_person_text(prompt)
            turn_id = _kept_turn(self.config, session_id, prompt) or _derived_turn(session_id, prompt,
                                                                                  self.clock.utc_now())
        # What the hook would have recalled nothing for, or recalled without the vector channel, is not asked here;
        # the server checks again rather than take the hook's word for it.
        if (turn_id is None or type(prompt) is not str or not prompt.strip() or contains_secret_like_text(prompt)
                or (self.host == "claude-code" and is_task_notification(prompt))
                or (self.host == "workbuddy" and is_workbuddy_notice(prompt))
                or (self.host == "codex" and is_codex_suggestions_prompt(prompt))):
            return {}
        deadline = self.clock.monotonic() + max(0.0, remaining)
        self._ensure_host_runtime(audience)
        context = self._context(audience, session_id, "human_direct")
        return self._auto_recall(context, prompt, f"{self.host}-auto:{session_id}:{turn_id}", current_refs, deadline,
                                 gaps)

    def _auto_recall(self, context, prompt: str, request_id: str, current_refs: tuple[str, ...], deadline: float, gaps: tuple[str, ...]) -> dict[str, Any]:
        """Render this turn's automatic recall context, or nothing once the budget is gone."""
        remaining = self._remaining(deadline)
        self.diagnostics.recall_vectors = None
        if remaining <= 0:
            self._diag("deadline_exceeded")
            return {}
        request: RecallRequest = {
            "protocol_version": "1.1",
            "request_id": request_id[:100],
            "query": prompt.strip()[:_RECALL_QUERY_CHARS],
            "mode": "auto",
            "max_items": 6,
            "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS,
        }
        try:
            packet = self.core.recall_packet(context, request, current_source_refs=current_refs, deadline_seconds=remaining)
            preparation = self.core.prepare_recall_render(context, packet)
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            code = getattr(exc, "code", None)
            self.diagnostics.recall_error_detail = _error_detail(
                f"{type(exc).__name__}:{code}" if isinstance(code, str) else type(exc).__name__)
            self._diag("recall_exception", gaps=gaps)
            return {}
        without = recall_without_vectors(packet.get("gaps") or ())
        self.diagnostics.recall_vectors = without is None
        if without is not None:
            self.diagnostics.recall_vector_gap = _error_detail(without)
        incomplete = recall_incomplete(packet)
        if incomplete is not None:
            # Its vector search may have run, but nothing of it reached the answer: ranked as without it, a hook's own
            # empty answer beat the entry's server's finished one (review of rc11).
            self.diagnostics.recall_vectors = False
            self.diagnostics.recall_error_detail = _error_detail(incomplete)
            self._diag("recall_incomplete", gaps=gaps)
        if self._remaining(deadline) <= 0:
            # Nothing of its vector search reached an answer: ranked as with it, this empty answer beat a server's
            # (review of rc11).
            self.diagnostics.recall_vectors = False
            self._diag("deadline_exceeded", gaps=gaps)
            return {}
        # In a shared store the model is told which agent it is, so another entry's items read as theirs.
        entry = ((self.config.entry_id, self.config.entry_name) if isinstance(self.config, SharedClientConfig)
                 else None)
        text = render_host_recall_context(preparation.canonical_text, context=preparation.context, entry=entry)
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}

    def _stop(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self.host == "dsh" and type(payload.get("last_assistant_message")) is not str:
            # A turn that ended without a reply (aborted, failed), or dsh's plugin sending what it kept of earlier
            # turns: the record lines carry whatever was said.
            self._diag("no_reply")
            return {}
        if self.host == "workbuddy":
            reply = payload.get("last_assistant_message")
            if type(reply) is str and self._workbuddy_error_reply(session_id, payload, reply):
                # WorkBuddy hands the Stop the error it showed in place of a reply (not signed in, a model or network
                # failure) as the reply; the model said nothing, and the record marks that message with the error.
                _note_error_reply(self.config, session_id, reply)
                self._diag("client_error_reply")
                return {}
            turn_id, repeated = _close_turn(self.config, session_id, payload, reply if type(reply) is str else "",
                                            self.clock.utc_now())
            self._closed_reply = (turn_id, _words(reply)) if type(reply) is str and reply.strip() else None
            gaps = ()
            if repeated:
                # A turn that failed or was stopped before it said anything hands the Stop the reply before it.
                self._diag("repeated_reply")
                return {}
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
            if turn_id is None:
                self._diag("missing_turn_id", gaps=gaps)
                return {}
        message = payload.get("last_assistant_message")
        if type(message) is not str:
            gaps = (*gaps, "outcome_gap:missing_assistant_body")
            message = ""
        if self.host == "codex" and is_codex_suggestions_reply(message):
            # The model's answer to Codex's request for suggestions (``is_codex_suggestions_prompt``).
            self._diag("host_generated_reply")
            return {}
        event, outcome_gaps = assistant_stop_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            turn_id=turn_id,
            message=message,
            recorded_at=self.clock.utc_now(),
        )
        context = self._context(audience, session_id, "assistant_visible")
        self._capture(context, audience, event, deadline=deadline, gaps=(*gaps, *outcome_gaps))
        return {}

    def _workbuddy_error_reply(self, session_id: str, payload: dict[str, Any], reply: str) -> bool:
        """Whether a WorkBuddy Stop's reply is an error its record marks (``transcript.workbuddy_error_reply``).  A client
        on another machine judges it from its own record and says so (this side never opens a record for a request); a
        record that cannot be found or read says no, and the reply is stored as before."""
        if not self._local_record:
            return self._client_error_reply
        try:
            record = transcript.workbuddy_record_path(payload.get("transcript_path"), session_id,
                                                      record_id=payload.get("agent_id"))
        except OSError:
            return False
        return record is not None and transcript.workbuddy_error_reply(record, reply)

    def _post_tool_use(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
        if turn_id is None:
            self._diag("missing_turn_id", gaps=gaps)
            return {}
        tool_use_id = payload.get("tool_use_id")
        if type(tool_use_id) is not str or not tool_use_id.strip() or len(tool_use_id) > 240:
            self._diag("missing_tool_use_id", gaps=(*gaps, "capability_gap:missing_tool_use_id"))
            return {}
        tool_name = payload.get("tool_name")
        if type(tool_name) is not str or not tool_name.strip():
            self._diag("missing_tool_name", gaps=(*gaps, "capability_gap:missing_tool_name"))
            return {}
        event, tool_gaps, origin = tool_use_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id.strip(),
            tool_name=tool_name.strip(),
            tool_input=payload.get("tool_input"),
            tool_response=payload.get("tool_response"),
            recorded_at=self.clock.utc_now(),
        )
        if tool_gaps:
            self._diag("tool_payload_gap", gaps=tool_gaps)
        context = self._context(audience, session_id, cast(Origin, origin))
        self._capture(context, audience, event, deadline=deadline, gaps=(*gaps, *tool_gaps))
        return {}


#: How long a thread that asked for suggestions stays marked.  Its tool calls, answer and end follow within minutes;
#: an older mark is removed the next time a thread is marked.
_SUGGESTIONS_THREAD_SECONDS = 24 * 3600


def _session_marks(config, kind: str, session_id: str) -> Path:
    """One session's mark of ``kind``: beside the pointer for an entry of a shared store, in its data for a store of
    its own."""
    folder = (Path(config.home) / "scope-recall" if isinstance(config, SharedClientConfig)
              else Path(config.data_directory)) / kind
    return folder / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _suggestions_mark(config, session_id: str) -> Path:
    """One thread's mark (``_session_marks``)."""
    return _session_marks(config, "host-threads", session_id)


def _mark_suggestions_thread(config, session_id: str) -> None:
    """Remember a thread Codex opened to ask for suggestions; a mark that cannot be written lets only its rest in."""
    mark = _suggestions_mark(config, session_id)
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
        cutoff = time.time() - _SUGGESTIONS_THREAD_SECONDS
        for count, old in enumerate(mark.parent.iterdir()):
            if count >= 256:
                break
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _suggestions_thread(config, session_id: str, *, ended: bool = False) -> bool:
    """Whether a hook belongs to a thread ``_mark_suggestions_thread`` marked; the thread's end removes the mark."""
    mark = _suggestions_mark(config, session_id)
    try:
        marked = time.time() - mark.stat().st_mtime < _SUGGESTIONS_THREAD_SECONDS
    except OSError:
        return False
    if ended or not marked:
        try:
            mark.unlink(missing_ok=True)
        except OSError:
            pass
    return marked


# -- WorkBuddy's turns ---------------------------------------------------------
# WorkBuddy's hooks name no turn they share.  Its ``generation_id`` is the id of the session's latest model request,
# made anew for each request: a prompt carries the previous turn's last one (none on a session's first prompt) and its
# Stop carries this turn's.  So a prompt opens a turn, under that id when no turn of the session has it yet and else
# under one made from the session, the person's words and the moment; the turn is kept in a small file per session for
# the Stop that closes it and the read of the session record after, and the session's end removes it.  The file is
# disposable: without it a Stop makes a turn of its own, and the record is matched by words and moment as before.

#: How long a session's turns are kept; older ones are removed when a turn is kept.
_TURNS_SECONDS = 24 * 3600
#: Turns kept per session: a Stop's read of the record covers its own turn and any before it that fired no Stop.
_TURNS_KEPT = 16


def _words(text: str) -> str:
    """What two copies of one message share whatever their line breaks: WorkBuddy's prompt hook takes the newlines
    out of the person's words, and its record keeps them."""
    return hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()


def _derived_turn(session_id: str, text: str, moment: str) -> str:
    return "turn-" + hashlib.sha256("\x00".join((session_id, text, moment)).encode("utf-8")).hexdigest()[:32]


def _turns(config, session_id: str) -> dict[str, Any]:
    """A session's kept turns, oldest first, each ``[turn id, words or None]``, and the words of its last reply."""
    path = _session_marks(config, "turns", session_id)
    try:
        if time.time() - path.stat().st_mtime >= _TURNS_SECONDS:
            return {"turns": [], "reply": None, "error": None}
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"turns": [], "reply": None, "error": None}
    if not isinstance(kept, dict):
        return {"turns": [], "reply": None, "error": None}
    turns = [list(item) for item in kept.get("turns") or () if isinstance(item, list) and len(item) == 2
             and type(item[0]) is str and (item[1] is None or type(item[1]) is str)]
    reply, error = kept.get("reply"), kept.get("error")
    return {"turns": turns[-_TURNS_KEPT:], "reply": reply if type(reply) is str else None,
            "error": error if type(error) is str else None}


def _keep_turns(config, session_id: str, kept: dict[str, Any]) -> None:
    """Write a session's turns; one that cannot be written leaves the next Stop to make its own."""
    path = _session_marks(config, "turns", session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        pending.write_text(json.dumps(kept), encoding="utf-8")
        os.replace(pending, path)
        cutoff = time.time() - _TURNS_SECONDS
        for count, old in enumerate(path.parent.iterdir()):
            if count >= 256:
                break
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _forget_turns(config, session_id: str) -> None:
    try:
        _session_marks(config, "turns", session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _open_turn(config, session_id: str, payload: dict[str, Any], words: str | None, moment: str) -> str:
    """The turn a WorkBuddy prompt opens, kept for its Stop; ``words`` are the person's, None for a notice."""
    kept = _turns(config, session_id)
    given, _gaps = turn_id_from_payload(payload, required=False, field=_TURN_FIELD["workbuddy"])
    turn = (given if given is not None and all(given != known for known, _words_of in kept["turns"])
            else _derived_turn(session_id, words or "", moment))
    kept["turns"] = [*kept["turns"], [turn, _words(words) if words and words.strip() else None]][-_TURNS_KEPT:]
    _keep_turns(config, session_id, kept)
    return turn


def _close_turn(config, session_id: str, payload: dict[str, Any], reply: str, moment: str) -> tuple[str, bool]:
    """The turn a WorkBuddy Stop closes: the last one a prompt opened, else its own (its ``generation_id``, or one
    made from the reply); and whether the reply is the one the session's last Stop had."""
    kept = _turns(config, session_id)
    if kept["turns"]:
        turn = kept["turns"][-1][0]
    else:
        given, _gaps = turn_id_from_payload(payload, required=False, field=_TURN_FIELD["workbuddy"])
        turn = given or _derived_turn(session_id, reply, moment)
    words = _words(reply) if reply.strip() else None
    # The last reply, or the error WorkBuddy showed in place of one since: a stopped turn hands either.
    repeated = words is not None and words in (kept["reply"], kept["error"])
    if words is not None and not repeated:
        kept["reply"], kept["error"] = words, None
        _keep_turns(config, session_id, kept)
    return turn, repeated


def _note_error_reply(config, session_id: str, reply: str) -> None:
    """Keep the words of an error WorkBuddy showed in place of a reply, which a later stopped turn may hand its Stop."""
    kept = _turns(config, session_id)
    kept["error"] = _words(reply)
    _keep_turns(config, session_id, kept)


def _kept_turn(config, session_id: str, text: str) -> str | None:
    """The latest kept turn of the session opened for these words."""
    words = _words(text)
    return next((turn for turn, kept in reversed(_turns(config, session_id)["turns"]) if kept == words), None)


def _replied_entry(said: list["transcript.Said"], closed: tuple[str, str] | None) -> tuple[str, str] | None:
    """The record id of the model's message with the words of the reply a Stop closed its turn with, among those after
    the person's last message of the read, and that turn."""
    if closed is not None:
        turn, words = closed
        for entry in reversed(said):
            if entry.role == "user":
                break
            if _words(entry.text) == words:
                return entry.entry_id, turn
    return None


def _record_turns(config, session_id: str, said: list["transcript.Said"]) -> dict[str, str]:
    """The kept turn of each of the person's record messages that has one, by record id: the latest messages take the
    latest turns of the same words, each turn one message."""
    open_turns = list(reversed(_turns(config, session_id)["turns"]))
    found: dict[str, str] = {}
    for entry in reversed(said):
        if entry.role != "user":
            continue
        words = _words(entry.text)
        match = next((index for index, (_turn, kept) in enumerate(open_turns) if kept == words), None)
        if match is not None:
            found[entry.entry_id] = open_turns.pop(match)[0]
    return found


#: What a runtime config that does not name ``hook_processing_seconds`` runs: the worker's default.  No
#: installer writes the key, so without this an entry's hooks fell back to 2 s, and most automatic recalls
#: came back empty.
_DEFAULT_CONFIGURED_BUDGET_S = RuntimeInstanceConfig.hook_processing_seconds


def _configured_budget(runtime_config_path: str | None) -> float | None:
    """The entry's ``hook_processing_seconds``, the default when its runtime config does not name one, or
    ``None`` when the config cannot be read or names an invalid one."""
    if not runtime_config_path:
        return None
    try:
        raw = json.loads(Path(runtime_config_path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if "hook_processing_seconds" not in raw:
            return _DEFAULT_CONFIGURED_BUDGET_S
        return _strict_hook_budget(raw["hook_processing_seconds"])
    except (OSError, UnicodeError, ValueError):
        return None


def _error_detail(code: object) -> str | None:
    """Keep an error code verbatim, bounded and free of anything but a code.

    Codes are enum-like by construction, so the guard is cheap insurance
    rather than sanitisation: whatever ends up on the diagnostic line must be
    recognisable as a code and cannot become a channel for payload text.
    """
    text = str(code or "").strip()
    if not text or len(text) > 64:
        return None
    return text if all(char.isalnum() or char in "_.:-" for char in text) else None


#: Gaps by which a recall packet says its vector search did not run or did not finish, in the order one is named:
#: the search failed, was unavailable, or had no time, or the whole search failed before it.  A search that ran and
#: had candidates refused (``vector_rejected:*``, ``vector_old_or_mismatched_space``) is not among them.
_WITHOUT_VECTORS = (
    lambda gap: gap.startswith("vector_error:"),
    lambda gap: gap == "vector_unavailable",
    lambda gap: gap in ("deadline_exceeded", "deadline_exceeded_collect", "deadline_exceeded_vector"),
    lambda gap: gap.startswith("sqlite_unavailable"),
)


def recall_incomplete(packet) -> str | None:
    """What says a recall came back empty because its read did not finish (the store could not be read, or its time
    ran out at any step: ``status: unavailable``), or None.  Such a packet reads like one that found nothing, and a
    server's was taken over the hook's own (reviews of rc11)."""
    if not isinstance(packet, dict) or packet.get("status") != "unavailable":
        return None
    gaps = [gap for gap in packet.get("gaps") or () if isinstance(gap, str)]
    cause = next((gap for gap in gaps if gap.startswith(("deadline_exceeded", "sqlite_unavailable"))), None)
    return cause or (gaps[0] if gaps else "unavailable")


def _server_own_vector_fault(gap: object) -> bool:
    """Whether a server's recall went without its vector search for a reason of its own, which the hook's own recall
    may not share: no vector search at all, its key (``credential_*``), its LanceDB helper or another fault of its own
    process that is not an embedding call's (``core.vector_failure``), or its embedding connection and worker
    (``network_error``, ``http_protocol``, ``transport_*``), which the server keeps between prompts (rc12) while the
    hook's are new.  Otherwise an embedding call's failure (what the provider answered, the time it took, a spent
    budget) the hook meets as well: a second recall only cost the prompt its time and a second metered call (reviews
    of rc11).  The spend ledger's lock held by another writer is not one (an ``OperationalError``), and costs one
    recall more.  Nor is the search running out of time here."""
    if gap == "vector_unavailable":
        return True
    if type(gap) is not str or not gap.startswith("vector_error:"):
        return False
    parts = gap.split(":")
    if len(parts) < 2 or not parts[1]:
        return False
    if parts[1] != "AuxiliaryModelError":
        return True
    return len(parts) >= 3 and (parts[2].startswith(("credential_", "transport_")) or parts[2] in _CONNECTION_FAULTS)


def recall_without_vectors(gaps) -> str | None:
    """The gap that says a recall ran without its vector search, or None when the search ran."""
    listed = [gap for gap in gaps if isinstance(gap, str)]
    for matches in _WITHOUT_VECTORS:
        found = next((gap for gap in listed if matches(gap)), None)
        if found is not None:
            return found
    return None


def emit_result(result: dict[str, Any], *, diagnostics: HookDiagnostics | None = None, empty: str = "{}") -> None:
    # Codex decodes hook stdout as UTF-8, while a Windows child process may
    # inherit a legacy code-page TextIOWrapper.  ASCII JSON is safe on both
    # sides and json.loads restores the original Unicode values.  An empty
    # answer is written as ``empty`` (``boundary.EMPTY_ANSWER``).
    sys.stdout.write(json.dumps(result, ensure_ascii=True) if result else empty)
    if diagnostics is not None and diagnostics.last_reason:
        sys.stderr.write(f"CODEX_HOOK:{diagnostics.last_reason}\n")
    if diagnostics is not None and diagnostics.capture_stage:
        detail = {
            "stage": diagnostics.capture_stage, "disposition": diagnostics.capture_disposition,
            "durability": diagnostics.capture_durability, "error_type": diagnostics.capture_error_type,
            "error_code": diagnostics.capture_error_code, "elapsed_ms": diagnostics.capture_elapsed_ms,
        }
        # Local operator channel only.  stdout carries the host contract; this
        # line is what a person reads when the contract code is not specific
        # enough to act on.
        if diagnostics.capture_error_detail and diagnostics.capture_error_detail != diagnostics.capture_error_code:
            detail["error_detail"] = diagnostics.capture_error_detail
        sys.stderr.write("CODEX_CAPTURE:" + json.dumps(detail, ensure_ascii=True, separators=(",", ":")) + "\n")
    if diagnostics is not None and diagnostics.recall_error_detail:
        sys.stderr.write(f"CODEX_RECALL:{diagnostics.recall_error_detail}\n")
    if diagnostics is not None and diagnostics.recall_vector_gap:
        sys.stderr.write(f"CODEX_RECALL_VECTOR:{diagnostics.recall_vector_gap}\n")
