"""Hermes hook registration with one global, instance-aware dispatcher."""
from __future__ import annotations

from typing import Any, Callable
import itertools
import logging
import threading
import time
import weakref

_log = logging.getLogger(__name__)

_SUPPORTED_HOOKS = ("pre_llm_call", "api_request_error", "post_tool_call", "post_llm_call")
_OBSERVERS = {"pre_llm_call": "observe_pre_llm", "api_request_error": "observe_api_request_error",
              "post_tool_call": "observe_post_tool_call", "post_llm_call": "observe_post_llm_call"}
#: Hermes' default ``plugins.hook_callback_timeout``.  Past it Hermes 0.21.5 abandons the call and skips the callback
#: for 60 s; this dispatcher is one callback per hook for every session of the gateway, so every session's hook is
#: skipped.
_HOST_HOOK_TIMEOUT_S = 30.0
#: The longest a hook waits for its own session, at most a third of the host's timeout.
_SESSION_WAIT_CAP_S = 10.0
_REGISTRY_LOCK = threading.RLock()
_ADAPTERS: weakref.WeakSet[Any] = weakref.WeakSet()
#: When each adapter last bound its session (``initialize``, a session switch), in order: a session's hooks go to
#: the adapter that bound it last.
_BOUND: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_BINDINGS = itertools.count(1)
_REGISTERED_CONTEXTS: weakref.WeakSet[Any] = weakref.WeakSet()
_FALLBACK_CONTEXT_IDS: set[int] = set()


def _register_adapter_instance(adapter: Any) -> None:
    with _REGISTRY_LOCK:
        _ADAPTERS.add(adapter)
        _BOUND[adapter] = next(_BINDINGS)


def _unregister_adapter_instance(adapter: Any) -> None:
    with _REGISTRY_LOCK:
        _ADAPTERS.discard(adapter)


def _active_adapter(kwargs: dict[str, Any]) -> Any | None:
    session_id = str(kwargs.get("session_id") or "").strip()
    if not session_id:
        return None
    platform = str(kwargs.get("platform") or "").strip().lower()
    sender_id = str(kwargs.get("sender_id") or "").strip()
    with _REGISTRY_LOCK:
        matches = []
        for adapter in tuple(_ADAPTERS):
            identity = getattr(adapter, "_identity", None)
            if identity is None or not getattr(adapter, "_initialized", False):
                continue
            if identity.session_id != session_id:
                continue
            if platform and identity.scope.platform != platform:
                continue
            if sender_id and identity.scope.user_id != sender_id:
                continue
            matches.append(adapter)
        if not matches:
            return None
        bindings = {adapter._identity.binding for adapter in matches}
        audiences = {(tuple(sorted(item._identity.runtime_audience.allowed_scope_ids)),
                      item._identity.local_scope_id, item._identity.read_only,
                      item._identity.scope.chat_type, item._identity.scope.chat_id,
                      item._identity.scope.thread_id) for item in matches}
        if len(bindings) != 1 or len(audiences) != 1:
            # A global hook must never choose one installation for an
            # ambiguous session identifier.
            return None
        # Hermes rebuilds an agent its cache evicted: a new provider binds the same session, and the old one, retired
        # but not shut down, stays registered.  Chosen by the lower id(), the hooks often went to the old one:
        # pre_llm_call stored the message there under the turn's id, while on_turn_start and sync_turn reached the
        # new one, which stored the message again under an ordinal and the reply with it (tianji and yuheng: the
        # first turn after each rebuild, 6 of 48 turns from 2026-09-28).  The adapter that bound it last is the host's.
        return max(matches, key=lambda item: _BOUND.get(item, 0))


def host_hook_timeout() -> float | None:
    """Hermes' ``plugins.hook_callback_timeout`` as Hermes reads it, or its default outside Hermes; ``None`` when
    the operator set it to 0 or less, with which Hermes waits for a hook however long it takes."""
    try:
        from hermes_cli.plugins import _resolve_hook_callback_timeout  # pyright: ignore[reportMissingImports]
        timeout = float(_resolve_hook_callback_timeout())
    except Exception:
        return _HOST_HOOK_TIMEOUT_S
    return timeout if timeout > 0 else None


def _dispatch(event: str, kwargs: dict[str, Any], *, wait: float) -> Any | None:
    adapter = _active_adapter(kwargs)
    if adapter is None:
        return None
    if event == "post_llm_call":
        # Runs before Hermes sends the reply and writes nothing: it keeps its copy under the adapter's own
        # small lock, which also drops it when a session switch has cleared the turn meanwhile.
        adapter.observe_post_llm_call(**kwargs)
        return adapter
    # The hook waits for its own session only so long: waited out past the host's timeout, it was abandoned and the
    # host skipped this hook for every session (tianji 2026-09-26: three tool hooks behind their session's
    # prefetch).  One it cannot wait for is counted and said.
    if not adapter._lock.acquire(timeout=wait):
        adapter._session_busy(event, kwargs)
        return adapter
    try:
        # Session switch can occur after selection; never send that old
        # callback through the replacement audience's identity.
        if _active_adapter(kwargs) is adapter:
            getattr(adapter, _OBSERVERS.get(event, "observe_api_request_error"))(**kwargs)
    finally:
        adapter._lock.release()
    return adapter


def _global_callback(event: str) -> Callable[..., None]:
    def callback(**kwargs: Any) -> None:
        started, timeout = time.monotonic(), host_hook_timeout()
        adapter = _dispatch(event, kwargs, wait=_SESSION_WAIT_CAP_S if timeout is None else
                            min(_SESSION_WAIT_CAP_S, timeout / 3))
        elapsed = time.monotonic() - started
        if timeout is not None and elapsed >= timeout:
            _log.warning("scope-recall: %s took %.1f s, past the host's %g s hook timeout; the host skips it for "
                         "every session for the next minute", event, elapsed, timeout)
            if adapter is not None:
                adapter._count_backpressure(f"{event}_overran")

    # Hermes names a callback in its timeout and skip lines; every plugin's closure called ``callback`` read alike.
    callback.__name__ = callback.__qualname__ = f"scope_recall_{event}"
    callback.scope_recall_registration_identity = ("scope-recall", "global-dispatcher")
    return callback


def _context_registered(ctx: Any) -> bool:
    try:
        return ctx in _REGISTERED_CONTEXTS
    except TypeError:
        return id(ctx) in _FALLBACK_CONTEXT_IDS


def _mark_context_registered(ctx: Any) -> None:
    try:
        _REGISTERED_CONTEXTS.add(ctx)
    except TypeError:
        _FALLBACK_CONTEXT_IDS.add(id(ctx))


def register_capture_hooks(ctx: Any, adapter: Any) -> list[str]:
    """Register global callbacks once; adapter selection happens per event."""

    _register_adapter_instance(adapter)
    iter_hook_callbacks = _iter_hook_callbacks()
    if iter_hook_callbacks is None or _context_registered(ctx):
        return []
    registered: list[str] = []
    for event in _SUPPORTED_HOOKS:
        callback = _global_callback(event)
        ctx.register_hook(event, callback)
        registered.append(event)
    _mark_context_registered(ctx)
    return registered


def update_adapter_binding(adapter: Any) -> None:
    _register_adapter_instance(adapter)


def unregister_adapter(adapter: Any) -> None:
    _unregister_adapter_instance(adapter)


def _iter_hook_callbacks():
    try:
        from hermes_cli.plugins import iter_hook_callbacks  # pyright: ignore[reportMissingImports]
    except ImportError:
        return None
    return iter_hook_callbacks


def unsupported_host_fields() -> dict[str, str]:
    """Documented public gaps for this bounded slice."""

    return {
        "on_session_reset": "unsupported_in_adapter_slice_use_on_session_switch",
        "provider_queue_prefetch": "optional_noop_when_prefetch_is_synchronous",
        "png_raw_attachment_bytes": "unsupported_host_shape_metadata_only",
        "turn_cancelled_hook": "unsupported_public_hook_record_gap_only",
        "turn_interrupted_hook": "unsupported_public_hook_record_gap_only",
    }
