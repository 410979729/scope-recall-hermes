"""Hermes hook registration with one global, instance-aware dispatcher."""
from __future__ import annotations

from typing import Any, Callable
import threading
import weakref

_SUPPORTED_HOOKS = ("pre_llm_call", "api_request_error", "post_tool_call")
_REGISTRY_LOCK = threading.RLock()
_ADAPTERS: weakref.WeakSet[Any] = weakref.WeakSet()
_REGISTERED_CONTEXTS: weakref.WeakSet[Any] = weakref.WeakSet()
_FALLBACK_CONTEXT_IDS: set[int] = set()


def _register_adapter_instance(adapter: Any) -> None:
    with _REGISTRY_LOCK:
        _ADAPTERS.add(adapter)


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
        return sorted(matches, key=lambda item: id(item))[0]


def _dispatch(method: str, **kwargs: Any) -> None:
    adapter = _active_adapter(kwargs)
    if adapter is not None:
        with adapter._lock:
            # Session switch can occur after selection; never send that old
            # callback through the replacement audience's identity.
            if _active_adapter(kwargs) is adapter:
                getattr(adapter, method)(**kwargs)


def _global_callback(event: str) -> Callable[..., None]:
    def callback(**kwargs: Any) -> None:
        if event == "pre_llm_call":
            _dispatch("observe_pre_llm", **kwargs)
        elif event == "post_tool_call":
            _dispatch("observe_post_tool_call", **kwargs)
        else:
            _dispatch("observe_api_request_error", **kwargs)

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
        "post_llm_call": "success-only capture remains on MemoryProvider.sync_turn",
        "on_session_reset": "unsupported_in_adapter_slice_use_on_session_switch",
        "provider_queue_prefetch": "optional_noop_when_prefetch_is_synchronous",
        "png_raw_attachment_bytes": "unsupported_host_shape_metadata_only",
        "turn_cancelled_hook": "unsupported_public_hook_record_gap_only",
        "turn_interrupted_hook": "unsupported_public_hook_record_gap_only",
    }
