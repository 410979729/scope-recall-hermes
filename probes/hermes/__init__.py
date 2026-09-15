import json
import os
from pathlib import Path
import sqlite3
import time


MARKER = "TEST_SCOPE_RECALL_P02_HERMES_CONTEXT_42a9"


def register(ctx):
    from agent.memory_provider import MemoryProvider
    from hermes_cli.plugins import iter_hook_callbacks

    directory = Path(os.environ["HERMES_HOME"]).resolve()
    roots = [parent for parent in Path(__file__).resolve().parents if (parent / "probes/hermes/__init__.py").is_file() and (parent / "verification/P02").is_dir()]
    if not roots or not directory.is_relative_to((roots[0] / ".execution").resolve()):
        raise RuntimeError("P02 profile must stay under its development worktree")
    marker = directory / "TEST-P02-profile.json"
    if not directory.name.startswith("TEST-P02-") or not marker.is_file():
        raise RuntimeError("P02 probe only loads in its marked isolated TEST profile")
    declaration = json.loads(marker.read_text(encoding="utf-8"))
    if declaration != {"dataset":"SYNTHETIC_TEST_ONLY", "purpose":"P02_HOST_INTERFACE_PROBE"}:
        raise RuntimeError("Invalid P02 test profile marker")

    def observe(event, **payload):
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 240:
            return None
        turn_id = str(payload.get("turn_id", ""))[:240]
        message = payload.get("user_message", "")
        if isinstance(message, list):
            message = "\n".join(part.get("text", "") for part in message if isinstance(part, dict) and part.get("type") == "text")
        synthetic = isinstance(message, str) and message.startswith("TEST_SCOPE_RECALL ")
        if isinstance(message, str) and message.startswith("[A2A inbound "):
            synthetic = message.partition("\n\n")[2].startswith("TEST_SCOPE_RECALL ")
        with sqlite3.connect(directory / "probe.sqlite3", timeout=0.1) as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS observations (sequence INTEGER PRIMARY KEY, event TEXT, session_id TEXT, turn_id TEXT, observed_ns INTEGER, metadata TEXT)")
            if synthetic:
                db.execute("INSERT OR IGNORE INTO sessions VALUES (?)", (session_id,))
            lifecycle = {"provider_initialize", "provider_session_switch", "on_session_start", "on_session_reset"}
            if event not in lifecycle and not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return None
            metadata = {"field_types": {key:type(value).__name__ for key,value in payload.items()}, "test_probe_only":True, "transcript_scan":False}
            for key in ("completed", "failed", "interrupted", "retryable"):
                if isinstance(payload.get(key), bool):
                    metadata[key] = payload[key]
            for key in ("tool_call_id", "tool_name", "status", "turn_exit_reason", "api_request_id"):
                value = payload.get(key)
                if isinstance(value, str) and len(value) <= 240:
                    metadata[key] = value
            for key in ("status_code", "api_call_count", "retry_count", "max_retries"):
                if type(payload.get(key)) is int:
                    metadata[key] = payload[key]
            if "session_argument_empty" in payload:
                metadata["session_argument_empty"] = payload["session_argument_empty"]
                metadata["session_identity_source"] = "provider_binding" if payload["session_argument_empty"] else "callback_argument"
            messages = payload.get("conversation_history", payload.get("messages"))
            if isinstance(messages, list):
                metadata["message_shapes"] = [{"role":m.get("role"),"content_type":type(m.get("content")).__name__,"keys":sorted(m)} for m in messages[-8:] if isinstance(m,dict)]
            response = payload.get("assistant_response", payload.get("assistant_content"))
            if isinstance(response, str):
                metadata["assistant_contains_marker"] = MARKER in response
            db.execute("INSERT INTO observations(event,session_id,turn_id,observed_ns,metadata) VALUES (?,?,?,?,?)", (event,session_id,turn_id,time.time_ns(),json.dumps(metadata)))
        return {"context":f"Isolated P02 hook delivery marker: {MARKER}. Interface probe only, not recalled user history."} if event == "pre_llm_call" and synthetic else None

    class ProbeProvider(MemoryProvider):
        @property
        def name(self):
            return "scope-recall-p02"

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            if Path(kwargs["hermes_home"]).resolve() != directory:
                raise RuntimeError("P02 profile identity mismatch")
            self.session_id = session_id
            observe("provider_initialize", session_id=session_id, **kwargs)

        def on_session_switch(self, new_session_id, **kwargs):
            self.session_id = new_session_id
            observe("provider_session_switch", session_id=new_session_id, **kwargs)

        def get_tool_schemas(self):
            return []

        def prefetch(self, query, *, session_id=""):
            observe("provider_prefetch", session_id=session_id or self.session_id, session_argument_empty=not session_id)
            return ""

        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            observe("provider_sync_turn", user_message=user_content, assistant_content=assistant_content, session_id=session_id, messages=messages)

    ctx.register_memory_provider(ProbeProvider())
    for event in ("pre_llm_call", "post_llm_call", "post_tool_call", "api_request_error", "on_session_start", "on_session_end", "on_session_reset"):
        # Memory-provider activation calls register again for every agent. The
        # public hook registry is shared, including across loader namespaces.
        identity = ("scope-recall-p02", str(directory))
        if any(getattr(callback, "p02_registration_identity", None) == identity for callback in iter_hook_callbacks(event)):
            continue
        def callback(_event=event, **kwargs):
            return observe(_event, **kwargs)
        callback.p02_registration_identity = identity
        ctx.register_hook(event, callback)
