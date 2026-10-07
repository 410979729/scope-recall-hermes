"""Dispatch Codex (and Claude Code) hook events through the single MemoryCore boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
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
    TURN_FIELD,
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
from .hook_answer import (
    HookDiagnostics,
    error_detail,
    recall_incomplete,
    recall_without_vectors,
    server_own_vector_fault,
)
from .identity import resolve_runtime_audience, trusted_context
from .session_marks import (
    close_turn,
    derived_turn,
    forget_turns,
    in_suggestions_thread,
    kept_turn,
    mark_suggestions_thread,
    note_error_reply,
    open_turn,
    record_turns,
    replied_entry,
    words_of,
)
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
#: What a server's recall may report of how it ended, besides its vector gap and its error.
_RESIDENT_REASONS = frozenset({"deadline_exceeded", "recall_exception", "recall_incomplete"})
_SUPPORTED_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "Interrupt", "SessionEnd"})
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
_HOST_EVENTS = {
    "codex": _SUPPORTED_EVENTS,
    "claude-code": frozenset({"UserPromptSubmit", "Stop", "SessionEnd"}),
    "workbuddy": frozenset({"UserPromptSubmit", "Stop", "SessionEnd"}),
    # dsh's plugin (``distribution/dsh``) sends a prompt hook before a turn's first step and a Stop at its
    # end; dsh has no session end.
    "dsh": frozenset({"UserPromptSubmit", "Stop"}),
}


class HookClock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemHookClock:
    def utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic(self) -> float:
        return time.monotonic()


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
        handler = cls(
            config, core=core, clock=event_clock if event_clock is not None else clock, hook_started_at=hook_started_at
        )
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

    def note(self, reason: str, *, gaps: tuple[str, ...] = ()) -> None:
        self.diagnostics.last_reason = reason
        if gaps:
            self._merge_runtime_gaps(gaps)

    def remaining(self, deadline: float) -> float:
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

    # -- trusted runtime -------------------------------------------------

    def context(self, audience, session_id: str, origin: Origin) -> TrustedContext:
        return trusted_context(self.config, audience, session_id=session_id, actor_origin=origin)

    def ensure_runtime(self, audience=None) -> None:
        """Attach Lance/worker runtime only after Source persist, or for wakeup."""
        if self._host_runtime is not None or self._runtime_attach_attempted:
            return
        self._runtime_attach_attempted = True
        session_id = f"{self.host}-runtime:{self.config.installation_id}"
        started = time.monotonic()
        try:
            partition = self.context(audience, session_id, "host_generated") if audience is not None else None
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
            self.note("runtime_attach_failed", gaps=("capability_gap:trusted_runtime_invalid",))
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
                partition = self.context(audience, session_id, "host_generated")
                worker_gaps = cast(Callable[..., tuple[str, ...]], launch)(
                    session_id=session_id,
                    allowed_scope_ids=audience.allowed_scope_ids,
                    project_id=partition.project_id,
                    branch_id=partition.branch_id,
                )
        except Exception:
            worker_gaps = (GAP_WORKER_LAUNCH_FAILED,)
        if worker_gaps:
            self.note("runtime_worker", gaps=worker_gaps)

    def _wake_after_capture(self, session_id: str, audience, deadline: float) -> None:
        """After a Stop/SessionEnd capture, attach the runtime and wake the owned worker."""
        if self._captured_this_call() and self.remaining(deadline) >= _RUNTIME_ATTACH_MIN_S:
            self.ensure_runtime(audience)
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
        self.ensure_runtime()
        warm = getattr(getattr(self._host_runtime, "_runtime", None), "warm_vector_store", None)
        if callable(warm):
            warm(seconds)

    def warm_embedding(self, seconds: float) -> None:
        """Ask the runtime's query embedding route for one vector now, for a server's start
        (``local_endpoint.KeptRecaller.warm``; its keep-warm searches do not).  It writes nothing."""
        self.ensure_runtime()
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

    def session_of(self, payload: dict[str, Any]) -> str | None:
        session_id = payload.get("session_id")
        # A shared entry's stored session carries the entry (``identity.stored_session_id``).
        limit = 240 - len(self.config.entry_id) - 1 if isinstance(self.config, SharedClientConfig) else 240
        if type(session_id) is not str or not session_id.strip() or len(session_id.strip()) > limit:
            self.note("invalid_session")
            return None
        return session_id.strip()

    def audience_of(self, payload: dict[str, Any]):
        audience = resolve_runtime_audience(self.config, payload.get("cwd"))
        if not audience.allowed_scope_ids:
            self.note("no_audience", gaps=audience.capability_gaps)
            return None
        return audience

    def handle_payload(
        self,
        payload: dict[str, Any],
        *,
        record: RecordLines | None = None,
        local_record: bool = True,
        error_reply: bool = False,
    ) -> dict[str, Any]:
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
            self.note("unsupported_event")
            return {}
        if self.host == "workbuddy" and is_workbuddy_agent_run(payload):
            # A subagent's prompt is the agent that started it speaking, and its end is not the session's: like a
            # task notification, none of it is the person's.  WorkBuddy 5.3.14 sends these hooks for the main session
            # only; this keeps a later version that sends them from storing a subagent under the person's session.
            self.note("agent_run")
            return {}
        session_id = self.session_of(payload)
        if session_id is None:
            return {}
        audience = self.audience_of(payload)
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
            if (
                self._session_start(session_id, audience, deadline)
                and self.remaining(deadline) >= _RUNTIME_ATTACH_MIN_S
            ):
                self.ensure_runtime(audience)
                self._maybe_launch_owned_worker(session_id, audience, require_persisted=False)
            return {}
        if event == "UserPromptSubmit":
            return self._user_prompt_submit(session_id, audience, payload, deadline)
        if self.host == "codex" and in_suggestions_thread(self.config, session_id, ended=event == "SessionEnd"):
            # The rest of a thread Codex opened to ask the model for suggestions: its tool calls, its answer and its
            # end are Codex's own activity.  On the pilot one thread left four tool outputs of 2-11 kB and an end
            # marker after its request and answer had been kept out.
            self.note("host_generated_thread")
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
            forget_turns(self.config, session_id)
        self._wake_after_capture(session_id, audience, deadline)
        if self.host == "dsh" and record is not None:
            result = {**result, "through": record.through or 0}
        return result

    def handle_bytes(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > _MAX_STDIN_BYTES:
            self.note("input_too_large")
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            # Nested past what the parser takes, a tool's output ended the hook with no answer (review of rc11).
            self.note("invalid_json")
            return {}
        if type(payload) is not dict:
            self.note("invalid_root")
            return {}
        return self.handle_payload(payload)

    # -- capture ---------------------------------------------------------

    def _capture(
        self,
        context,
        audience,
        event,
        *,
        deadline: float,
        gaps: tuple[str, ...] = (),
        via_inbox: bool = True,
        wait: float = _CAPTURE_TIMEOUT_S,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Record one host event; returns the committed source refs and the accumulated gaps.

        ``via_inbox=False`` is for a message read from the session record, which keeps it until it is
        written: one write instead of the inbox's two.
        """
        if event is None:
            return (), gaps
        started = time.monotonic()
        self.diagnostics.capture_stage = "ingress"
        self.diagnostics.capture_durability = "not_persisted"
        if self.remaining(deadline) <= 0:
            self.diagnostics.capture_error_code = "DEADLINE_EXCEEDED"
            self.diagnostics.capture_elapsed_ms = 0
            self.note("deadline_exceeded", gaps=gaps)
            return (), gaps
        try:
            if via_inbox:
                receipt = self.core.record_host_event(
                    context,
                    event,
                    scope_id=audience.capture_scope_id,
                    host_scope=audience.host_scope,
                    remaining_seconds=min(wait, self.remaining(deadline)),
                )
            else:
                receipt = self.core.record_event(
                    context,
                    event,
                    scope_id=audience.capture_scope_id,
                    remaining_seconds=min(wait, self.remaining(deadline)),
                )
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.diagnostics.capture_durability = "unknown"
            self.diagnostics.capture_error_type = type(exc).__name__
            if isinstance(exc, ContractError):
                self.diagnostics.note_capture_error(exc.code)
                if (exc.code, exc.field) == DELETED_KEY:
                    # A copy of a deleted message under its key, written straight from the session record: refused for
                    # good, as the inbox cancels one.  Taken as unsettled, every later Stop stopped at that line
                    # (review of rc13).
                    self.diagnostics.capture_disposition = "cancelled"
            gaps = (*gaps, "capture_gap:write_exception")
            self.note("capture_exception", gaps=gaps)
            return (), gaps
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            self.diagnostics.capture_elapsed_ms = elapsed
            self.diagnostics.capture_total_ms = (self.diagnostics.capture_total_ms or 0) + elapsed
        self.diagnostics.capture_disposition = receipt.disposition
        self.diagnostics.capture_durability = receipt.durability
        if receipt.error_code:
            self.diagnostics.note_capture_error(receipt.error_code)
        if receipt.durability == "queued":
            self._queued_this_call = True
            self.diagnostics.capture_stage = "durable_inbox"
            gaps = (*gaps, "capture_gap:durable_ingress_pending")
            self.note("capture_queued", gaps=gaps)
            return (), gaps
        if receipt.durability != "persisted":
            gaps = (*gaps, f"capture_gap:{receipt.disposition}")
            self.note("capture_unavailable", gaps=gaps)
            return (), gaps
        self._persisted_this_call = True
        self.diagnostics.capture_stage = "source_committed"
        refs = tuple(f"{write.ref}@{write.revision}" for write in receipt.event_refs)
        return refs, (*gaps, *receipt.gaps)

    def _reads_record(self, event: object) -> bool:
        return (
            event in ("Stop", "SessionEnd")
            and self.host in _READS_RECORD
            and isinstance(self.config, SharedClientConfig)
        )

    def _read_record(
        self, session_id: str, audience, payload: dict[str, Any], deadline: float, *, remote: RecordLines | None = None
    ) -> None:
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
            record = (
                transcript.workbuddy_record_path(
                    payload.get("transcript_path"), session_id, record_id=payload.get("agent_id")
                )
                if workbuddy
                else transcript.record_path(payload.get("transcript_path"), session_id)
            )
            if record is None:
                self.note("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
            cursor = transcript.Cursor(self.config.home, session_id, record)
            start = cursor.load()
            try:
                lines = transcript.read(record, start, rows=transcript.workbuddy_said if workbuddy else transcript.said)
            except OSError:
                self.note("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
                return
        said = [entry for _end, entry in lines if entry is not None]
        # WorkBuddy's record names no turn: a person's message there is the turn its prompt hook kept for the same words,
        # and the model's message after it with the words of the Stop's reply is that Stop's turn: held when the Stop
        # stored it, stored from here when the Stop took it for the previous reply repeated (``close_turn``).
        turns = record_turns(self.config, session_id, said) if workbuddy else {}
        replied = replied_entry(said, self._closed_reply) if workbuddy else None
        held: tuple[bool, ...] = ()
        if said:
            try:
                held = self.core.said_in_session(
                    self.context(audience, session_id, "host_generated"),
                    audience.capture_scope_id,
                    [
                        (entry.role, entry.text, entry.occurred_at, self._record_key(session_id, entry, turns, replied))
                        for entry in said
                    ],
                    window_seconds=_RECORD_SAME_MESSAGE_S,
                    remaining_seconds=max(0.0, self.remaining(deadline)),
                )
            except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
                # Named, so that a store that fails otherwise than busy says what failed (review of rc13).
                self.diagnostics.capture_error_type = type(exc).__name__
                self.note("session_record_check_failed")
                return
        known = {entry.entry_id for entry, stored in zip(said, held) if stored}
        until = min(deadline, self.clock.monotonic() + _RECORD_READ_S)
        position = start
        for end, entry in lines:
            if entry is not None and entry.entry_id not in known:
                if self.remaining(until) < _RECORD_CAPTURE_MIN_S:
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
                if not self._captured_for_good(self.context(audience, session_id, origin), audience, event, until):
                    break
            position = end
        if remote is not None:
            remote.through = position
        elif position != start:
            cursor.save(position)

    def _record_key(
        self, session_id: str, entry: "transcript.Said", turns: dict[str, str], replied: tuple[str, str] | None
    ) -> str | None:
        """The key a hook stored a record message under, when the record or a kept turn names it."""
        if entry.role == "user" and (turns.get(entry.entry_id) or entry.prompt_id):
            kind, event_id = "user", turns.get(entry.entry_id) or entry.prompt_id
        elif replied is not None and entry.entry_id == replied[0]:
            kind, event_id = "assistant", replied[1]
        else:
            return None
        return host_source_key(
            host=self.host,
            installation_id=self.config.installation_id,
            session_id=session_id,
            event_kind=kind,
            event_id=event_id,
        )

    def _captured_for_good(self, context, audience, event, deadline: float) -> bool:
        """Capture one record message; False when it may succeed later and the read must stop here."""
        diagnostics = self.diagnostics
        diagnostics.capture_disposition = diagnostics.capture_error_code = diagnostics.capture_error_type = None
        self._capture(context, audience, event, deadline=deadline, via_inbox=False)
        return diagnostics.capture_settled

    def _session_start(self, session_id: str, audience, deadline: float) -> bool:
        context = self.context(audience, session_id, "host_generated")
        try:
            self.core.status(context)
        except ContractError:
            self.note("binding_unavailable")
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
        self._capture(self.context(audience, session_id, "host_generated"), audience, event, deadline=deadline)
        return {}

    def _interrupt(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self.host])
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
        self._capture(
            self.context(audience, session_id, "host_generated"), audience, event, deadline=deadline, gaps=gaps
        )
        return {}

    def _user_prompt_submit(
        self, session_id: str, audience, payload: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        if self.host == "workbuddy":
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self.note("missing_prompt", gaps=("capability_gap:missing_prompt",))
                return {}
            # Only the person's words are stored and recalled for, never what WorkBuddy wraps around them.
            prompt = workbuddy_person_text(prompt)
            notice = is_workbuddy_notice(prompt)
            # A turn is kept for a notice too, so that the reply to it is stored under a turn of its own.
            turn_id, gaps = (
                open_turn(self.config, session_id, payload, None if notice else prompt, self.clock.utc_now()),
                (),
            )
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self.host])
            if turn_id is None:
                self.note("missing_turn_id", gaps=gaps)
                return {}
            prompt = payload.get("prompt")
            if type(prompt) is not str:
                self.note("missing_prompt", gaps=(*gaps, "capability_gap:missing_prompt"))
                return {}
            notice = self.host == "claude-code" and is_task_notification(prompt)
        if notice:
            # Claude Code's own notice that a background task finished: recorded as the owner's words it
            # became a message they never wrote, and a recall on it answers nothing they asked.  WorkBuddy
            # hands its model the same notice (its ``BackgroundTaskNotifier``), and a Stop hook's or a goal's
            # request to go on.
            self.note("task_notification")
            return {}
        if self.host == "codex" and is_codex_suggestions_prompt(prompt):
            # Codex asking the model what the owner might do next, through the hook a message comes by: not their
            # words, and nothing for a recall to answer.  The thread is marked, so that its later hooks, each a
            # process of its own, keep the rest of it out too.
            mark_suggestions_thread(self.config, session_id)
            self.note("host_generated_prompt")
            return {}
        if self.host == "codex":
            # The owner speaking in a marked thread makes the rest of it theirs.
            in_suggestions_thread(self.config, session_id, ended=True)
        attachment_refs, attachment_gaps = authorized_attachment_refs(payload)
        gaps = (*gaps, *attachment_gaps)
        if attachment_gaps:
            self.note("attachment_gap", gaps=attachment_gaps)
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
        context = self.context(audience, session_id, "human_direct")
        wait = min(_PROMPT_CAPTURE_TIMEOUT_S, max(_CAPTURE_TIMEOUT_S, self.remaining(deadline) / 2))
        current_refs, capture_gaps = self._capture(context, audience, event, deadline=deadline, gaps=gaps, wait=wait)
        # The vector search comes with the runtime.  A prompt the store was too busy to take is recalled by
        # meaning as well: six on the work computer's two entries in one night were recalled by words alone.  One
        # the capture refused, or that holds a credential however the capture ended, goes without it, so that
        # nothing of it reaches an embedding provider.
        vectors = (
            self._captured_this_call()
            or (event is not None and not self.diagnostics.capture_refused and not contains_secret_like_text(prompt))
        ) and self.remaining(deadline) >= _RUNTIME_ATTACH_MIN_S
        if vectors:
            self.ensure_runtime(audience)
            if self._queued_this_call:
                self._maybe_launch_owned_worker(session_id, audience)
        if not prompt.strip():
            return {}
        if event is not None and not current_refs and not self._queued_this_call:
            self.note("capture_failed", gaps=capture_gaps)
        # The turn is recalled whether or not its message was stored; skipping here left it without memory
        # whenever the store was busy, which is when a writer holds the lease.  A message that failed or still
        # waits in the inbox is not among the sources a recall reads, so there is nothing of this turn to fence
        # out.  One refused (a credential) attached no runtime above, so its recall has no vector channel and
        # nothing of it goes to an embedding provider.
        request_id = f"{self.host}-auto:{session_id}:{turn_id}"
        if vectors and self.resident_recall is not None:
            return self._resident_answer(payload, context, prompt, request_id, current_refs, deadline, capture_gaps)
        return self._auto_recall(context, prompt, request_id, current_refs, deadline, capture_gaps)

    def _resident_answer(
        self,
        payload: dict[str, Any],
        context,
        prompt: str,
        request_id: str,
        current_refs: tuple[str, ...],
        deadline: float,
        gaps: tuple[str, ...],
    ) -> dict[str, Any]:
        """This prompt's recall from the entry's MCP server, with its vector search warm, or the hook's own.

        The server is asked from a thread and given all of the hook's time but its answer's way back.  One that has
        not answered when ``_LOCAL_RECALL_RESERVE_S`` are left is recalled alongside, with the helper this hook
        started at its own start: given only what the hook did not keep back, a recall that needed most of the time
        had none (review of rc11).  The hook then uses the answer that ran its vector search, the server's when both
        or neither did; one whose own went without it waits for the server until its own time is up.  An answer that
        failed, ran out of time or came back empty because its read did not finish (``_RESIDENT_REASONS``), or none,
        leaves the hook's own.  One without its vector search is used as it is unless what failed was the server's
        own (``server_own_vector_fault``) and this hook has a vector search: the hook then recalls as well (a server
        that lost its key recalled every prompt by words alone).  The server writes nothing: the prompt was stored
        here, so an answer that comes after the hook is done costs the turn nothing but its warm vectors."""
        remaining = self.remaining(deadline)
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
        answered.wait(max(0.0, self.remaining(deadline) - _LOCAL_RECALL_RESERVE_S))
        read = answered.is_set()
        server = self._resident_taken(answers[0]) if read else None
        if server is not None and (
            server[1].get("recall_vectors") is True
            or not self.has_vectors
            or not server_own_vector_fault(server[1].get("recall_vector_gap"))
        ):
            return self._resident_used(server)
        reason = self.diagnostics.last_reason
        own = self._auto_recall(context, prompt, request_id, current_refs, deadline, gaps)
        own_vectors = self.diagnostics.recall_vectors is True
        if not read and not own_vectors:
            # The hook's own went without its vector search: the server's, warm, is worth what time is left.
            answered.wait(self.remaining(deadline))
        if not read and answered.is_set():
            server, read = self._resident_taken(answers[0]), True
        if server is not None and (server[1].get("recall_vectors") is True or not own_vectors):
            self.diagnostics.last_reason = reason  # what the hook's own recall said is not what answered
            return self._resident_used(server)
        if server is not None:
            gap = error_detail(server[1].get("recall_vector_gap"))
            self.resident_outcome = "without_vectors" + (f":{gap}" if gap else "")
        elif not read:
            self.resident_outcome = "slow" if own_vectors else "late"
        return own

    def _resident_taken(
        self, answered: tuple[dict[str, Any], dict[str, Any]] | None
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The server's answer and its diagnostics, or None when there is none to take (it ran out of time or
        failed, which the hook's stderr then says)."""
        if answered is None:
            return None
        result, fields = answered
        reason = fields.get("last_reason")
        if reason in _RESIDENT_REASONS:
            # Said on the hook's stderr: a server whose recalls kept failing looked healthy there (review of rc11).
            detail = error_detail(fields.get("recall_error_detail"))
            self.resident_outcome = f"failed:{reason}" + (f":{detail}" if detail else "")
            return None
        return answered

    def _resident_used(self, answered: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        """The server's answer as this hook's, its diagnostics taken with it."""
        result, fields = answered
        for name in ("recall_vector_gap", "recall_error_detail"):
            value = fields.get(name)
            if value is None or type(value) is str:
                setattr(self.diagnostics, name, error_detail(value) if value else None)
        return result if isinstance(result, dict) else {}

    @property
    def has_vectors(self) -> bool:
        """Whether this hook's own runtime has a vector search to recall with."""
        runtime = self._host_runtime.runtime if self._host_runtime is not None else None
        return runtime is not None and getattr(runtime.config, "vector", None) is not None

    def resident_recall_for(
        self, payload: dict[str, Any], current_refs: tuple[str, ...], gaps: tuple[str, ...], remaining: float
    ) -> dict[str, Any]:
        """A prompt's automatic recall and nothing else, for the hook that stored the prompt itself
        (``local_endpoint``): the identity, audience and recall ``_user_prompt_submit`` gives it, in ``remaining``
        seconds.  It writes nothing."""
        self.diagnostics = HookDiagnostics(capability_gaps=self.diagnostics.capability_gaps)
        self.diagnostics.last_event = "UserPromptSubmit"
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return {}
        session_id = self.session_of(payload)
        audience = self.audience_of(payload) if session_id is not None else None
        if audience is None:
            return {}
        turn_id, _gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self.host])
        prompt = payload.get("prompt")
        if self.host == "workbuddy":
            if type(prompt) is not str or is_workbuddy_agent_run(payload):
                return {}
            # The turn the hook kept for these words moments ago (``open_turn``), or one of this recall's own.
            prompt = workbuddy_person_text(prompt)
            turn_id = kept_turn(self.config, session_id, prompt) or derived_turn(
                session_id, prompt, self.clock.utc_now()
            )
        # What the hook would have recalled nothing for, or recalled without the vector channel, is not asked here;
        # the server checks again rather than take the hook's word for it.
        if (
            turn_id is None
            or type(prompt) is not str
            or not prompt.strip()
            or contains_secret_like_text(prompt)
            or (self.host == "claude-code" and is_task_notification(prompt))
            or (self.host == "workbuddy" and is_workbuddy_notice(prompt))
            or (self.host == "codex" and is_codex_suggestions_prompt(prompt))
        ):
            return {}
        deadline = self.clock.monotonic() + max(0.0, remaining)
        self.ensure_runtime(audience)
        context = self.context(audience, session_id, "human_direct")
        return self._auto_recall(
            context, prompt, f"{self.host}-auto:{session_id}:{turn_id}", current_refs, deadline, gaps
        )

    def _auto_recall(
        self,
        context,
        prompt: str,
        request_id: str,
        current_refs: tuple[str, ...],
        deadline: float,
        gaps: tuple[str, ...],
    ) -> dict[str, Any]:
        """Render this turn's automatic recall context, or nothing once the budget is gone."""
        remaining = self.remaining(deadline)
        self.diagnostics.recall_vectors = None
        if remaining <= 0:
            self.note("deadline_exceeded")
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
            packet = self.core.recall_packet(
                context, request, current_source_refs=current_refs, deadline_seconds=remaining
            )
            preparation = self.core.prepare_recall_render(context, packet)
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            code = getattr(exc, "code", None)
            self.diagnostics.recall_error_detail = error_detail(
                f"{type(exc).__name__}:{code}" if isinstance(code, str) else type(exc).__name__
            )
            self.note("recall_exception", gaps=gaps)
            return {}
        without = recall_without_vectors(packet.get("gaps") or ())
        self.diagnostics.recall_vectors = without is None
        if without is not None:
            self.diagnostics.recall_vector_gap = error_detail(without)
        incomplete = recall_incomplete(packet)
        if incomplete is not None:
            # Its vector search may have run, but nothing of it reached the answer: ranked as without it, a hook's own
            # empty answer beat the entry's server's finished one (review of rc11).
            self.diagnostics.recall_vectors = False
            self.diagnostics.recall_error_detail = error_detail(incomplete)
            self.note("recall_incomplete", gaps=gaps)
        if self.remaining(deadline) <= 0:
            # Nothing of its vector search reached an answer: ranked as with it, this empty answer beat a server's
            # (review of rc11).
            self.diagnostics.recall_vectors = False
            self.note("deadline_exceeded", gaps=gaps)
            return {}
        # In a shared store the model is told which agent it is, so another entry's items read as theirs.
        entry = (self.config.entry_id, self.config.entry_name) if isinstance(self.config, SharedClientConfig) else None
        text = render_host_recall_context(preparation.canonical_text, context=preparation.context, entry=entry)
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}

    def _stop(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self.host == "dsh" and type(payload.get("last_assistant_message")) is not str:
            # A turn that ended without a reply (aborted, failed), or dsh's plugin sending what it kept of earlier
            # turns: the record lines carry whatever was said.
            self.note("no_reply")
            return {}
        if self.host == "workbuddy":
            reply = payload.get("last_assistant_message")
            if type(reply) is str and self._workbuddy_error_reply(session_id, payload, reply):
                # WorkBuddy hands the Stop the error it showed in place of a reply (not signed in, a model or network
                # failure) as the reply; the model said nothing, and the record marks that message with the error.
                note_error_reply(self.config, session_id, reply)
                self.note("client_error_reply")
                return {}
            turn_id, repeated = close_turn(
                self.config, session_id, payload, reply if type(reply) is str else "", self.clock.utc_now()
            )
            self._closed_reply = (turn_id, words_of(reply)) if type(reply) is str and reply.strip() else None
            gaps = ()
            if repeated:
                # A turn that failed or was stopped before it said anything hands the Stop the reply before it.
                self.note("repeated_reply")
                return {}
        else:
            turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self.host])
            if turn_id is None:
                self.note("missing_turn_id", gaps=gaps)
                return {}
        message = payload.get("last_assistant_message")
        if type(message) is not str:
            gaps = (*gaps, "outcome_gap:missing_assistant_body")
            message = ""
        if self.host == "codex" and is_codex_suggestions_reply(message):
            # The model's answer to Codex's request for suggestions (``is_codex_suggestions_prompt``).
            self.note("host_generated_reply")
            return {}
        event, outcome_gaps = assistant_stop_source_event(
            installation_id=self.config.installation_id,
            host=self.host,
            session_id=session_id,
            turn_id=turn_id,
            message=message,
            recorded_at=self.clock.utc_now(),
        )
        context = self.context(audience, session_id, "assistant_visible")
        self._capture(context, audience, event, deadline=deadline, gaps=(*gaps, *outcome_gaps))
        return {}

    def _workbuddy_error_reply(self, session_id: str, payload: dict[str, Any], reply: str) -> bool:
        """Whether a WorkBuddy Stop's reply is an error its record marks (``transcript.workbuddy_error_reply``).  A client
        on another machine judges it from its own record and says so (this side never opens a record for a request); a
        record that cannot be found or read says no, and the reply is stored as before."""
        if not self._local_record:
            return self._client_error_reply
        try:
            record = transcript.workbuddy_record_path(
                payload.get("transcript_path"), session_id, record_id=payload.get("agent_id")
            )
        except OSError:
            return False
        return record is not None and transcript.workbuddy_error_reply(record, reply)

    def _post_tool_use(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=TURN_FIELD[self.host])
        if turn_id is None:
            self.note("missing_turn_id", gaps=gaps)
            return {}
        tool_use_id = payload.get("tool_use_id")
        if type(tool_use_id) is not str or not tool_use_id.strip() or len(tool_use_id) > 240:
            self.note("missing_tool_use_id", gaps=(*gaps, "capability_gap:missing_tool_use_id"))
            return {}
        tool_name = payload.get("tool_name")
        if type(tool_name) is not str or not tool_name.strip():
            self.note("missing_tool_name", gaps=(*gaps, "capability_gap:missing_tool_name"))
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
            self.note("tool_payload_gap", gaps=tool_gaps)
        context = self.context(audience, session_id, cast(Origin, origin))
        self._capture(context, audience, event, deadline=deadline, gaps=(*gaps, *tool_gaps))
        return {}


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
