"""Hermes 0.21.0 operator-tool registration and fail-closed dispatch tests."""
from __future__ import annotations

import json
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.hermes.installation import _build_audience_scope_ids


def _payload(text: str, *, key: str) -> dict:
    return {
        "protocol_version": "1.1",
        "source_event_key": key,
        "source_revision": 1,
        "origin": "human_direct",
        "role": "user",
        "content": text,
        "occurred_at": None,
        "recorded_at": "2026-09-06T12:00:00Z",
        "time_precision": "unknown",
        "capture_state": "complete",
        "evidence_refs": [],
    }


def test_operator_tools_expose_frozen_names_and_strict_boundary(adapter):
    provider, _clock = adapter
    schemas = provider.get_tool_schemas()

    # ``get_tool_schemas`` appends ``trace_tool_schema()`` after the static
    # ``_TOOL_SCHEMAS`` block, so ``trace`` is last here rather than in the
    # middle as on the Codex surface.
    assert [schema["name"] for schema in schemas] == [
        "recall", "inspect", "profile", "entity", "revise", "forget", "status", "trace",
    ]
    assert all(schema["parameters"]["additionalProperties"] is False for schema in schemas)

    status = json.loads(provider.handle_tool_call("status", {}))
    assert status["protocol_version"] == "1.1"
    assert status["result"]["session"] == "TEST-session-1"
    assert status["result"]["platform"] == "cli"

    rejected = json.loads(provider.handle_tool_call(
        "status", {"protocol_version": "1.1", "forged_scope": "private"}
    ))
    assert rejected["error"] == {"code": "INPUT_INVALID", "field": "unknown_field"}


def test_cli_operator_tools_use_core_revision_and_exact_delete_receipt(adapter):
    provider, _clock = adapter
    core = provider._core
    identity = provider._identity
    context = identity.trusted_context()

    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="human-1", user_message="我喜欢茶。"
    )
    source_ref, source_revision = provider._current_source_refs[-1].rsplit("@", 1)
    proposal = {
        "protocol_version": "1.1",
        "source_refs": [f"{source_ref}@{source_revision}"],
        "claim_proposals": [{
            "kind": "preference", "subject": "我", "predicate": "喜欢",
            "value_text": "茶", "conditions": [], "statement_kind": "assertion",
            "valid_from": None, "valid_to": None,
            "evidence_spans": [{
                "source_ref": source_ref, "source_revision": int(source_revision),
                "quote": "我喜欢茶。",
            }],
        }],
        "resume_proposals": [], "reference_proposals": [],
    }
    claim = core.accept_claim_proposals(
        context, proposal, scope_id=identity.local_scope_id, remaining_seconds=5
    ).items[0]

    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="human-2",
        user_message=f"我喜欢茶这条 {claim.ref} 写错了，改为咖啡/茶。",
    )
    correction_ref = provider._current_source_refs[-1]
    current_before_revision = core.current_claim(context, claim.ref)
    assert current_before_revision is not None
    revised = json.loads(provider.handle_tool_call("revise", {
        "protocol_version": "1.1",
        "target_ref": claim.ref,
        "expected_revision": current_before_revision.revision,
        "new_value": "咖啡/茶",
        "conditions": [],
        "source_evidence_refs": [correction_ref],
        "valid_from": None,
    }))
    assert "result" in revised, revised
    assert revised["result"]["items"][0]["revision"] == current_before_revision.revision + 1

    inspected = json.loads(provider.handle_tool_call("inspect", {
        "protocol_version": "1.1", "ref": claim.ref,
    }))
    assert inspected["result"]["kind"] == "claim"
    assert inspected["result"]["revision"] == current_before_revision.revision + 1

    provider.observe_pre_llm(
        session_id="TEST-session-1", turn_id="human-3",
        user_message=f"忘记 {claim.ref}。",
    )
    forgotten = json.loads(provider.handle_tool_call("forget", {
        "protocol_version": "1.1",
        "target_refs": [claim.ref],
        "mode": "delete",
        "expected_revisions": {claim.ref: revised["result"]["items"][0]["revision"]},
    }))
    assert forgotten["result"]["requested_refs"] == [claim.ref]
    assert forgotten["result"]["mode"] == "delete"
    assert forgotten["result"]["memory_epoch"] > revised["result"]["memory_epoch"]


def test_a2a_same_named_mutators_reject_before_private_ref_lookup(hermes_home, initialize_kwargs):
    remote_scope = "scope:remote"
    owner_scope = _build_audience_scope_ids(
        platform="a2a",
        user_id="TEST-owner",
        agent_identity=initialize_kwargs["agent_identity"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        project_id=initialize_kwargs["agent_workspace"],
        conversation_key="group-1",
    )["owner_private"]
    audiences = [
        {
            "platform": "a2a", "user_id": "TEST-owner", "chat_type": "private",
            "chat_id": "TEST-owner", "thread_id": "main", "gateway_session_key": "",
            "agent_workspace": "TEST-workspace", "allowed_scope_ids": [owner_scope],
            "writable_scope_ids": [owner_scope], "capture_scope_id": owner_scope,
            "kind": "owner_private",
        },
        {
            "platform": "a2a", "user_id": "TEST-peer", "chat_type": "dm",
            "chat_id": "TEST-remote", "thread_id": "", "gateway_session_key": "",
            "agent_workspace": "TEST-workspace", "allowed_scope_ids": [remote_scope],
            "writable_scope_ids": [remote_scope], "capture_scope_id": remote_scope,
            "kind": "conversation",
        },
    ]
    _binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="a2a",
        user_id="TEST-owner",
        agent_workspace=initialize_kwargs["agent_workspace"],
        audiences=audiences,
        test_mode=False,
    )
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize("TEST-a2a-session", **dict(
        initialize_kwargs,
        platform="a2a",
        user_id="TEST-peer",
        chat_type="dm",
        chat_id="TEST-remote",
        thread_id="",
    ))
    try:
        for name, args in (
            ("revise", {
                "protocol_version": "1.1", "target_ref": "PRIVATE-CLAIM",
                "expected_revision": 1, "new_value": "leak",
                "conditions": [], "source_evidence_refs": [], "valid_from": None,
            }),
            ("forget", {
                "protocol_version": "1.1", "target_refs": ["PRIVATE-CLAIM"],
                "mode": "delete", "expected_revisions": {"PRIVATE-CLAIM": 1},
            }),
        ):
            result = json.loads(provider.handle_tool_call(name, args))
            assert result["error"] == {"code": "ACCESS_DENIED", "field": "origin"}
            assert result["origin"] == "memory_reinjection"
            assert "PRIVATE-CLAIM" not in json.dumps(result, ensure_ascii=False)
    finally:
        provider.shutdown()


def test_frozen_hermes_cli_dispatches_registered_status_tool_once():
    """The real Hermes 0.21.0 CLI must execute one local-stub tool round."""

    hermes_root = Path(os.environ.get("SCOPE_RECALL_TEST_HERMES_ROOT", ""))
    hermes_python = Path(os.environ.get("SCOPE_RECALL_TEST_HERMES_PYTHON", ""))
    v6_home = Path(os.environ.get("SCOPE_RECALL_TEST_HERMES_V6_HOME", ""))
    query_file = v6_home.parent / "cli-input.txt"
    if not (hermes_root.is_dir() and hermes_python.is_file() and v6_home.is_dir() and query_file.is_file()):
        import pytest
        pytest.skip("requires the isolated frozen Hermes TEST runtime and prepared v6 home")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self.server.payloads.append(payload)  # type: ignore[attr-defined]
            if len(self.server.payloads) == 1:  # type: ignore[attr-defined]
                message = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call-test-status-1", "type": "function",
                        "function": {
                            "name": "status",
                            "arguments": json.dumps({
                                "protocol_version": "1.1", "request_id": "cli-status-1",
                            }),
                        },
                    }],
                }
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "TEST-G0-HERMES-TOOL-ROUND-OK"}
                finish = "stop"
            body = json.dumps({
                "id": "chatcmpl-test-hermes-tool",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 29992), Handler)
    server.payloads = []  # type: ignore[attr-defined]
    server.timeout = 0.5
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set() and len(server.payloads) < 2:  # type: ignore[attr-defined]
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        repo = Path(__file__).parents[3]
        env = dict(os.environ)
        env.update({
            "HERMES_HOME": str(v6_home),
            "PYTHONPATH": os.pathsep.join((str(repo), str(hermes_root))),
            "SCOPE_RECALL_TEST_G0_LOCAL_TOKEN": "test-local-token",
        })
        completed = subprocess.run(
            [
                str(hermes_python), "-B", "-m", "hermes_cli.main", "chat",
                "--query-file", str(query_file), "-Q", "--source", "cli",
                "--provider", "g0-local-zero", "--toolsets", "memory",
            ],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=45,
        )
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=2)

    assert completed.returncode == 0, completed.stderr
    assert len(server.payloads) == 2  # type: ignore[attr-defined]
    first = server.payloads[0]  # type: ignore[attr-defined]
    assert '"name": "status"' in json.dumps(first.get("tools"), ensure_ascii=False), first.get("tools")
    second = server.payloads[1]  # type: ignore[attr-defined]
    assert any(
        message.get("role") == "tool" and "cli-status-1" in str(message.get("content"))
        for message in second.get("messages", [])
    )
    assert "TEST-G0-HERMES-TOOL-ROUND-OK" in completed.stdout
    capture_path = os.environ.get("SCOPE_RECALL_TEST_HERMES_TOOL_CAPTURE")
    if capture_path:
        def digest(value: object) -> str:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        status_schema = next(
            tool for tool in first.get("tools", [])
            if tool.get("name") == "status"
            or tool.get("function", {}).get("name") == "status"
        )
        tool_result = next(
            message["content"] for message in second.get("messages", [])
            if message.get("role") == "tool" and "cli-status-1" in str(message.get("content"))
        )
        capture = {
            "request_count": len(server.payloads),  # type: ignore[attr-defined]
            "status_schema_sha256": digest(status_schema),
            "status_tool_call_arguments_sha256": digest({
                "protocol_version": "1.1", "request_id": "cli-status-1",
            }),
            "status_tool_result_sha256": hashlib.sha256(str(tool_result).encode("utf-8")).hexdigest(),
            "final_public_output": "TEST-G0-HERMES-TOOL-ROUND-OK",
            "final_public_output_sha256": hashlib.sha256(
                b"TEST-G0-HERMES-TOOL-ROUND-OK"
            ).hexdigest(),
        }
        destination = Path(capture_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
