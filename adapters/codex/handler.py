"""Dispatch Codex (and Claude Code) hook events through the single MemoryCore boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable, Protocol, cast

from scope_recall.contracts import ContractError, Origin, RecallRequest, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS
from scope_recall.runtime.instance import RuntimeInstanceConfig
from ..runtime_wiring import _strict_hook_budget, render_host_recall_context

from . import transcript
from .boundary import (
    assistant_stop_source_event,
    authorized_attachment_refs,
    is_task_notification,
    lifecycle_source_event,
    recorded_source_event,
    tool_use_source_event,
    turn_id_from_payload,
    user_prompt_source_event,
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
_TOTAL_BUDGET_S = 2.0
#: Attaching the trusted runtime after a capture needs this much budget left.
_RUNTIME_ATTACH_MIN_S = 0.3
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
_TURN_FIELD = {"codex": "turn_id", "claude-code": "prompt_id"}
#: Clients whose prompt hook may run the entry's ``hook_processing_seconds`` (at most 6 s) from the
#: start.  Codex gives a hook 2 s (its hooks.json timeout), and the runtime that carries the longer
#: budget is attached only after the capture, so a Codex prompt keeps the default.  Claude Code waits
#: 15 s (``maintenance/install_claude_code.py``), and recall on the pilot's shared store took 2.7-5.7 s:
#: with 2 s most automatic recalls would have come back empty.
_CONFIGURED_PROMPT_BUDGET = frozenset({"claude-code"})
#: Clients whose Stop and SessionEnd also read the session record (``transcript``): what the person said,
#: whatever the prompt hook could not write, and what the model said while it worked.  Claude Code waits
#: 10 s for these hooks; a turn's lines take well under a second, and a long backlog is read over several
#: turns, at most ``_RECORD_READ_S`` each, so the end of a turn is not held up.
_READS_RECORD = frozenset({"claude-code"})
_RECORD_READ_S = 3.0
#: A capture is started only with this much of the reading time left.
_RECORD_CAPTURE_MIN_S = 0.5
#: A hook's copy of a message and the record's are the same message when the words match and the moments are this close.
_RECORD_SAME_MESSAGE_S = 120.0
_HOST_EVENTS = {"codex": _SUPPORTED_EVENTS,
                "claude-code": frozenset({"UserPromptSubmit", "Stop", "SessionEnd"})}


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
        trusted_runtime_config_path: str | None = None,
        hook_started_at: float | None = None,
    ) -> "CodexHookHandler":
        """A client attached to a shared store; its runtime config is the entry's, beside its pointer."""
        config = load_shared_client(home, host)
        core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
        handler = cls(config, core=core, clock=clock, hook_started_at=hook_started_at)
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

    # -- trusted runtime -------------------------------------------------

    def _context(self, audience, session_id: str, origin: Origin) -> TrustedContext:
        return trusted_context(self.config, audience, session_id=session_id, actor_origin=origin)

    def _ensure_host_runtime(self, audience=None) -> None:
        """Attach Lance/worker runtime only after Source persist, or for wakeup."""
        if self._host_runtime is not None or self._runtime_attach_attempted:
            return
        self._runtime_attach_attempted = True
        session_id = f"{self.host}-runtime:{self.config.installation_id}"
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

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._persisted_this_call = False
        self._queued_this_call = False
        self.diagnostics = HookDiagnostics(capability_gaps=self.diagnostics.capability_gaps)
        event = payload.get("hook_event_name")
        self.diagnostics.last_event = str(event) if event is not None else None
        if event not in _HOST_EVENTS[self.host]:
            self._diag("unsupported_event")
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
        if event == "Interrupt":
            return self._interrupt(session_id, audience, payload, deadline)
        if event == "PostToolUse":
            return self._post_tool_use(session_id, audience, payload, deadline)
        capture = self._stop if event == "Stop" else self._session_end
        result = capture(session_id, audience, payload, deadline)
        if self._reads_record(event):
            self._read_record(session_id, audience, payload, deadline)
        self._wake_after_capture(session_id, audience, deadline)
        return result

    def handle_bytes(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > _MAX_STDIN_BYTES:
            self._diag("input_too_large")
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            self._diag("invalid_json")
            return {}
        if type(payload) is not dict:
            self._diag("invalid_root")
            return {}
        return self.handle_payload(payload)

    # -- capture ---------------------------------------------------------

    def _capture(self, context, audience, event, *, deadline: float, gaps: tuple[str, ...] = (),
                 via_inbox: bool = True) -> tuple[tuple[str, ...], tuple[str, ...]]:
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
                    remaining_seconds=min(_CAPTURE_TIMEOUT_S, self._remaining(deadline)),
                )
            else:
                receipt = self.core.record_event(context, event, scope_id=audience.capture_scope_id,
                                                 remaining_seconds=min(_CAPTURE_TIMEOUT_S, self._remaining(deadline)))
        except (ContractError, OSError, RuntimeError) as exc:
            self.diagnostics.capture_durability = "unknown"
            self.diagnostics.capture_error_type = type(exc).__name__
            if isinstance(exc, ContractError):
                self._note_capture_error(exc.code)
            gaps = (*gaps, "capture_gap:write_exception")
            self._diag("capture_exception", gaps=gaps)
            return (), gaps
        finally:
            self.diagnostics.capture_elapsed_ms = round((time.monotonic() - started) * 1000)
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

    def _read_record(self, session_id: str, audience, payload: dict[str, Any], deadline: float) -> None:
        """Record what the session record shows was said since the last read (see ``transcript``).

        What a hook already stored is recognised by its words and moment and skipped.  A capture that
        cannot be written now ends the read there; the next Stop starts again from that message.
        """
        record = transcript.record_path(payload.get("transcript_path"), session_id)
        if record is None:
            self._diag("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
            return
        cursor = transcript.Cursor(self.config.home, session_id, record)
        start = cursor.load()
        try:
            lines = transcript.read(record, start)
        except OSError:
            self._diag("session_record_unavailable", gaps=("capture_gap:session_record_unavailable",))
            return
        said = [entry for _end, entry in lines if entry is not None]
        held: tuple[bool, ...] = ()
        if said:
            try:
                held = self.core.said_in_session(
                    self._context(audience, session_id, "host_generated"), audience.capture_scope_id,
                    [(entry.role, entry.text, entry.occurred_at) for entry in said],
                    window_seconds=_RECORD_SAME_MESSAGE_S)
            except (ContractError, OSError, RuntimeError):
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
        if position != start:
            cursor.save(position)

    def _captured_for_good(self, context, audience, event, deadline: float) -> bool:
        """Capture one record message; False when it may succeed later and the read must stop here."""
        diagnostics = self.diagnostics
        diagnostics.capture_disposition = diagnostics.capture_error_code = diagnostics.capture_error_type = None
        self._capture(context, audience, event, deadline=deadline, via_inbox=False)
        # Stored, or refused in a way no retry changes: a secret, an invalid message, its id already taken.
        return (diagnostics.capture_durability == "persisted"
                or diagnostics.capture_disposition in ("rejected", "conflict")
                or diagnostics.capture_error_code in ("SECRET_DETECTED", "INPUT_INVALID", "VERSION_CONFLICT"))

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
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
        if turn_id is None:
            self._diag("missing_turn_id", gaps=gaps)
            return {}
        prompt = payload.get("prompt")
        if type(prompt) is not str:
            self._diag("missing_prompt", gaps=(*gaps, "capability_gap:missing_prompt"))
            return {}
        if self.host == "claude-code" and is_task_notification(prompt):
            # Claude Code's own notice that a background task finished: recorded as the owner's words it
            # became a message they never wrote, and a recall on it answers nothing they asked.
            self._diag("task_notification")
            return {}
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
        current_refs, capture_gaps = self._capture(context, audience, event, deadline=deadline, gaps=gaps)
        if self._captured_this_call() and self._remaining(deadline) >= _RUNTIME_ATTACH_MIN_S:
            self._ensure_host_runtime(audience)
            if self._queued_this_call:
                self._maybe_launch_owned_worker(session_id, audience)
        if not prompt.strip():
            return {}
        if event is not None and not current_refs:
            if not self._queued_this_call:
                self._diag("capture_failed", gaps=capture_gaps)
            return {}
        return self._auto_recall(context, prompt, f"{self.host}-auto:{session_id}:{turn_id}", current_refs, deadline, capture_gaps)

    def _auto_recall(self, context, prompt: str, request_id: str, current_refs: tuple[str, ...], deadline: float, gaps: tuple[str, ...]) -> dict[str, Any]:
        """Render this turn's automatic recall context, or nothing once the budget is gone."""
        remaining = self._remaining(deadline)
        if remaining <= 0:
            self._diag("deadline_exceeded")
            return {}
        request: RecallRequest = {
            "protocol_version": "1.1",
            "request_id": request_id[:100],
            "query": prompt,
            "mode": "auto",
            "max_items": 6,
            "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS,
        }
        try:
            packet = self.core.recall_packet(context, request, current_source_refs=current_refs, deadline_seconds=remaining)
            preparation = self.core.prepare_recall_render(context, packet)
        except (ContractError, OSError, RuntimeError):
            self._diag("recall_exception", gaps=gaps)
            return {}
        if self._remaining(deadline) <= 0:
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
        turn_id, gaps = turn_id_from_payload(payload, required=True, field=_TURN_FIELD[self.host])
        if turn_id is None:
            self._diag("missing_turn_id", gaps=gaps)
            return {}
        message = payload.get("last_assistant_message")
        if type(message) is not str:
            gaps = (*gaps, "outcome_gap:missing_assistant_body")
            message = ""
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


def emit_result(result: dict[str, Any], *, diagnostics: HookDiagnostics | None = None) -> None:
    # Codex decodes hook stdout as UTF-8, while a Windows child process may
    # inherit a legacy code-page TextIOWrapper.  ASCII JSON is safe on both
    # sides and json.loads restores the original Unicode values.
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
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
