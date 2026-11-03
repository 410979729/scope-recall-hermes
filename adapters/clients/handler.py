"""Dispatch Codex (and Claude Code) hook events through the single MemoryCore boundary."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol, cast

from scope_recall.contracts import ContractError, Origin, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.capture_inbox import DELETED_KEY, PendingCapture
from scope_recall.core.events import prepare_capture
from scope_recall.core.recall_budget import DEFAULT_INJECTION_TOKEN_CAP
from scope_recall.core.recall_trace import record_unbound_attempt
from scope_recall.core.session_brief import observe_session_start
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.writer_lease import TruthWriterBusyError
from scope_recall.runtime.instance_config import RuntimeInstanceConfig

from ..host_profiles import HOSTS, agent_run
from ..runtime_wiring import strict_hook_budget
from .boundary import (
    filter_capture_reply,
    without_lone_surrogates,
)
from .bounded_call import BoundedCall
from .config import CodexConfigError, CodexInstallationConfig, SharedClientConfig, load_codex_config, load_shared_client
from .hook_answer import (
    HookDiagnostics,
)
from .hook_events import (
    CAPTURE_TIMEOUT_S,
    PROMPT_CAPTURE_TIMEOUT_S,
    RUNTIME_ATTACH_MIN_S,
    WORKER_START_MIN_S,
    HookEvents,
)
from .identity import project_root_of, resolve_runtime_audience, stored_session_id, trusted_context
from .prompt_recall import PromptRecall
from .record_locator import remember
from .record_reader import RecordLines, RecordReader
from .runtime_wiring import (
    GAP_UNCONFIGURED,
    GAP_WORKER_LAUNCH_FAILED,
    TrustedHostRuntime,
    attach_trusted_host_runtime,
)
from .session_marks import (
    clear_compaction,
    compaction_marker,
    forget_turns,
    in_suggestions_thread,
    mark_compaction,
    rebase_compaction,
)

_MAX_STDIN_BYTES = 65536
_TOTAL_BUDGET_S = 2.0
_SUPPORTED_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse", "Interrupt", "SessionEnd"})
#: Attached prompt hooks may use the entry's bounded processing allowance. The installed command timeout also
#: leaves room for loading, serialization and shutdown outside the recall operation.
_CONFIGURED_PROMPT_BUDGET = frozenset({"claude-code", "codex", "workbuddy", "dsh"})
_HOST_EVENTS = {host: profile.events for host, profile in HOSTS.items()}


class HookClock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


class SystemHookClock:
    def utc_now(self) -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class HookCall:
    """One hook call's own state."""

    #: Whether it stored its message, or left it in the durable inbox.
    persisted: bool = False
    queued: bool = False
    #: The turn a WorkBuddy Stop closed and the words of its reply, for the read of the record after it.
    closed_reply: tuple[str, str] | None = None
    #: The id of the line holding the reply a dsh Stop stored, and its turn, for the read of the lines after it.
    reply_line: tuple[str, str] | None = None
    #: Whether this hook may open the session record its payload names: a client on another machine sends its lines.
    local_record: bool = True
    #: That client's word that its Stop's reply is an error its record marks.
    client_error_reply: bool = False

    @property
    def captured(self) -> bool:
        return self.persisted or self.queued


def _hook_core(config: CodexInstallationConfig | SharedClientConfig, clock: HookClock | None) -> MemoryCore:
    """The core a hook captures through until its trusted runtime attaches: its writes may wait for the writer lease as
    long as the owner's prompt asks (``PROMPT_CAPTURE_TIMEOUT_S``); every other capture asks for its 1 s.  A store waits
    no longer than its core's write timeout, whatever a capture asks, and at the default 1 s a prompt that met a lease
    held longer would not be stored."""
    return MemoryCore(CoreConfig(config.to_binding(), write_timeout_seconds=PROMPT_CAPTURE_TIMEOUT_S), clock=clock)


class CodexHookHandler:
    """Stateless per-process handler; durable idempotence lives in core SQLite."""

    def __init__(
        self,
        config: CodexInstallationConfig | SharedClientConfig,
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
            self.core = _hook_core(config, clock)
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
        self.call = HookCall()
        self._pending_runtime_config_path: str | None = None
        self._injection_request_id: str | None = None
        self._runtime_attach_attempted = host_runtime is not None
        #: The normalized project directory this hook's payload names, if any.  A
        #: shared entry's configured audience has none, so the request's cwd is
        #: used for project-scoped private intervals only; it grants no scope.
        self._project_root: str | None = None
        #: Host settings require the session's actual working directory, not an audience fallback.
        self._host_working_dir: str | None = None
        self._compacted_at: str | None = None
        self._compaction_seq: int | None = None
        self._compaction_epoch: str | None = None
        self.compaction_ack: int | None = None
        self.compaction_watermark: int | None = None
        self.remote_paths = False
        self.background_prompt_capture = False
        self.pending_capture: PendingCapture | None = None
        #: An attach the last hook stopped waiting for (``ensure_runtime``), still this handler's.
        self._attaching: BoundedCall | None = None
        self._starting: BoundedCall | None = None
        #: The entry's running MCP server, asked for a prompt's recall (``local_endpoint.Recaller``): given the
        #: payload, the stored refs, the gaps and the seconds it may take, the result and its diagnostics, or None.
        self.resident_recall: Callable[..., tuple[dict[str, Any], dict[str, Any]] | None] | None = None
        #: How a prompt's recall went with the server, when the hook decided it (``PromptRecall.answer``): ``slow``,
        #: ``late``, ``without_vectors:<gap>`` or ``failed:<reason>``.  The hook's stderr says this, or else what
        #: ``resident_recall`` says of itself.
        self.resident_outcome: str | None = None
        self.prompt_recall = PromptRecall(self)
        self.record_reader = RecordReader(self)
        self.events = HookEvents(self)
        self._upgrade_wake_context: tuple[Any, float] | None = None
        if host_runtime is None and isinstance(getattr(self.core, "storage", None), SQLiteStorage):
            self.core.storage.on_upgrade_pending = self._wake_pending_upgrade

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
    ) -> CodexHookHandler:
        config = load_codex_config(config_path)
        if host_runtime is None and core is None:
            # Capture uses a basic Core first.  Trusted runtime attach
            # (vectors/auxiliary/worker) waits until after a durable Source commit.
            core = _hook_core(config, clock)
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
        remote_paths: bool = False,
    ) -> CodexHookHandler:
        """A client attached to a shared store; its runtime config is the entry's, beside its pointer.

        ``event_clock``, when given, dates the hook's own events (a remote client's moment), while the store keeps
        ``clock``'s time for when it stored them.
        """
        config = load_shared_client(home, host)
        core = _hook_core(config, clock)
        handler = cls(
            config, core=core, clock=event_clock if event_clock is not None else clock, hook_started_at=hook_started_at
        )
        handler.remote_paths = remote_paths
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

    # -- trusted runtime -------------------------------------------------

    def bind_project_root(self, payload: dict[str, Any], audience) -> str | None:
        """Bind the directory a hook names for project-scoped private intervals.

        The hook's cwd supplies privacy identity. The configured project root
        remains the audience authorization boundary, independently of this directory.
        """
        value = payload.get("cwd")
        self._project_identity = "unverified" if payload.get("project_unverified") is True else "remote"
        self._host_working_dir = project_root_of(value, resolve_links=not getattr(self, "remote_paths", False))
        self._project_root = self._host_working_dir or project_root_of(audience.matched_project_root)
        return self._project_root

    def context(self, audience, session_id: str, origin: Origin) -> TrustedContext:
        context = trusted_context(
            self.config,
            audience,
            session_id=session_id,
            actor_origin=origin,
            project_root=self._project_root,
        )
        if self.remote_paths or getattr(self, "_project_identity", None) == "unverified":
            context = replace(context, project_identity=getattr(self, "_project_identity", "remote"))
        return replace(
            context,
            compacted_at=self._compacted_at,
            compaction_seq=self._compaction_seq,
            compaction_epoch=self._compaction_epoch,
        )

    def ensure_runtime(self, audience=None, *, deadline: float | None = None) -> None:
        """Attach the vector and worker runtime only after Source persist, or for wakeup.

        With a hook's ``deadline`` the attach has what is left of the hook's time: with less than
        ``RUNTIME_ATTACH_MIN_S`` it is not begun, and the hook waits for it no longer than its time
        (``BoundedCall``).  One the hook stopped waiting for is this handler's once a later call here finds it done
        (a kept handler's next prompt), and a handler closed meanwhile closes it."""
        if self._host_runtime is not None:
            return
        attaching = self._attaching
        if attaching is None:
            if self._runtime_attach_attempted:
                return
            if deadline is not None and self.remaining(deadline) < RUNTIME_ATTACH_MIN_S:
                return
            self._runtime_attach_attempted = True
            attaching = self._attaching = BoundedCall(
                partial(self._attach, audience),
                name="scope-recall-runtime-attach",
                remaining_seconds=None if deadline is None else self.remaining(deadline),
            )
        if not attaching.wait(None if deadline is None else self.remaining(deadline)):
            self.note("runtime_attach_late")
            return
        self._attaching = None
        host_runtime, self.diagnostics.runtime_attach_ms = attaching.result()
        if host_runtime is None:
            self.note("runtime_attach_failed", gaps=("capability_gap:trusted_runtime_invalid",))
            return
        self._host_runtime = host_runtime
        self.core = host_runtime.core
        self._merge_runtime_gaps()

    def _attach(self, audience) -> tuple[TrustedHostRuntime | None, int]:
        """The trusted runtime, or None when it could not be attached, and how long that took in milliseconds."""
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
            host_runtime = None
        return host_runtime, round((time.monotonic() - started) * 1000)

    def launch_worker(
        self, session_id: str, audience, *, require_persisted: bool = True, deadline: float | None = None
    ) -> None:
        """Wake the owned worker.  With a hook's ``deadline`` the start has what is left of the hook's time: with less
        than ``WORKER_START_MIN_S`` it is not begun, and the hook waits for it no longer than its time.  The work it
        would have done waits for whatever wakes the worker next: a session's start, which wakes it for work already
        pending, or the next turn's end, session's end or queued prompt with the time for it."""
        if self._host_runtime is None or not self._host_runtime.configured:
            if self.call.captured:
                self._merge_runtime_gaps()
            return
        if require_persisted and not self.call.captured:
            return
        if deadline is not None and self.remaining(deadline) < WORKER_START_MIN_S:
            self.note("runtime_worker_deferred")
            return
        starting = self._starting
        if starting is None:
            starting = self._starting = BoundedCall(
                partial(self._launch, self._host_runtime, session_id, audience),
                name="scope-recall-worker-start",
                remaining_seconds=None if deadline is None else self.remaining(deadline),
            )
        if not starting.wait(None if deadline is None else self.remaining(deadline)):
            self.note("runtime_worker_late")
            return
        worker_gaps = starting.result()
        self._starting = None
        if worker_gaps:
            self.note("runtime_worker", gaps=worker_gaps)

    def _launch(self, runtime, session_id: str, audience) -> tuple[str, ...]:
        """Start the bounded worker for ``audience``: the gaps its start leaves."""
        try:
            launch = getattr(runtime, "maybe_launch_bounded_worker", None)
            if not callable(launch):
                return (GAP_WORKER_LAUNCH_FAILED,)
            partition = self.context(audience, session_id, "host_generated")
            return cast(Callable[..., tuple[str, ...]], launch)(
                session_id=session_id,
                allowed_scope_ids=audience.allowed_scope_ids,
                project_id=partition.project_id,
                branch_id=partition.branch_id,
            )
        except Exception:
            return (GAP_WORKER_LAUNCH_FAILED,)

    def _wake_after_capture(self, session_id: str, audience, deadline: float) -> None:
        """After a Stop/SessionEnd capture, attach the runtime and wake the owned worker, each within the hook's
        time."""
        if self.call.captured:
            self.ensure_runtime(audience, deadline=deadline)
        self.launch_worker(session_id, audience, deadline=deadline)

    @property
    def runtime_ready(self) -> bool:
        """Whether this handler has its trusted runtime attached from a config it could read.  One kept for later
        prompts (``local_endpoint.KeptRecaller``) never attaches again, so without it the handler is made anew: a
        config read at a bad moment (a sharing violation) would leave every later recall without its vector search,
        where a handler of its own reads it again."""
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
        from ...core.private import outbound_blocked

        context = TrustedContext(self.config.to_binding(), "warm-embedding", self.config.scope_ids, "host_generated")
        if outbound_blocked(self.core.storage, self.core.clock, context):
            return
        self.ensure_runtime()
        warm = getattr(getattr(self._host_runtime, "_runtime", None), "warm_query_embedding", None)
        if callable(warm):
            warm(seconds)

    def close(self) -> None:
        attaching, self._attaching = self._attaching, None
        if attaching is not None:
            # An attach the hook stopped waiting for: the runtime it makes is closed once it is made.
            attaching.abandon(lambda made: made[0].close() if made[0] is not None else None)
        if self._host_runtime is not None:
            # A short hook must return without synchronously killing the
            # already-owned bounded watchdog.  The watchdog owns cleanup and
            # removes its ephemeral trusted config when the drain exits.
            runtime, self._host_runtime = self._host_runtime, None
            starting, self._starting = self._starting, None
            if starting is None:
                runtime.close(detach_worker=True)
            else:
                starting.abandon(lambda _result: runtime.close(detach_worker=True))

    # -- payload dispatch ------------------------------------------------

    def session_of(self, payload: dict[str, Any]) -> str | None:
        session_id = payload.get(HOSTS[self.host].session_field)
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

    def _load_compaction(self, event, session_id, payload, compacted_at, compaction_seq, compaction_epoch):
        """Bind the pending host compaction without consuming it on owner commands."""
        if self.remote_paths:
            if compacted_at is not None:
                from scope_recall.contracts import utc_timestamp

                try:
                    self._compacted_at = utc_timestamp(compacted_at)
                except ContractError:
                    pass
            if (
                type(compaction_seq) is int
                and 1 <= compaction_seq < 2**63
                and type(compaction_epoch) is str
                and 1 <= len(compaction_epoch) <= 64
            ):
                self._compaction_seq, self._compaction_epoch = compaction_seq, compaction_epoch
        elif self.host in {"claude-code", "codex"}:
            if event == "SessionStart" and payload.get("source") == "compact":
                mark_compaction(self.config, session_id, self.clock.utc_now())
            marker = compaction_marker(self.config, session_id)
            if marker is not None:
                self._compacted_at = marker["at"]
                self._compaction_seq = marker["ordinal"]
                self._compaction_epoch = marker["epoch"]

    def _handle_start_or_prompt(self, event, session_id, audience, payload, deadline):
        """Deliver and account for a normal start or prompt before confirming compaction."""
        context = self.context(audience, session_id, "host_generated")
        # An entry state that cannot be read means nothing is injected; the reason stays visible to doctor
        # and the host, e.g. SCHEMA_UNSUPPORTED while another process upgrades the store.
        try:
            if self.core.entry_state(context)[0] == "paused":
                self.note("entry_state:paused")
                return {}
        except ContractError as exc:
            self.note(exc.code)
            return {}
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            self.note("entry_state_unavailable")
            return {}
        if event == "SessionStart":
            if (
                self.host in {"claude-code", "codex"}
                and self.call.local_record
                and not self.remote_paths
                and payload.get("source") != "compact"
            ):
                try:
                    observe_session_start(
                        self.core.storage,
                        context,
                        datetime.now(UTC).isoformat(),
                        remaining_seconds=min(0.05, max(0.001, self.remaining(deadline))),
                    )
                except (ContractError, sqlite3.Error, OSError, ValueError, TruthWriterBusyError):
                    pass
            result = self._session_started(session_id, audience, deadline, payload)
        else:
            result = self.events.prompt(session_id, audience, payload, deadline)
        return self._finish_compaction(
            session_id,
            audience,
            deadline,
            self._confirm_brief(result, deadline, self.context(audience, session_id, "host_generated")),
        )

    def handle_payload(
        self,
        payload: dict[str, Any],
        *,
        record: RecordLines | None = None,
        local_record: bool = True,
        error_reply: bool = False,
        compacted_at: str | None = None,
        compaction_seq: int | None = None,
        compaction_epoch: str | None = None,
    ) -> dict[str, Any]:
        payload = without_lone_surrogates(payload)
        # A client on another machine sends its record's lines, and its own judgement of the Stop's reply.
        self.call = HookCall(local_record=record is None and local_record, client_error_reply=error_reply)
        self.diagnostics = HookDiagnostics(capability_gaps=self.diagnostics.capability_gaps)
        self._pending_brief = None
        self._injection_request_id = None
        self._compacted_at = None
        self._compaction_seq = None
        self._compaction_epoch = None
        self.compaction_ack = None
        self.compaction_watermark = None
        event = payload.get("hook_event_name")
        self.diagnostics.last_event = str(event) if event is not None else None
        if event == "PrivateCheck":
            return self._private_check(payload)
        admitted = self._admit(event, payload)
        if admitted is None:
            return {}
        session_id, audience = admitted
        if event == "UserPromptSubmit":
            command_result = self.events.recording_command(session_id, audience, payload)
            if command_result is not None:
                return command_result
        self._load_compaction(event, session_id, payload, compacted_at, compaction_seq, compaction_epoch)
        # The extended trusted budget is for the auto recall path and for a client's
        # read of its session record.  The other hooks keep their short processing cap.
        budget = (
            self._hook_budget() if event == "UserPromptSubmit" or self.record_reader.reads(event) else _TOTAL_BUDGET_S
        )
        deadline = self._hook_deadline(budget)
        self._injection_started = self.clock.monotonic()
        self._upgrade_wake_context = audience, deadline
        if event in {"SessionStart", "UserPromptSubmit"}:
            return self._handle_start_or_prompt(event, session_id, audience, payload, deadline)
        if self.host == "codex" and in_suggestions_thread(self.config, session_id, ended=event == "SessionEnd"):
            # A thread opened for application suggestions contains application activity. Its mark excludes later
            # tool calls, replies and end events as well as the request that opened it.
            self.note("host_generated_thread")
            return {}
        if event == "Interrupt":
            return self._finish_compaction(
                session_id, audience, deadline, self.events.interrupt(session_id, audience, payload, deadline)
            )
        if event == "PostToolUse":
            return self._finish_compaction(
                session_id, audience, deadline, self.events.post_tool_use(session_id, audience, payload, deadline)
            )
        return self._finish_compaction(
            session_id,
            audience,
            deadline,
            self._turn_ended(event, session_id, audience, payload, deadline, record=record, local_record=local_record),
        )

    def _private_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bind a host's content-free request to the same Core policy as capture."""
        session_id = self.session_of(payload)
        audience = self.audience_of(payload)
        if session_id is None or audience is None:
            return {}
        self.bind_project_root(payload, audience)
        self._upgrade_wake_context = audience, self._hook_deadline(_TOTAL_BUDGET_S)
        return self.core.private_check(self.context(audience, session_id, "host_generated"), payload.get("times"))

    def _wake_pending_upgrade(self) -> None:
        """Admit maintenance before a hook needs usable storage or a successful capture."""
        if self._upgrade_wake_context is None:
            return
        audience, deadline = self._upgrade_wake_context
        self.ensure_runtime(audience, deadline=deadline)
        if self._host_runtime is not None:
            self._host_runtime.wake_upgrade(known_pending=True)

    def _note_unbound_prompt(self, session_id, payload):
        """Translate an authenticated hook refusal into core-only diagnostic metadata."""
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return
        context = TrustedContext(
            self.config.to_binding(),
            stored_session_id(self.config, session_id),
            frozenset(),
            "human_direct",
            entry_id=self.config.entry_id if isinstance(self.config, SharedClientConfig) else None,
            project_root=project_root_of(payload.get("cwd"), resolve_links=False),
            project_identity="unverified",
        )
        record_unbound_attempt(self.core, context, payload.get("prompt"))

    def _admit(self, event: object, payload: dict[str, Any]) -> tuple[str, Any] | None:
        """The session and audience of a hook this client sends and this handler takes, or None, said why."""
        if event not in _HOST_EVENTS[self.host]:
            self.note("unsupported_event")
            return None
        if agent_run(self.host, payload):
            # A subagent's prompt is the agent that started it speaking, and its end is not the session's: like a
            # task notification, none of it is the person's. The host can send these hooks for the main session
            # only; this keeps a later version that sends them from storing a subagent under the person's session.
            self.note("agent_run")
            return None
        session_id = self.session_of(payload)
        if session_id is None:
            return None
        audience = self.audience_of(payload)
        if audience is None:
            self._note_unbound_prompt(session_id, payload)
            return None
        self.bind_project_root(payload, audience)
        if self.call.local_record:
            remember(self.config, payload)
        if self._host_runtime is not None:
            self._host_runtime.rebind_session(session_id, audience.allowed_scope_ids)
            self._merge_runtime_gaps()
        return session_id, audience

    def briefing(self, context: TrustedContext, deadline: float, *, compact: bool = False) -> str:
        """Deliver this session's prepared background within the hook deadline."""
        runtime = self._host_runtime.runtime if self._host_runtime is not None else None
        if runtime is None:
            enabled, tokens = _configured_brief(self._pending_runtime_config_path)
        else:
            enabled, tokens = runtime.config.session_brief_enabled, runtime.config.session_brief_tokens
        try:
            if not enabled and not compact:
                with self.core.storage.read(context, remaining_seconds=self.remaining(deadline)) as tx:
                    tx.memory_epoch()
                return ""
            brief = self.core.session_brief(
                context,
                token_limit=tokens if enabled else 0,
                deliver=enabled,
                compact=compact,
                remaining_seconds=self.remaining(deadline),
                defer_confirmation=True,
            )
        except (ContractError, sqlite3.Error, OSError, ValueError):
            self.note("brief_unavailable")
            return ""
        if brief.get("delivery_reason") == "entry_state:paused":
            self.note("entry_state:paused")
        self.diagnostics.brief_tokens = brief["tokens"]
        self.diagnostics.brief_elapsed_ms = brief.get("elapsed_ms", 0)
        self.diagnostics.brief_items = tuple((item["ref"], item["revision"]) for item in brief["items"])
        if "_epoch" in brief:
            self._pending_brief = (context, brief)
        return brief["text"]

    def _confirm_brief(self, result: dict, deadline: float, context) -> dict:
        """Fit and confirm only the final response chosen by this host hook."""
        pending = self._pending_brief
        brief = pending[1] if pending else None
        output = result.get("hookSpecificOutput", {})
        text = output.get("additionalContext", "")
        if not text and self.remaining(deadline) <= 0:
            return result
        try:
            self._injection_limits()
            prepared = self.core.prepare_session_injection(
                context,
                text,
                brief=brief,
                request_id=self._injection_request_id,
                remaining_seconds=self.remaining(deadline),
            )
            prepared["_injection"]["elapsed_ms"] = max(
                0, round((self.clock.monotonic() - self._injection_started) * 1000) - prepared["elapsed_ms"]
            )
            reason = self.core.confirm_session_brief(context, prepared, remaining_seconds=self.remaining(deadline))
        except Exception:  # noqa: BLE001 - an unfinished safety check cannot release prepared text.
            self.note("brief_safety_unconfirmed")
            return {}
        usage = prepared["_injection"]
        for name in ("injection_token_cap", "session_token_cap", "session_tokens"):
            setattr(self.diagnostics, name, usage[name])
        self.diagnostics.injection_tokens = usage["tokens"]
        self.diagnostics.injection_format = usage.get("format", "json")
        self.diagnostics.injection_refs = usage.get("references", ())
        self.diagnostics.delivery_reason = reason
        self.diagnostics.brief_tokens = prepared["tokens"]
        if usage["reason"]:
            self.note(usage["reason"])
        if reason in {"brief_delivery_unconfirmed", "injection_delivery_unconfirmed", "brief_safety_unconfirmed"}:
            self.diagnostics.session_statistics_failed = True
            self.note(reason)
        if reason == "brief_delivery_unconfirmed" and prepared.get("_log_checked"):
            self.compaction_watermark = prepared.get("_compaction_cutoff")
        if reason == "brief_delivered":
            self.compaction_ack = context.compaction_seq
            self.compaction_watermark = prepared.get("_confirmed_seq")
        if reason == "entry_state:paused":
            self.note(reason)
        if reason in {"brief_invalidated", "brief_safety_unconfirmed", "entry_state:paused"}:
            self.diagnostics.injection_tokens = 0
            self.diagnostics.injection_refs = ()
            return {}
        text = usage["text"]
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": self.diagnostics.last_event, "additionalContext": text}}

    def _injection_limits(self) -> None:
        """Read owner limits even when model runtime attachment is unavailable."""
        path = self._pending_runtime_config_path
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        self.core.config = replace(
            self.core.config,
            injection_format=raw.get("injection_format", "json"),
            injection_token_cap=raw.get("injection_token_cap", DEFAULT_INJECTION_TOKEN_CAP),
            session_token_cap=raw.get("session_token_cap"),
        )

    def _finish_compaction(self, session_id: str, audience, deadline: float, result: dict) -> dict:
        """Retire only a watermark that the core committed in this request."""
        if (
            self._compaction_seq
            and not self.compaction_ack
            and self.diagnostics.last_reason
            not in {"brief_delivery_unconfirmed", "brief_safety_unconfirmed", "entry_state:paused"}
        ):
            left = min(0.03, self.remaining(deadline))
            if left > 0:
                context = self.context(audience, session_id, "host_generated")
                try:
                    if self.core.sync_session_compaction(context, remaining_seconds=left):
                        self.compaction_watermark = self.core.session_compaction_watermark(context)
                        self.compaction_ack = self._compaction_seq
                except (ContractError, sqlite3.Error, OSError, ValueError, TruthWriterBusyError):
                    pass
        if (
            self._compaction_seq
            and self._compaction_epoch
            and self.compaction_watermark is not None
            and not self.remote_paths
        ):
            rebase_compaction(
                self.config, session_id, self._compaction_epoch, self._compaction_seq, self.compaction_watermark
            )
        if self.compaction_ack and not self.remote_paths:
            clear_compaction(
                self.config, session_id, self.compaction_ack, self._compaction_epoch, self.compaction_watermark
            )
        return result

    def _session_started(self, session_id: str, audience, deadline: float, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.context(audience, session_id, "host_generated")
        text = self.briefing(
            context,
            min(deadline, self.clock.monotonic() + CAPTURE_TIMEOUT_S),
            compact=payload.get("source") == "compact",
        )
        if not isinstance(self.config, SharedClientConfig) and self.diagnostics.last_reason != "brief_unavailable":
            self.ensure_runtime(audience, deadline=deadline)
            self.launch_worker(session_id, audience, require_persisted=False, deadline=deadline)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}} if text else {}

    def _turn_ended(
        self,
        event: object,
        session_id: str,
        audience,
        payload: dict[str, Any],
        deadline: float,
        *,
        record: RecordLines | None,
        local_record: bool,
    ) -> dict[str, Any]:
        """A Stop or a SessionEnd: its message stored, what the session record shows was said, the worker woken."""
        if self.host == "dsh" and record is None:
            # The turn's messages as dsh's plugin kept them (it has no record a hook could open), read as a remote
            # client's lines; the answer says how many are stored, so the plugin drops those and sends the rest again.
            record = RecordLines.from_dsh(payload)
        if event == "Stop":
            result = self.events.stop(session_id, audience, payload, deadline, record=record)
        else:
            result = self.events.session_end(session_id, audience, payload, deadline)
        # A server for a client on another machine reads only the lines that client sent (``local_record``
        # False): the payload's transcript_path names a file over there, and a path from a request is never
        # opened here.
        if self.record_reader.reads(event) and (record is not None or local_record):
            self.record_reader.read(
                session_id,
                audience,
                payload,
                deadline,
                remote=record,
                closed_reply=self.call.closed_reply,
                reply_line=self.call.reply_line,
            )
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
            # Nested past what the parser takes, a tool's output would otherwise end the hook with no answer.
            self.note("invalid_json")
            return {}
        if type(payload) is not dict:
            self.note("invalid_root")
            return {}
        filtered = filter_capture_reply(payload)
        if filtered is not None:
            return filtered
        result = self.handle_payload(payload)
        notice = self.diagnostics.private_notice()
        if notice:
            prior = result.get("systemMessage")
            result = {**result, "systemMessage": f"{prior}\n{notice}" if isinstance(prior, str) and prior else notice}
        if self.host == "dsh" and payload.get("hook_event_name") != "PrivateCheck":
            session_id, audience = self.session_of(payload), self.audience_of(payload)
            if session_id is not None and audience is not None:
                # The native spool learns about rule history from its ordinary
                # hooks, without starting a process for each message.
                result = {
                    **result,
                    "private_rules": self.core.private_rules_exist(
                        self.context(audience, session_id, "host_generated")
                    ),
                }
        return result

    # -- capture ---------------------------------------------------------

    def _capture_event(self, context, audience, event, deadline, gaps, via_inbox, wait):
        """Preserve prefiltered omission metadata through either capture route."""
        removed_gaps = tuple(
            gap for gap in gaps if gap == "private_content_omitted" or gap.startswith("private_spans_removed:")
        )
        prepared = None
        if removed_gaps and isinstance(self.core, MemoryCore):
            prepared = prepare_capture(event, context)
            prepared = replace(prepared, gaps=tuple(dict.fromkeys((*prepared.gaps, *removed_gaps))))
        if via_inbox:
            receipt = self.core.record_host_event(
                context,
                event,
                scope_id=audience.capture_scope_id,
                host_scope=audience.host_scope,
                remaining_seconds=min(wait, self.remaining(deadline)),
                **({"_prepared": prepared} if prepared is not None else {}),
                **(
                    {"background": True}
                    if self.background_prompt_capture and self.diagnostics.last_event == "UserPromptSubmit"
                    else {}
                ),
            )
            if isinstance(receipt, PendingCapture):
                self.pending_capture = receipt
                receipt = receipt.receipt
        else:
            receipt = self.core.record_event(
                context,
                event,
                scope_id=audience.capture_scope_id,
                remaining_seconds=min(wait, self.remaining(deadline)),
                **({"_prepared": prepared} if prepared is not None else {}),
            )
        return receipt

    def _note_private_spans(self, receipt):
        """Count content-free omission markers from a capture receipt."""
        for gap in set(receipt.gaps):
            if gap.startswith("private_spans_removed:"):
                count = gap.removeprefix("private_spans_removed:")
                if count.isascii() and count.isdecimal():
                    self.diagnostics.private_spans_removed += int(count)

    def capture(
        self,
        context,
        audience,
        event,
        *,
        deadline: float,
        gaps: tuple[str, ...] = (),
        via_inbox: bool = True,
        wait: float = CAPTURE_TIMEOUT_S,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Record one host event; returns the committed source refs and the accumulated gaps.

        ``via_inbox=False`` is for a message read from the session record, which keeps it until it is
        written: one write instead of the inbox's two.
        """
        if event is None:
            return (), gaps
        # Diagnostic milliseconds need a precise clock even when deadline clocks tick coarsely.
        started = time.perf_counter()
        self.diagnostics.capture_stage = "ingress"
        self.diagnostics.capture_durability = "not_persisted"
        if self.remaining(deadline) <= 0:
            self.diagnostics.capture_error_code = "DEADLINE_EXCEEDED"
            self.diagnostics.capture_elapsed_ms = 0
            self.note("deadline_exceeded", gaps=gaps)
            return (), gaps
        try:
            receipt = self._capture_event(context, audience, event, deadline, gaps, via_inbox, wait)
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.diagnostics.capture_durability = "unknown"
            self.diagnostics.capture_error_type = type(exc).__name__
            if isinstance(exc, ContractError):
                self.diagnostics.note_capture_error(exc.code)
                if (exc.code, exc.field) == DELETED_KEY:
                    # A copy of a deleted message under its key, written straight from the session record: refused for
                    # good, as the inbox cancels one.  Taken as unsettled, every later Stop would stop at that
                    # line.
                    self.diagnostics.capture_disposition = "cancelled"
            gaps = (*gaps, "capture_gap:write_exception")
            self.note("capture_exception", gaps=gaps)
            return (), gaps
        finally:
            elapsed = round((time.perf_counter() - started) * 1000)
            self.diagnostics.capture_elapsed_ms = elapsed
            self.diagnostics.capture_total_ms = (self.diagnostics.capture_total_ms or 0) + elapsed
        self.diagnostics.capture_disposition = receipt.disposition
        self.diagnostics.capture_durability = receipt.durability
        self.diagnostics.masked_secret_count += receipt.masked_secret_count
        self._note_private_spans(receipt)
        if receipt.error_code:
            self.diagnostics.note_capture_error(receipt.error_code)
        if receipt.durability == "queued":
            self.call.queued = True
            self.diagnostics.capture_stage = "durable_inbox"
            gaps = (*gaps, "capture_gap:durable_ingress_pending")
            self.note("capture_conflict_pending" if receipt.disposition == "conflict" else "capture_queued", gaps=gaps)
            return (), gaps
        if receipt.error_code in {"PRIVATE_MODE", "PRIVATE_MODE_UNVERIFIED"}:
            gap = "private_mode" if receipt.error_code == "PRIVATE_MODE" else "private_mode_unverified"
            gaps = (*gaps, f"capture_gap:{gap}")
            self.note("private_mode", gaps=gaps)
            return (), gaps
        if receipt.durability != "persisted":
            gaps = (*gaps, *receipt.gaps, f"capture_gap:{receipt.disposition}")
            self.note("capture_unavailable", gaps=gaps)
            return (), gaps
        self.call.persisted = True
        self.diagnostics.capture_stage = "source_committed"
        refs = tuple(f"{write.ref}@{write.revision}" for write in receipt.event_refs)
        return refs, (*gaps, *receipt.gaps)

    def finish_prompt_capture(self) -> None:
        """Read the immediate commit's current receipt without waiting for it."""
        if self.pending_capture is None:
            return
        receipt = self.pending_capture.receipt
        self.diagnostics.capture_disposition = receipt.disposition
        self.diagnostics.capture_durability = receipt.durability
        if receipt.error_code:
            self.diagnostics.note_capture_error(receipt.error_code)
        if receipt.durability == "persisted":
            self.call.persisted, self.call.queued = True, False
            self.diagnostics.capture_stage = "source_committed"
            if self.diagnostics.last_reason == "capture_queued":
                self.diagnostics.last_reason = None
        elif receipt.disposition == "conflict":
            self.note("capture_conflict_pending")
        elif receipt.durability != "queued":
            self.call.queued = False
            self.diagnostics.capture_stage = "ingress"
            if self.diagnostics.last_reason == "capture_queued":
                self.note(
                    "private_mode"
                    if receipt.error_code in {"PRIVATE_MODE", "PRIVATE_MODE_UNVERIFIED"}
                    else "capture_unavailable"
                )

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
        return self.prompt_recall.for_resident(payload, current_refs, gaps, remaining)


#: What a runtime config that does not name ``hook_processing_seconds`` runs: the worker's default.  No
#: installer writes the key, so without this an entry's hooks would fall back to 2 s, and most automatic
#: recalls would come back empty.
_DEFAULT_CONFIGURED_BUDGET_S = RuntimeInstanceConfig.hook_processing_seconds


def _configured_brief(runtime_config_path: str | None) -> tuple[bool, int]:
    """Read the entry's briefing switch and limit without attaching model services."""
    if not runtime_config_path:
        return True, 1024
    try:
        raw = json.loads(Path(runtime_config_path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False, 1024
        enabled = raw.get("session_brief_enabled", True)
        tokens = raw.get("session_brief_tokens", 1024)
        if type(enabled) is not bool or type(tokens) is not int or not 1 <= tokens <= 8192:
            return False, 1024
        return enabled, tokens
    except FileNotFoundError:
        return True, 1024
    except (OSError, UnicodeError, ValueError):
        return False, 1024


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
        return strict_hook_budget(raw["hook_processing_seconds"])
    except (OSError, UnicodeError, ValueError):
        return None
