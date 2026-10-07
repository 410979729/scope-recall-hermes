"""Which session the Hermes adapter speaks for: the one Hermes opens (``initialize``), the one it switches to
(``on_session_switch``), and the line a desktop or tui session bound to no scope says in the host's log.  It works on
the adapter's state under the adapter's lock (``self._adapter``)."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from scope_recall.core import CoreConfig, MemoryCore

from .audiences import LOCAL_PLATFORMS
from .identity import (
    HermesIdentity,
    assert_same_installation,
    bind_hermes_identity,
    switch_hermes_identity,
    unbound_session_hint,
)
from .installation import assert_core_binding_matches
from .runtime_wiring import attach_trusted_host_runtime

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: The adapter's log name, which these lines carried before the adapter was split: a host writes it into each line,
#: and a logging configuration may name it.
_log = logging.getLogger("scope_recall.adapters.hermes.provider")


def _start_vector_helper(host_runtime) -> None:
    """Start the vector search's helper when a gateway first binds (``vector.process_store.prestart``).

    The first search opened it then, and its LanceDB import (about 2 s) could outrun that recall's budget: a probe
    run as a gateway's first turn after a start came back without its vector search (``helper_open_deadline``).
    """
    runtime = getattr(host_runtime, "runtime", None)
    if sys.platform != "win32" or runtime is None or runtime.config.vector is None:
        return
    try:
        from ...vector.process_store import prestart

        prestart()
    except OSError as exc:
        # The first search starts its own helper, as before: slower, never a reason not to bind.
        _log.warning("could not start a vector helper ahead: %s", type(exc).__name__)


class SessionBinding:
    """A Hermes adapter's session; one per adapter."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter
        #: The route this session last reported as bound to no scope; a session switch on it is not reported again.
        self._unbound_route: tuple[str, ...] | None = None

    def bind(self, session_id: str, **kwargs) -> None:
        fresh = bind_hermes_identity(session_id, **kwargs)
        with self._adapter._lock:
            assert_same_installation(self._adapter._identity, fresh)
            runtime_path = kwargs.get("trusted_runtime_config_path") or fresh.runtime_config_path
            if self._adapter._host_runtime is None:
                self._adapter._host_runtime = attach_trusted_host_runtime(
                    config_path=runtime_path,
                    expected_binding=fresh.binding,
                    session_id=fresh.session_id,
                    allowed_scope_ids=fresh.writable_scope_ids,
                    core=self._adapter._core,
                    clock=self._adapter._clock,
                )
                _start_vector_helper(self._adapter._host_runtime)
            else:
                self._adapter._host_runtime.rebind_session(
                    fresh.session_id,
                    fresh.writable_scope_ids,
                )
            self._adapter._core = self._adapter._host_runtime.core
            if self._adapter._core is None:
                self._adapter._core = MemoryCore(CoreConfig(fresh.binding), clock=self._adapter._clock)
            else:
                assert_core_binding_matches(self._adapter._core, fresh.binding)
            # An unknown or unconfigured audience is a valid fail-closed
            # capability state: initialize succeeds so the host can report
            # the gap, while no Core read/write is attempted.
            if fresh.runtime_audience.allowed_scope_ids:
                self._adapter._core.status(fresh.trusted_context())
            self._adapter._identity = fresh
            self._adapter._ledger.reset()
            self._adapter._turn_counter = 0
            self._adapter._active_turn_id = ""
            self._adapter._pre_llm_pending = False
            with self._adapter._said_lock:
                self._adapter._interim_said.clear()
                self._adapter._steer_said.clear()
                self._adapter._notice_turns.clear()
            self._adapter._user_captured_turns.clear()
            self._adapter._session_watermark = 0
            self._adapter._reset_current_source_refs()
            self._adapter._current_task_message = ""
            # The buffer stays: each capture keeps the session, actor and scope it was said in, and is written under
            # its own scope's grant (``_retry_pass``).  Cleared here, a session started again in this adapter dropped
            # tool results that had only met a busy store.
            self._adapter._diagnostics.capability_gaps = tuple(
                dict.fromkeys((*fresh.runtime_audience.capability_gaps, *self._adapter._host_runtime.capability_gaps))
            )
            self._adapter._worker_launch_gaps = ()
            self._adapter._initialized = True
            self._unbound_route = None
            self._say_if_unbound(fresh)
            from .hooks import update_adapter_binding

            update_adapter_binding(self._adapter)

    def switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        identity = self._adapter._require_identity()
        fresh = switch_hermes_identity(identity, new_session_id, parent_session_id=parent_session_id, **kwargs)
        self._adapter._outcomes.reset_session(identity.session_id)
        self._adapter._ledger.reset()
        self._adapter._reset_current_source_refs()
        # A compression gives the conversation a new session id in the middle of a turn, and the turn goes on:
        # its id and what it said on the way stay, or its post_llm_call no longer matched the turn and what it
        # said between tool calls, and what the person sent meanwhile, was never recorded.
        if reset or kwargs.get("reason") != "compression":
            self._adapter._current_task_message = ""
            self._adapter._pre_llm_pending = False
            with self._adapter._said_lock:
                # With the turn id cleared under the same lock, a post_llm_call of the old session that arrives
                # late finds no turn to keep its copy for.
                self._adapter._active_turn_id = ""
                self._adapter._interim_said.clear()
                self._adapter._steer_said.clear()
                self._adapter._notice_turns.clear()
            self._adapter._user_captured_turns.clear()
        self._adapter._identity = fresh
        runtime_audience = fresh.runtime_audience
        self._adapter._diagnostics.capability_gaps = tuple(
            dict.fromkeys(
                (
                    *runtime_audience.capability_gaps,
                    *(self._adapter._host_runtime.capability_gaps if self._adapter._host_runtime else ()),
                )
            )
        )
        self._adapter._worker_launch_gaps = ()
        self._say_if_unbound(fresh)
        if self._adapter._host_runtime is not None:
            self._adapter._host_runtime.rebind_session(new_session_id, fresh.writable_scope_ids)
        from .hooks import update_adapter_binding

        update_adapter_binding(self._adapter)
        if reset:
            self._adapter._turn_counter = 0
        self._adapter._session_watermark += 1

    def _say_if_unbound(self, identity: HermesIdentity) -> None:
        """Say once per session, in the host's log, that a desktop or tui session binds no scope (#175).

        Such a session fails closed: nothing in it is captured or recalled.  Hermes reads none of this
        adapter's diagnostics, so without this line a Desktop login's sessions wrote nothing for days and
        nothing said so.  A gateway chat left unmapped is the owner's choice and says nothing, as before: a line
        for each would name its users, some by phone number (review of 3.4.10).  The platform, the login and the
        gap codes only, never what was said, and nothing a login could make into a line of its own.
        """
        scope = identity.scope
        if (
            scope.platform not in LOCAL_PLATFORMS
            or scope.agent_context != "primary"
            or identity.runtime_audience.allowed_scope_ids
        ):
            self._unbound_route = None
            return
        route = (scope.platform, scope.user_id, scope.chat_type, scope.chat_id, scope.thread_id, scope.agent_workspace)
        if route == self._unbound_route:
            return
        self._unbound_route = route
        said = (
            "scope-recall: session bound to no memory scope: a %s session for %s (%s); nothing in it is "
            "captured or recalled; %s"
        ) % (
            scope.platform,
            scope.user_id[:120],
            ", ".join(identity.runtime_audience.capability_gaps),
            unbound_session_hint(scope),
        )
        _log.warning("%s", "".join(character for character in said if character.isprintable()))
