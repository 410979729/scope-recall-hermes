"""Candidate P18 Codex app-server transport.

This module is a protocol candidate, not a replacement for the Codex desktop
UI adapter.  It contains a real stdio JSON-RPC implementation for a future
explicitly authorized diagnostic, plus an offline JSONL fixture path used by
the narrow P18 tests.  No hook is invoked by this module: hook evidence is
only accepted from app-server notifications.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Protocol


MODEL = "gpt-5.6-luna"
EFFORT = "low"
# Live Codex may emit Reconnecting 2/5-5/5 after responseStreamDisconnected.
# 90s cut the first formal turn before 5/5; keep the same low-effort protocol.
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_STDOUT_BYTES = 8_388_608
# One tool-heavy poster turn exceeded 2048 JSONL notifications and marked a
# completed turn FAIL. Keep a bound; size it for multi-tool native turns.
MAX_STDOUT_LINES = 32_768
MAX_STDERR_BYTES = 1_024
MAX_CLEANUP_SECONDS = 4.0
POST_COMPLETION_USAGE_GRACE_SECONDS = 0.25
FORMAL_GATE_REASON = "P18_G0_G2_method_adjudication_required"
TOKEN_USAGE_UPDATED_METHODS = frozenset(
    {
        "thread/tokenUsage/updated",
        "turn/tokenUsage/updated",
        # Accept the spelling used by older app-server protocol snapshots,
        # while still requiring the explicit token-usage event below.
        "thread/token_usage/updated",
        "turn/token_usage/updated",
    }
)


class CodexAppServerError(RuntimeError):
    """A bounded protocol or admission failure."""


class FormalEvaluationBlocked(CodexAppServerError):
    """Formal P18 execution is unavailable until the method gate is decided."""


class BudgetBoundary(Protocol):
    """Outer-ledger seam; the transport never creates or owns the ledger."""

    def reserve(self, model: str, request: bytes) -> Any: ...

    def finish(self, reservation: Any, status: str, usage: Mapping[str, int] | None) -> str: ...


@dataclass(frozen=True)
class VerifiedUsageBaseline:
    """Cumulative usage bound to a prior completed turn on one thread."""

    thread_id: str
    prompt_tokens: int
    completion_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise ValueError("usage_baseline_thread_id")
        if any(type(value) is not int or value < 0 for value in (self.prompt_tokens, self.completion_tokens)):
            raise ValueError("usage_baseline_tokens")


def verified_usage_baseline_from_receipt(receipt: Mapping[str, Any]) -> VerifiedUsageBaseline:
    """Create a resume baseline only from a reliable prior transport receipt."""

    association = receipt.get("association")
    usage = receipt.get("usage")
    if receipt.get("status") != "PASS" or not isinstance(association, Mapping) or not isinstance(usage, Mapping) or usage.get("known") is not True:
        raise CodexAppServerError("usage_baseline_receipt_untrusted")
    thread_id = association.get("thread_id")
    cumulative = usage.get("cumulative_tokens")
    if not isinstance(thread_id, str) or not isinstance(cumulative, Mapping):
        raise CodexAppServerError("usage_baseline_receipt_missing_cumulative")
    prompt_tokens = cumulative.get("prompt_tokens")
    completion_tokens = cumulative.get("completion_tokens")
    if type(prompt_tokens) is not int or prompt_tokens < 0 or type(completion_tokens) is not int or completion_tokens < 0:
        raise CodexAppServerError("usage_baseline_receipt_invalid_cumulative")
    return VerifiedUsageBaseline(thread_id, prompt_tokens, completion_tokens)


@dataclass(frozen=True)
class ArmHookPolicy:
    """Expected hook capability for one P18 arm, not a trust grant."""

    arm_id: str
    require_user_prompt_submit: bool
    require_scope_recall_hook: bool
    forbid_scope_recall_hook: bool


ARM_HOOK_POLICIES: dict[str, ArmHookPolicy] = {
    "A": ArmHookPolicy("A", False, False, True),
    "B": ArmHookPolicy("B", False, False, True),
    "C": ArmHookPolicy("C", True, True, False),
    "D": ArmHookPolicy("D", False, False, True),
}


@dataclass(frozen=True)
class CodexTransportConfig:
    codex_exe: Path
    cwd: Path
    arm_id: str
    expected_hooks_policy: ArmHookPolicy
    model: str = MODEL
    effort: str = EFFORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    server_args: tuple[str, ...] = ("app-server",)
    allow_candidate_diagnostic: bool = False
    formal_evaluation: bool = False
    formal_config_path: Path | None = None
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        exe = Path(self.codex_exe).expanduser().resolve()
        cwd = Path(self.cwd).expanduser().resolve()
        if not exe.is_absolute() or not cwd.is_absolute():
            raise ValueError("codex_exe_and_cwd_must_be_absolute")
        if "test" not in str(cwd).lower():
            raise ValueError("codex_cwd_must_be_TEST_path")
        lowered = str(cwd).replace("/", "\\").lower().rstrip("\\")
        if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
            raise ValueError("production_path_forbidden")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model")
        policy = self.expected_hooks_policy
        if not isinstance(policy, ArmHookPolicy) or policy.arm_id != self.arm_id:
            raise ValueError("unknown_or_mismatched_arm_hook_policy")
        if self.effort != EFFORT:
            raise ValueError("candidate_transport_requires_low_effort")
        if not 0 < float(self.timeout_seconds) <= DEFAULT_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds")
        object.__setattr__(self, "codex_exe", exe)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "expected_hooks_policy", policy)
        if self.formal_config_path is not None:
            object.__setattr__(self, "formal_config_path", Path(self.formal_config_path).expanduser().resolve())


@dataclass(frozen=True)
class _PeerMessage:
    kind: str
    payload: Any


class _JsonlPeer:
    """Bounded JSONL reader/writer with EOF-aware response waits."""

    def __init__(self, process: subprocess.Popen[str] | None, fixture: Path | None) -> None:
        self.process = process
        self.fixture = fixture
        self.messages: queue.Queue[_PeerMessage] = queue.Queue()
        self.responses: dict[Any, dict[str, Any]] = {}
        self.request_id = 0
        self.reader_done = threading.Event()
        self.stderr_done = threading.Event()
        self.stdout_bytes = 0
        self.stdout_lines = 0
        self.stdout_truncated = False
        self.parse_errors = 0
        self.stderr_bytes = 0
        self.stderr_lines = 0
        self.stderr_truncated = False
        self._stderr_tail = bytearray()
        # Keep the bounded JSONL wire transcript available for a formal
        # evidence writer.  This is deliberately capped by the same stdout
        # budget as parsing; it never retains unbounded model output.
        self._raw_stdout = bytearray()

    def start(self) -> None:
        if self.fixture is not None:
            threading.Thread(target=self._read_fixture, daemon=True).start()
            self.stderr_done.set()
            return
        assert self.process is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_fixture(self) -> None:
        try:
            with self.fixture.open("r", encoding="utf-8") as handle:  # type: ignore[union-attr]
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._enqueue(line)
        finally:
            self.reader_done.set()

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._enqueue(line)
        finally:
            self.reader_done.set()

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            for line in self.process.stderr:
                raw = line.encode("utf-8", errors="replace")
                if self.stderr_bytes + len(raw) > MAX_STDERR_BYTES:
                    self.stderr_truncated = True
                    break
                self.stderr_bytes += len(raw)
                self.stderr_lines += 1
                self._stderr_tail.extend(raw)
        finally:
            self.stderr_done.set()

    def _enqueue(self, line: str) -> None:
        raw = line.strip()
        encoded = raw.encode("utf-8", errors="replace")
        if self.stdout_bytes + len(encoded) > MAX_STDOUT_BYTES or self.stdout_lines >= MAX_STDOUT_LINES:
            self.stdout_truncated = True
            return
        self.stdout_bytes += len(encoded)
        self.stdout_lines += 1
        self._raw_stdout.extend(encoded)
        self._raw_stdout.extend(b"\n")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.parse_errors += 1
            self.messages.put(_PeerMessage("parse_error", None))
            return
        self.messages.put(_PeerMessage("message", payload))

    def next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def send(self, payload: Mapping[str, Any]) -> None:
        if self.fixture is not None:
            return
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def ingest(self, payload: Mapping[str, Any], notification: Callable[[Mapping[str, Any]], None]) -> None:
        if "id" in payload and ("result" in payload or "error" in payload):
            self.responses[payload["id"]] = dict(payload)
        elif isinstance(payload.get("method"), str):
            notification(payload)

    def wait_response(
        self,
        request_id: int,
        deadline: float,
        notification: Callable[[Mapping[str, Any]], None],
    ) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            response = self.responses.pop(request_id, None)
            if response is not None:
                return response
            if self.reader_done.is_set() and self.messages.empty():
                return None
            try:
                item = self.messages.get(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if item.kind == "message" and isinstance(item.payload, Mapping):
                self.ingest(item.payload, notification)
        return self.responses.pop(request_id, None)

    def drain_until(
        self,
        deadline: float,
        completed: Callable[[], bool],
        notification: Callable[[Mapping[str, Any]], None],
    ) -> None:
        completed_at: float | None = None
        while time.monotonic() < deadline:
            is_completed = completed()
            if is_completed and completed_at is None:
                completed_at = time.monotonic()
            if self.reader_done.is_set() and self.messages.empty():
                return
            if completed_at is not None and time.monotonic() - completed_at >= POST_COMPLETION_USAGE_GRACE_SECONDS:
                return
            try:
                remaining = max(0.0, deadline - time.monotonic())
                if completed_at is not None:
                    remaining = min(remaining, max(0.0, POST_COMPLETION_USAGE_GRACE_SECONDS - (time.monotonic() - completed_at)))
                item = self.messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if completed_at is not None:
                    return
                continue
            if item.kind == "message" and isinstance(item.payload, Mapping):
                self.ingest(item.payload, notification)

    def close(self) -> str:
        if self.process is None:
            return "fixture"
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is not None:
            return "already_exited"
        try:
            self.process.terminate()
            self.process.wait(timeout=MAX_CLEANUP_SECONDS / 2)
            return "terminated"
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=MAX_CLEANUP_SECONDS / 2)
            return "killed"

    def stderr_summary(self) -> dict[str, Any]:
        return {
            "byteLength": self.stderr_bytes,
            "lineCount": self.stderr_lines,
            "sha256": hashlib.sha256(bytes(self._stderr_tail)).hexdigest() if self._stderr_tail else None,
            "truncated": self.stderr_truncated,
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _safe_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safe_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    aliases = {
        "inputTokens": "prompt_tokens",
        "promptTokens": "prompt_tokens",
        "prompt_tokens": "prompt_tokens",
        "outputTokens": "completion_tokens",
        "completionTokens": "completion_tokens",
        "completion_tokens": "completion_tokens",
        "totalTokens": "total_tokens",
        "total_tokens": "total_tokens",
    }
    result: dict[str, int] = {}
    for key, canonical in aliases.items():
        parsed = _safe_int(value.get(key))
        if parsed is not None:
            result[canonical] = parsed
    if "prompt_tokens" not in result or "completion_tokens" not in result:
        return None
    result.setdefault("total_tokens", result["prompt_tokens"] + result["completion_tokens"])
    return result


def _safe_event_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _safe_token_usage_event(method: Any, params: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one official tokenUsage update into bounded receipt data.

    App-server ``turn/completed`` payloads are deliberately not accepted here:
    only the dedicated tokenUsage update stream is authoritative for billing.
    Both identifiers are mandatory so usage from another thread or turn can
    never be reconciled into this operation.
    """
    if not isinstance(method, str) or method not in TOKEN_USAGE_UPDATED_METHODS:
        return None
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, Mapping):
        token_usage = params.get("token_usage")
    last = token_usage.get("last") if isinstance(token_usage, Mapping) else None
    total = token_usage.get("total") if isinstance(token_usage, Mapping) else None
    usage = _safe_usage(last)
    cumulative = _safe_usage(total)
    thread_id = _safe_event_id(params.get("threadId") or params.get("thread_id"))
    turn_id = _safe_event_id(params.get("turnId") or params.get("turn_id"))
    if thread_id is None:
        thread = params.get("thread")
        thread_id = _safe_event_id(thread.get("id")) if isinstance(thread, Mapping) else None
    if turn_id is None:
        turn = params.get("turn")
        turn_id = _safe_event_id(turn.get("id")) if isinstance(turn, Mapping) else None
    if thread_id is None or turn_id is None or usage is None and cumulative is None:
        return None
    updated = params.get("updated")
    if updated is None:
        updated = params.get("updatedAt")
    if updated is None:
        updated = params.get("updated_at")
    if not isinstance(updated, (str, int, float)) or isinstance(updated, bool):
        updated = None
    return {"method": method, "threadId": thread_id, "turnId": turn_id, "updated": updated, "usage": usage, "cumulative": cumulative}


def _latest_matching_usage(events: Any, thread_id: str | None, turn_id: str | None, *, baseline: Mapping[str, int] | None = None) -> tuple[dict[str, int] | None, list[dict[str, Any]]]:
    """Return a trusted turn delta, never a final internal-request ``last``.

    Every reliable result requires cumulative ``total`` snapshots and a
    verified baseline.  A resumed thread without that binding receives an
    unknown usage result; a last-only notification is never a billing basis.
    """
    matching = [
        dict(event)
        for event in (events if isinstance(events, list) else [])
        if isinstance(event, Mapping)
        and event.get("threadId") == thread_id
        and event.get("turnId") == turn_id
        and (isinstance(event.get("usage"), Mapping) or isinstance(event.get("cumulative"), Mapping))
    ]
    if not matching:
        return None, []
    # The stream is ordered, but use an explicit updated value when every
    # candidate has a comparable value so a late delivery cannot select an
    # older accounting snapshot.
    updated = [event.get("updated") for event in matching]
    if not all(isinstance(event.get("cumulative"), Mapping) for event in matching):
        return None, matching
    if baseline is not None and not all(type(baseline.get(name)) is int and baseline[name] >= 0 for name in ("prompt_tokens", "completion_tokens")):
        return None, matching
    if all(type(value) is int for value in updated) or all(isinstance(value, str) for value in updated):
        ordered = sorted(enumerate(matching), key=lambda pair: (pair[1].get("updated"), pair[0]))
    elif all(value is None for value in updated):
        ordered = list(enumerate(matching))
    else:
        return None, matching
    previous = baseline or {"prompt_tokens": 0, "completion_tokens": 0}
    final: Mapping[str, int] | None = None
    for _, event in ordered:
        cumulative = event["cumulative"]
        if any(type(cumulative.get(name)) is not int or cumulative[name] < previous[name] for name in ("prompt_tokens", "completion_tokens")):
            return None, matching
        previous = cumulative
        final = cumulative
    if final is None or baseline is None:
        return None, matching
    return {"prompt_tokens": final["prompt_tokens"] - baseline["prompt_tokens"], "completion_tokens": final["completion_tokens"] - baseline["completion_tokens"], "total_tokens": (final["prompt_tokens"] - baseline["prompt_tokens"]) + (final["completion_tokens"] - baseline["completion_tokens"])}, matching


def _safe_error(payload: Mapping[str, Any], kind: str) -> dict[str, Any]:
    error = payload.get("error")
    return {
        "kind": kind,
        "code": error.get("code") if isinstance(error, Mapping) and type(error.get("code")) is int else None,
        "error_type": "rpc_error" if isinstance(error, Mapping) else "unexpected_response",
    }


def _safe_hook_run(run: Mapping[str, Any]) -> dict[str, Any]:
    output_entries: list[dict[str, Any]] = []
    for entry in run.get("entries") or []:
        if not isinstance(entry, Mapping) or entry.get("kind") != "context":
            continue
        text = entry.get("text")
        if isinstance(text, str):
            output_entries.append({"kind": "context", "textSha256": _sha256_text(text), "textLength": len(text)})
    allowed = ("id", "eventName", "status", "executionMode", "handlerType", "scope", "source", "startedAt", "completedAt", "durationMs")
    return {key: run.get(key) for key in allowed if key in run} | {"entries": output_entries}


def _safe_hooks_list(result: Mapping[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    user_hooks: list[dict[str, Any]] = []
    for row in result.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        safe_hooks: list[dict[str, Any]] = []
        for hook in row.get("hooks") or []:
            if not isinstance(hook, Mapping):
                continue
            safe = {key: hook.get(key) for key in ("eventName", "key", "enabled", "trustStatus", "currentHash", "handlerType", "source", "isManaged", "displayOrder", "timeoutSec", "sourcePath") if key in hook}
            command = hook.get("command")
            if isinstance(command, str):
                safe["commandSha256"] = _sha256_text(command)
            safe_hooks.append(safe)
            if hook.get("eventName") == "userPromptSubmit":
                user_hooks.append(safe)
        entries.append({"cwd": row.get("cwd"), "errors": len(row.get("errors") or []), "warnings": len(row.get("warnings") or []), "hooks": safe_hooks})
    untrusted = [hook for hook in user_hooks if hook.get("trustStatus") != "trusted" or hook.get("enabled") is not True]
    return {"entries": entries, "userPromptSubmitHooks": user_hooks, "untrustedUserPromptSubmit": untrusted}


def _is_scope_recall_hook(hook: Mapping[str, Any]) -> bool:
    """Classify only explicit Scope Recall markers; never grant trust."""

    values = [hook.get("key"), hook.get("command"), hook.get("sourcePath")]
    command = hook.get("command")
    if isinstance(command, str) and " -EncodedCommand " in command:
        import base64
        try:
            values.append(base64.b64decode(command.split(" -EncodedCommand ", 1)[1], validate=True).decode("utf-16-le"))
        except (ValueError, UnicodeError):
            pass
    return any(
        isinstance(value, str)
        and any(token in value.lower() for token in ("scope-recall", "scope_recall", "scope recall"))
        for value in values
    )


def _hook_admission(result: Mapping[str, Any], policy: ArmHookPolicy, *, simple_commands: frozenset[str] = frozenset(), simple_source: Path | None = None) -> list[dict[str, Any]]:
    """Return fail-closed arm capability errors from raw hooks/list metadata."""

    all_hooks: list[Mapping[str, Any]] = []
    for row in result.get("data") or []:
        if isinstance(row, Mapping):
            all_hooks.extend(hook for hook in row.get("hooks") or [] if isinstance(hook, Mapping))
    def exact_simple(hook):
        return (policy.arm_id == "D" and simple_source is not None
                and Path(hook.get("sourcePath", "")).resolve() == simple_source
                and isinstance(hook.get("command"), str)
                and _sha256_text(hook["command"]) in simple_commands)
    scope_hooks = [hook for hook in all_hooks if not exact_simple(hook) and _is_scope_recall_hook(hook) and hook.get("enabled") is True]
    ups_hooks = [hook for hook in all_hooks if hook.get("eventName") == "userPromptSubmit" and hook.get("enabled") is True]
    errors: list[dict[str, Any]] = []
    if policy.arm_id == "D" and simple_commands:
        matching = [hook for hook in ups_hooks if exact_simple(hook) and hook.get("trustStatus") == "trusted"]
        if not matching:
            errors.append({"kind":"admission","error_type":"trusted_frozen_simple_search_hook_missing","arm_id":"D"})
    if policy.forbid_scope_recall_hook and scope_hooks:
        errors.append({"kind": "admission", "error_type": "scope_recall_hook_enabled_for_arm", "arm_id": policy.arm_id})
    if policy.require_user_prompt_submit and not ups_hooks:
        errors.append({"kind": "admission", "error_type": "user_prompt_submit_hook_missing", "arm_id": policy.arm_id})
    if policy.require_scope_recall_hook:
        trusted_scope_hooks = [
            hook for hook in scope_hooks if hook.get("eventName") == "userPromptSubmit" and hook.get("trustStatus") == "trusted"
        ]
        if not trusted_scope_hooks and not scope_hooks:
            errors.append({"kind": "admission", "error_type": "trusted_scope_recall_user_prompt_submit_missing", "arm_id": policy.arm_id})
    if policy.require_user_prompt_submit:
        untrusted_ups = [hook for hook in ups_hooks if hook.get("trustStatus") != "trusted"]
        if untrusted_ups:
            errors.append({"kind": "admission", "error_type": "user_prompt_submit_hook_untrusted", "arm_id": policy.arm_id})
    return errors


def _public_items(items: Any) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type not in {"userMessage", "agentMessage"}:
            continue
        if item_type == "agentMessage" and item.get("phase") not in {None, "final_answer"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            public.append({"type": item_type, "text": text})
    return public


def codex_binding_environment(binding: Mapping[str, Any]) -> dict[str, str]:
    environment = json.loads(Path(binding["roots"]["environment_path"]).read_text(encoding="utf-8-sig"))
    if binding["arm_id"] in {"C", "D"}:
        ref = binding["loader"]["hooks"]
        if hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() != ref["sha256"]:
            raise CodexAppServerError("native_hook_source_hash_mismatch")
        environment["SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS"] = ref["path"]
        environment["SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS_SHA256"] = ref["sha256"]
    return environment


def _simple_search_commands(config: CodexTransportConfig) -> frozenset[str]:
    if config.arm_id != "D":
        return frozenset()
    environment = config.environment or {}
    source = environment.get("SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS")
    digest = environment.get("SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS_SHA256")
    if not source or not digest:
        raise CodexAppServerError("frozen_simple_search_hook_binding_missing")
    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CodexAppServerError("native_hook_source_hash_mismatch")
    hooks = json.loads(raw)["hooks"]
    return frozenset(_sha256_text(command) for groups in hooks.values() for group in groups
        for hook in group["hooks"] for key in ("command", "commandWindows")
        if isinstance(command := hook.get(key), str))


def _configured_hook_admission(result: Mapping[str, Any], config: CodexTransportConfig) -> list[dict[str, Any]]:
    commands = _simple_search_commands(config) if config.arm_id == "D" else frozenset()
    return _hook_admission(result, config.expected_hooks_policy, simple_commands=commands,
        simple_source=(config.cwd/".codex/hooks.json").resolve() if commands else None)


def _trust_test_hooks(config: CodexTransportConfig, request: Callable) -> None:
    environment = getattr(config, "environment", None) or {}
    if not environment.get("SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS"):
        return
    target = (config.cwd / ".codex" / "hooks.json").resolve()
    response = request("config/value/write", {"keyPath": "projects", "value": {str(config.cwd): {"trust_level": "trusted"}}, "mergeStrategy": "upsert"})
    if not isinstance(response, Mapping) or "error" in response:
        raise CodexAppServerError("native_project_trust_config_failed")
    response = request("hooks/list", {"cwds": [str(config.cwd)]})
    result = response.get("result", response)
    state = {}
    simple_commands = _simple_search_commands(config) if getattr(config, "arm_id", None) == "D" else frozenset()
    for row in result.get("data", []):
        for hook in row.get("hooks", []):
            if Path(hook.get("sourcePath", "")).resolve() == target and (
                    _sha256_text(hook.get("command", "")) in simple_commands if getattr(config, "arm_id", None) == "D" else _is_scope_recall_hook(hook)):
                if not isinstance(hook.get("currentHash"), str) or not isinstance(hook.get("key"), str):
                    raise CodexAppServerError("native_hook_identity_missing")
                state[hook["key"]] = {"trusted_hash": hook["currentHash"], "enabled": True}
    if not state:
        raise CodexAppServerError("native_project_hooks_not_discovered")
    response = request("config/value/write", {"keyPath": "hooks.state", "value": state, "mergeStrategy": "upsert"})
    if not isinstance(response, Mapping) or "error" in response:
        raise CodexAppServerError("native_hook_trust_config_failed")


def _authenticate_test_peer(config: CodexTransportConfig, request: Callable) -> None:
    """Supply existing login tokens over stdin only, before any TEST model turn."""
    source = (getattr(config, "environment", None) or {}).get("SCOPE_RECALL_TEST_CODEX_AUTH_SOURCE") or os.environ.get("SCOPE_RECALL_TEST_CODEX_AUTH_SOURCE")
    if not source:
        return
    home = (getattr(config, "environment", None) or {}).get("CODEX_HOME", "")
    if not home or not any(part.upper().startswith("TEST") for part in Path(home).parts):
        raise CodexAppServerError("test_auth_requires_isolated_home")
    try:
        tokens = json.loads(Path(source).read_text(encoding="utf-8-sig"))["tokens"]
        access, account = tokens["access_token"], tokens["account_id"]
        if not all(isinstance(value, str) and value for value in (access, account)):
            raise ValueError()
    except Exception:
        raise CodexAppServerError("test_auth_source_unavailable") from None
    try:
        response = request("account/login/start", {"type": "chatgptAuthTokens", "accessToken": access, "chatgptAccountId": account})
        result = response.get("result", response) if isinstance(response, Mapping) else {}
        if result.get("type") != "chatgptAuthTokens":
            raise CodexAppServerError("test_auth_login_failed")
    finally:
        tokens.clear()
        access = account = ""


def _spawn(config: CodexTransportConfig) -> subprocess.Popen[str]:
    # Codex refuses an explicit, nonexistent HOME before JSON-RPC starts.
    # Fresh matrix bindings intentionally contain no persisted host state.
    environment = dict(config.environment or {})
    if environment.get("SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS"):
        if config.arm_id not in {"C", "D"}:
            raise CodexAppServerError("native_hooks_forbidden_for_baseline")
        if config.arm_id == "D":
            _simple_search_commands(config)
        source = Path(environment["SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS"])
        target = config.cwd / ".codex" / "hooks.json"
        raw = source.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != raw:
            raise CodexAppServerError("native_project_hooks_changed")
        target.write_bytes(raw)
    if environment.get("CODEX_HOME"):
        home = Path(environment["CODEX_HOME"]).resolve()
        if not any(part.upper().startswith("TEST") for part in home.parts):
            raise CodexAppServerError("codex_home_must_be_TEST_path")
        home.mkdir(parents=True, exist_ok=True)
        for name in ("TEMP", "TMP"):
            if environment.get(name):
                temp = Path(environment[name]).resolve()
                if not any(part.upper().startswith("TEST") for part in temp.parts):
                    raise CodexAppServerError("codex_temp_must_be_TEST_path")
                temp.mkdir(parents=True, exist_ok=True)
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    overrides = []
    if environment.get("SCOPE_RECALL_TEST_CODEX_NATIVE_HOOKS"):
        overrides = ["-c", f'projects.{json.dumps(str(config.cwd))}.trust_level="trusted"']
    return subprocess.Popen(
        [str(config.codex_exe), *overrides, *config.server_args],
        cwd=str(config.cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo,
        creationflags=creationflags,
        env={**os.environ, **dict(config.environment)} if config.environment is not None else None,
    )


class CodexAppServerTransport:
    """Candidate official app-server transport with fixture-only test seam."""

    transport = "candidate-codex-app-server-stdio-jsonrpc"
    formal_supported = False

    def __init__(self, config: CodexTransportConfig, *, budget: BudgetBoundary | None = None) -> None:
        if config.formal_evaluation:
            if config.formal_config_path is None:
                raise FormalEvaluationBlocked(FORMAL_GATE_REASON)
            try:
                from tests.eval.p18_formal_evidence import verify_formal_run_config

                readiness = verify_formal_run_config(config.formal_config_path)
            except Exception as exc:
                raise FormalEvaluationBlocked(FORMAL_GATE_REASON) from exc
            if not readiness.formal_execution_allowed:
                raise FormalEvaluationBlocked(FORMAL_GATE_REASON)
        elif not config.allow_candidate_diagnostic:
            raise CodexAppServerError("candidate_transport_requires_explicit_diagnostic_opt_in")
        self.config = config
        self.budget = budget
        self._started_peers: dict[str, tuple[_JsonlPeer, Mapping[str, Any]]] = {}
        self.formal_supported = bool(config.formal_evaluation)

    @classmethod
    def from_fixture(
        cls,
        fixture: Path,
        *,
        arm_id: str,
        cwd: Path | None = None,
        budget: BudgetBoundary | None = None,
    ) -> "CodexAppServerTransport":
        path = Path(fixture).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        policy = ARM_HOOK_POLICIES.get(arm_id)
        if policy is None:
            raise ValueError("unknown_arm_id")
        config = CodexTransportConfig(
            codex_exe=path,
            cwd=(cwd or path.parent).resolve(),
            arm_id=arm_id,
            expected_hooks_policy=policy,
            allow_candidate_diagnostic=True,
        )
        instance = cls(config, budget=budget)
        instance._fixture = path
        return instance

    def run_turn(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        raw_capture_path: Path | None = None,
        usage_baseline: VerifiedUsageBaseline | None = None,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt")
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise ValueError("thread_id")
        if usage_baseline is not None and not isinstance(usage_baseline, VerifiedUsageBaseline):
            raise CodexAppServerError("usage_baseline_untrusted")
        if thread_id is None and usage_baseline is not None:
            raise CodexAppServerError("usage_baseline_requires_resume_thread")
        if thread_id is not None and usage_baseline is not None and usage_baseline.thread_id != thread_id:
            raise CodexAppServerError("usage_baseline_thread_mismatch")
        fixture = getattr(self, "_fixture", None)
        if fixture is None and self.budget is None:
            raise CodexAppServerError("budget_adapter_required_for_runtime")
        prepared = self._started_peers.pop(thread_id, None) if thread_id else None
        if prepared is not None:
            peer, prepared_initialize = prepared
            process = peer.process
        else:
            process = None if fixture is not None else _spawn(self.config)
            if process is not None and callable(getattr(self, "process_observer", None)):
                self.process_observer(process)
            peer = _JsonlPeer(process, fixture)
            peer.start()
        deadline = time.monotonic() + self.config.timeout_seconds
        hook_events: list[dict[str, Any]] = []
        turn: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        initialize: Mapping[str, Any] | None = None
        hooks: dict[str, Any] = {"entries": [], "userPromptSubmitHooks": [], "untrustedUserPromptSubmit": []}
        thread_value = thread_id
        fresh_thread = thread_id is None or prepared is not None
        reservation: Any = None
        budget_status: str | None = None
        turn_dispatched = False
        thread_dispatched = False

        def notification(message: Mapping[str, Any]) -> None:
            nonlocal thread_value
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            if method in {"hook/started", "hook/completed"}:
                run = params.get("run") if isinstance(params.get("run"), Mapping) else {}
                hook_events.append(
                    {
                        "phase": "started" if method == "hook/started" else "completed",
                        **_safe_hook_run(run),
                        "threadId": params.get("threadId"),
                        "turnId": params.get("turnId"),
                    }
                )
            elif method == "thread/started":
                item = params.get("thread") if isinstance(params.get("thread"), Mapping) else {}
                if isinstance(item.get("id"), str):
                    thread_value = item["id"]
            elif method == "turn/started":
                item = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
                turn["started"] = {"threadId": params.get("threadId"), "turnId": item.get("id"), "status": item.get("status")}
            elif method in TOKEN_USAGE_UPDATED_METHODS:
                usage_event = _safe_token_usage_event(method, params)
                if usage_event is not None:
                    events = turn.setdefault("usageEvents", [])
                    if isinstance(events, list) and len(events) < 64:
                        events.append(usage_event)
            elif method == "turn/completed":
                item = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
                public = _public_items(item.get("items"))
                turn["completed"] = {
                    "threadId": params.get("threadId"),
                    "turnId": item.get("id"),
                    "status": item.get("status"),
                    "durationMs": item.get("durationMs"),
                    "startedAt": item.get("startedAt"),
                    "completedAt": item.get("completedAt"),
                }
                turn["publicItems"] = public

        def request(method: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
            request_id = peer.next_id()
            peer.send({"id": request_id, "method": method, "params": dict(params)})
            return peer.wait_response(request_id, deadline, notification)

        def finish_budget(status: str, usage: Mapping[str, int] | None) -> str | None:
            if self.budget is None or reservation is None:
                return None
            return self.budget.finish(reservation, status, usage)

        def early_receipt() -> dict[str, Any]:
            return self._receipt(
                peer,
                process,
                hook_events,
                hooks,
                turn,
                thread_value,
                errors,
                thread_dispatched,
                turn_dispatched,
                initialize,
                budget_status=budget_status,
                cleanup=peer.close(),
                raw_capture_path=raw_capture_path,
            )

        try:
            if prepared is not None:
                initialize = prepared_initialize
            else:
                initialize_response = request(
                    "initialize",
                    {"clientInfo": {"name": "scope-recall-p18-candidate", "version": "0.1"}, "capabilities": {"experimentalApi": True}},
                )
                if initialize_response is None or "result" not in initialize_response:
                    errors.append(_safe_error(initialize_response or {}, "initialize"))
                    return early_receipt()
                initialize = initialize_response.get("result") if isinstance(initialize_response.get("result"), Mapping) else {}
                peer.send({"method": "initialized"})
                _authenticate_test_peer(self.config, request)
                _trust_test_hooks(self.config, request)
            hooks_response = request("hooks/list", {"cwds": [str(self.config.cwd)]})
            if hooks_response is None or "result" not in hooks_response:
                errors.append(_safe_error(hooks_response or {}, "hooks/list"))
                return early_receipt()
            hooks_result = hooks_response.get("result") if isinstance(hooks_response.get("result"), Mapping) else {}
            hooks = _safe_hooks_list(hooks_result)
            errors.extend(_configured_hook_admission(hooks_result, self.config))
            if errors:
                return early_receipt()

            input_value = {"type": "text", "text": prompt}
            request_body = json.dumps({"model": self.config.model, "effort": self.config.effort, "input": [input_value], "approvalPolicy": "never"}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if self.budget is not None:
                reservation = self.budget.reserve(self.config.model, request_body)

            if prepared is not None:
                # This live peer already observed thread/start, with no model
                # turn. Keep its wire history and dispatch on that same process.
                thread_response = {"result": {"thread": {"id": thread_value}}}
            elif thread_value:
                thread_response = request("thread/resume", {"threadId": thread_value})
            else:
                thread_response = request("thread/start", {"cwd": str(self.config.cwd), "model": self.config.model, "ephemeral": False, "approvalPolicy": "never", "sandbox": "danger-full-access"})
            thread_dispatched = True
            if thread_response is None or "result" not in thread_response:
                errors.append(_safe_error(thread_response or {}, "thread/resume" if thread_id else "thread/start"))
                budget_status = finish_budget("error", None)
                return early_receipt()
            result_thread = thread_response.get("result", {}).get("thread") if isinstance(thread_response.get("result"), Mapping) else None
            if not thread_value and isinstance(result_thread, Mapping) and isinstance(result_thread.get("id"), str):
                thread_value = result_thread["id"]
            if not thread_value:
                errors.append({"kind": "protocol", "error_type": "thread_id_missing"})
                budget_status = finish_budget("error", None)
                return early_receipt()

            # Deliberately no client additionalContext: hook output must arrive
            # only through app-server hook notifications.
            turn_response = request(
                "turn/start",
                {
                    "threadId": thread_value,
                    "model": self.config.model,
                    "effort": self.config.effort,
                    "input": [input_value],
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                },
            )
            turn_dispatched = True
            if turn_response is None or "result" not in turn_response:
                errors.append(_safe_error(turn_response or {}, "turn/start"))
                budget_status = finish_budget("error", None)
                return early_receipt()
            turn_obj = turn_response.get("result", {}).get("turn") if isinstance(turn_response.get("result"), Mapping) else None
            active_turn_id = turn_obj.get("id") if isinstance(turn_obj, Mapping) else None
            peer.drain_until(deadline, lambda: turn.get("completed", {}).get("turnId") == active_turn_id, notification)
            if turn.get("completed", {}).get("turnId") != active_turn_id:
                errors.append({"kind": "timeout", "error_type": "turn_completed_timeout"})
            usage, usage_events = _latest_matching_usage(
                turn.get("usageEvents"),
                thread_value,
                active_turn_id,
                baseline=(
                    {"prompt_tokens": 0, "completion_tokens": 0}
                    if fresh_thread
                    else ({"prompt_tokens": usage_baseline.prompt_tokens, "completion_tokens": usage_baseline.completion_tokens} if usage_baseline is not None else None)
                ),
            )
            turn["usage"] = usage
            turn["usageEventCount"] = len(usage_events)
            cumulative_rows = [event for event in usage_events if isinstance(event.get("cumulative"), Mapping)]
            updated_values = [event.get("updated") for event in cumulative_rows]
            if cumulative_rows and (all(type(value) is int for value in updated_values) or all(isinstance(value, str) for value in updated_values)):
                final_cumulative = max(enumerate(cumulative_rows), key=lambda pair: (pair[1].get("updated"), pair[0]))[1].get("cumulative")
            else:
                final_cumulative = cumulative_rows[-1].get("cumulative") if cumulative_rows else None
            turn["usageCumulative"] = dict(final_cumulative) if usage is not None and isinstance(final_cumulative, Mapping) else None
            turn["usageProvenance"] = "official_token_usage_updated_cumulative" if usage is not None and turn["usageCumulative"] is not None else "unknown_no_verified_cumulative_token_usage"
            budget_status = finish_budget("completed" if not errors else "error", usage)
        except (OSError, ValueError, CodexAppServerError) as exc:
            errors.append({"kind": "transport", "error_type": type(exc).__name__})
            budget_status = finish_budget("error", None)
        finally:
            cleanup = peer.close()
        return self._receipt(
            peer,
            process,
            hook_events,
            hooks,
            turn,
            thread_value,
            errors,
            thread_dispatched,
            turn_dispatched,
            initialize,
            budget_status=budget_status,
            cleanup=cleanup,
            raw_capture_path=raw_capture_path,
        )

    def _receipt(
        self,
        peer: _JsonlPeer,
        process: subprocess.Popen[str] | None,
        hook_events: list[dict[str, Any]],
        hooks: Mapping[str, Any],
        turn: Mapping[str, Any],
        thread_id: str | None,
        errors: list[dict[str, Any]],
        thread_dispatched: bool,
        turn_dispatched: bool,
        initialize: Mapping[str, Any] | None,
        *,
        budget_status: str | None = None,
        cleanup: str | None = None,
        raw_capture_path: Path | None = None,
    ) -> dict[str, Any]:
        raw_capture = None
        if raw_capture_path is not None:
            target = Path(raw_capture_path).expanduser().resolve()
            lowered = str(target).replace("/", "\\").lower()
            if "test" not in lowered or lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
                raise ValueError("raw_capture_must_stay_in_TEST_path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(peer._raw_stdout))
            raw_capture = {
                "path": str(target),
                "sha256": _sha256_bytes(bytes(peer._raw_stdout)),
                "bytes": len(peer._raw_stdout),
            }
        public_items = list(turn.get("publicItems") or [])
        final_output = "\n".join(item["text"] for item in public_items if item.get("type") == "agentMessage")
        usage = turn.get("usage")
        usage_known = isinstance(usage, Mapping)
        return {
            "schema": "scope-recall.p18.codex-appserver-candidate-receipt.v1",
            "status": (
                "FAIL"
                if errors or peer.parse_errors or peer.stdout_truncated or turn.get("completed", {}).get("status") != "completed"
                else ("PASS" if usage_known else "PASS_USAGE_UNKNOWN")
            ),
            "transport": self.transport,
            "formal_evaluation": bool(self.config.formal_evaluation),
            "formal_gate": "verified" if self.config.formal_evaluation else FORMAL_GATE_REASON,
            "arm": {"arm_id": self.config.arm_id, "expected_hooks_policy": asdict(self.config.expected_hooks_policy)},
            "paths": {"cwd": str(self.config.cwd), "codex_exe": str(self.config.codex_exe)},
            "process": {
                "pid": process.pid if process is not None else None,
                "codex_exe_sha256": _sha256_file(self.config.codex_exe) if process is not None else None,
                "exit_code": process.returncode if process is not None else None,
                "cleanup": cleanup or "fixture",
            },
            "rpc_provenance": {"model_turns_requested": 1, "thread_rpcs_dispatched": int(thread_dispatched), "turn_rpcs_dispatched": int(turn_dispatched)},
            "initialize": {key: initialize.get(key) for key in ("codexHome", "platformFamily", "platformOs") if isinstance(initialize, Mapping) and key in initialize},
            "hooks_list": dict(hooks),
            "hook_events": hook_events,
            "association": {"thread_id": thread_id, "turn_id": turn.get("completed", {}).get("turnId") or turn.get("started", {}).get("turnId")},
            "public_output": final_output,
            "public_items": public_items,
            "usage": {"known": usage_known, "tokens": dict(usage) if usage_known else None, "cumulative_tokens": dict(turn.get("usageCumulative")) if isinstance(turn.get("usageCumulative"), Mapping) else None, "unknown_policy": "outer_budget_reservation_retained" if not usage_known else None, "budget_status": budget_status, "provenance": turn.get("usageProvenance", "unknown_no_verified_cumulative_token_usage"), "matching_event_count": turn.get("usageEventCount", 0)},
            "turn": dict(turn),
            "timing": {"duration_ms": turn.get("completed", {}).get("durationMs")},
            "io": {"stdout_truncated": peer.stdout_truncated, "parse_error_count": peer.parse_errors, "stderr": peer.stderr_summary()},
            "wire_capture": raw_capture,
            "errors": errors,
        }
