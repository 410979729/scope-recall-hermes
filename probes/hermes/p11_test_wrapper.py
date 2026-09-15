"""TEST-only Hermes wrapper for bounded loader diagnostics."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

_TRACE_HOME = Path(os.environ.get("HERMES_HOME", "")).resolve() if os.environ.get("HERMES_HOME") else None


def _trace(event: str, **fields: Any) -> None:
    """Write TEST-only loader facts; never write env, prompts, or secrets."""
    try:
        if _TRACE_HOME is None:
            return
        path = _TRACE_HOME / "scope-recall" / "p11-lifecycle-trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"event": event, "module": __name__, "file": str(Path(__file__).resolve()), "time_ns": time.time_ns()}
        record.update(fields)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _safe_error(value: BaseException) -> dict[str, str]:
    summary = " ".join(str(value).split())[:300]
    return {"error_module": type(value).__module__, "error_class": type(value).__name__, "error_summary": summary}


def _type_facts(value: Any) -> dict[str, str]:
    cls = type(value)
    return {"module": cls.__module__, "class": cls.__name__}


_IDENTITY_KEYS = (
    "session_id", "platform", "agent_context", "agent_identity", "agent_workspace",
    "user_id", "user_id_alt", "user_name", "chat_type", "chat_id", "chat_name", "thread_id",
    "gateway_session_key", "hermes_home", "parent_session_id", "sender_id", "task_id", "turn_id",
    "model", "provider", "api_mode", "status_code",
)


def _kwargs_facts(kwargs: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "keys": sorted(str(key) for key in kwargs),
        "value_types": {str(key): type(value).__name__ for key, value in kwargs.items()},
        "identity": {},
    }
    for key in _IDENTITY_KEYS:
        value = kwargs.get(key)
        if isinstance(value, str) and len(value) <= 240:
            facts["identity"][key] = value
        elif key in kwargs:
            facts["identity"][key] = {"type": type(value).__name__, "present": value is not None}
    messages = kwargs.get("messages")
    if isinstance(messages, (list, tuple)):
        facts["messages"] = {
            "count": len(messages),
            "roles": [str(item.get("role")) for item in messages if isinstance(item, dict)],
            "text_lengths": [len(str(item.get("content", ""))) for item in messages if isinstance(item, dict)],
            "test_marker_count": sum(
                str(item.get("content", "")).count("TEST_SCOPE_RECALL")
                for item in messages if isinstance(item, dict)
            ),
        }
    for key in ("user_message", "assistant_message"):
        if key in kwargs:
            value = kwargs[key]
            text = value if isinstance(value, str) else ""
            facts[key] = {"type": type(value).__name__, "length": len(text),
                          "test_marker": "TEST_" in text}
    return facts


def _install_provider_trace(provider: Any) -> None:
    if getattr(provider, "_p11_test_trace_installed", False):
        return
    original_initialize = provider.initialize

    def traced_initialize(session_id: str, **kwargs: Any) -> Any:
        _trace("provider_initialize_enter", session_id=session_id, **_kwargs_facts(kwargs))
        try:
            result = original_initialize(session_id, **kwargs)
        except Exception as exc:
            _trace("provider_initialize_error", session_id=session_id, **_safe_error(exc))
            raise
        diagnostics = getattr(provider, "diagnostics", None)
        _trace(
            "provider_initialize_return",
            session_id=session_id,
            diagnostic_capability_gaps=list(getattr(diagnostics, "capability_gaps", ()) or ()),
            diagnostic_unsupported_fields=getattr(diagnostics, "unsupported_fields", None),
        )
        return result

    provider.initialize = traced_initialize
    provider._p11_test_trace_installed = True


_source_root = None
for _entry in tuple(sys.path):
    try:
        _candidate = Path(_entry).resolve()
        if (_candidate / "scope_recall.py").is_file() and (_candidate / "adapters" / "hermes" / "provider.py").is_file():
            _source_root = str(_candidate)
            break
    except (OSError, RuntimeError, TypeError):
        continue
if _source_root is not None:
    sys.path[:] = [_source_root] + [item for item in sys.path if item != _source_root]

_scope_preloaded = sys.modules.get("scope_recall")
try:
    _scope_spec = importlib.util.find_spec("scope_recall")
    _scope_spec_origin = str(getattr(_scope_spec, "origin", "") or "")
    _scope_spec_locations = [str(item) for item in (getattr(_scope_spec, "submodule_search_locations", []) or [])]
except Exception:
    _scope_spec_origin = ""
    _scope_spec_locations = []
_trace(
    "wrapper_import_begin",
    sys_path=[str(item) for item in sys.path],
    source_root_promoted=_source_root or "",
    scope_preloaded_module=getattr(_scope_preloaded, "__name__", ""),
    scope_preloaded_file=str(getattr(_scope_preloaded, "__file__", "")),
    scope_preloaded_path=[str(item) for item in (getattr(_scope_preloaded, "__path__", []) or [])],
    scope_spec_origin=_scope_spec_origin,
    scope_spec_locations=_scope_spec_locations,
)
try:
    from scope_recall.adapters.hermes import register_adapter
    from scope_recall.adapters.hermes.provider import ScopeRecallHermesAdapter
except Exception as exc:
    _trace("wrapper_import_error", **_safe_error(exc))
    raise


class _TracingContext:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def register_memory_provider(self, provider: Any) -> Any:
        facts = _type_facts(provider)
        _trace("register_memory_provider_enter", provider_module=facts["module"], provider_class=facts["class"])
        result = self._inner.register_memory_provider(provider)
        _trace("register_memory_provider_return", provider_module=facts["module"], provider_class=facts["class"])
        return result

    def register_hook(self, event: Any, callback: Any) -> Any:
        callback_module = getattr(callback, "__module__", type(callback).__module__)
        callback_name = getattr(callback, "__qualname__", type(callback).__name__)
        _trace("register_hook_enter", hook_event=str(event), callback_module=callback_module, callback_name=callback_name)
        def traced_callback(**kwargs: Any) -> Any:
            _trace("hook_dispatch_enter", hook_event=str(event), callback_name=callback_name, **_kwargs_facts(kwargs))
            try:
                result = callback(**kwargs)
            except Exception as exc:
                _trace("hook_dispatch_error", hook_event=str(event), callback_name=callback_name, **_safe_error(exc))
                raise
            _trace("hook_dispatch_return", hook_event=str(event), callback_name=callback_name)
            return result

        result = self._inner.register_hook(event, traced_callback)
        _trace("register_hook_return", hook_event=str(event), callback_module=callback_module, callback_name=callback_name)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_scope_module = sys.modules.get("scope_recall")
_trace(
    "wrapper_import",
    scope_recall_module=getattr(_scope_module, "__name__", ""),
    scope_recall_file=str(getattr(_scope_module, "__file__", "")),
    scope_recall_path=[str(item) for item in (getattr(_scope_module, "__path__", []) or [])],
    register_adapter_module=getattr(register_adapter, "__module__", ""),
    register_adapter_name=getattr(register_adapter, "__qualname__", ""),
    provider_module=ScopeRecallHermesAdapter.__module__,
    provider_class=ScopeRecallHermesAdapter.__name__,
    provider_bases=[f"{base.__module__}.{base.__name__}" for base in ScopeRecallHermesAdapter.__mro__[1:]],
)


def register(ctx: Any) -> Any:
    ctx_facts = _type_facts(ctx)
    _trace("register_enter", context_module=ctx_facts["module"], context_class=ctx_facts["class"])
    try:
        result = register_adapter(_TracingContext(ctx))
    except Exception as exc:
        _trace("register_error", **_safe_error(exc))
        raise
    result_facts = _type_facts(result)
    _install_provider_trace(result)
    _trace("register_return", provider_module=result_facts["module"], provider_class=result_facts["class"])
    return result
