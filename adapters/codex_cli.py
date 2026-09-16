"""Tool-free, ephemeral Codex CLI ConsolidationPort; no scheduler or OAuth reader.

The supported binary is pinned because feature/catalog semantics are part of
this security boundary. The catalog removes shell/apply_patch/code-mode tools,
feature gates remove other tools, and read-only/never remains defense in depth.
A new CLI build must pass the offline wire test before its digest is admitted.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

from .models import AuxiliaryModelError
from ..core.secret_patterns import contains_secret_like_text
from ..runtime.subscription_budget import SubscriptionBudgetLedger, SubscriptionBudgetPolicy

MODEL = "gpt-5.6-luna"
# Filled only for binaries whose effective request tool catalog was verified.
VERIFIED_BINARIES = frozenset({
    "960c111d47afd61669954b9df9e56083e302edbfa3ef6962d81dcc14a30051dc",
})
MAX_OUTPUT_BYTES = 1048576
_DISABLED = (
    "apps", "auth_elicitation", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "code_mode", "code_mode_host", "code_mode_only",
    "computer_use", "context_management", "default_mode_request_user_input",
    "deferred_executor", "exec_permission_approvals", "external_agent_memory_import",
    "goals", "hooks", "image_generation", "in_app_browser", "memories",
    "multi_agent", "multi_agent_v2", "plugins", "remote_plugin", "request_permissions_tool",
    "shell_snapshot", "shell_tool", "skill_mcp_dependency_install", "skill_search",
    "sleep_tool", "standalone_web_search", "token_budget", "tool_call_mcp_elicitation",
    "tool_suggest", "unbounded_connection_retries", "unified_exec", "view_image",
    "workspace_dependencies",
)


@dataclass(frozen=True)
class CodexCliRouteConfig:
    executable: Path
    executable_sha256: str
    model: str = MODEL
    kind: str = "codex_cli"
    subscription_budget: SubscriptionBudgetPolicy = SubscriptionBudgetPolicy()

    def __post_init__(self):
        if self.model != MODEL or self.kind != "codex_cli":
            raise ValueError("codex_model_not_approved")
        if not Path(self.executable).is_absolute():
            raise ValueError("codex_executable_must_be_absolute")
        if not re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256):
            raise ValueError("codex_executable_sha256")

    @classmethod
    def from_mapping(cls, raw):
        if set(raw) - {"kind", "model", "executable", "executable_sha256", "subscription_budget"}:
            raise ValueError("codex_unknown_config")
        return cls(executable=Path(raw["executable"]), executable_sha256=raw["executable_sha256"],
                   model=raw.get("model", MODEL), kind=raw.get("kind", "codex_cli"),
                   subscription_budget=SubscriptionBudgetPolicy.from_mapping(raw.get("subscription_budget")))


def _child_env() -> dict[str, str]:
    # Reuse CLI login by home/keyring location, never inspect/copy credential
    # values. API-key/provider overrides and ambient Codex feature flags do not
    # enter the subprocess. Network proxy location remains an operator setting.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "CODEX_HOME", "HTTP_PROXY", "HTTPS_PROXY",
               "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(RUST_LOG="off", NO_COLOR="1")
    return env


def _kill_tree(process: subprocess.Popen) -> None:
    """Only the tree created by this invocation; never kill by image name."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run([str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=1)


_EVENT_TYPES = frozenset({"thread.started", "turn.started", "item.started", "item.updated",
                          "item.completed", "turn.completed", "turn.failed", "error"})
_HTTP_STATUS = re.compile(r"(?:http(?:/\d(?:\.\d)?)?\s*|status(?:\s+code)?[\s:=]*)([1-5]\d\d)\b", re.I)


def _run(command: list[str], *, cwd: Path, prompt: bytes, seconds: float,
         diagnostics: dict | None = None, environment: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Drain bounded pipes; expose only closed-vocabulary metadata, even on timeout.

    Raw stdout is returned only to the existing decoder. Stderr is classified
    in memory and discarded. Neither arbitrary event fields nor error text are
    copied into diagnostics. Each stream has a total byte and line-size bound.
    """
    started = time.monotonic()
    diag = diagnostics if diagnostics is not None else {}
    diag.update(phase="spawn", event_types=[], error_categories=[], http_statuses=[],
                elapsed_seconds=0.0, process_state="not_started", exit_code=None)
    if seconds <= 0:
        raise AuxiliaryModelError("timeout")
    process = subprocess.Popen(command, cwd=cwd, env=_child_env() if environment is None else environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != "nt",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    chunks: list[bytes] = []
    oversized = threading.Event()
    refused = threading.Event()
    lock = threading.Lock()

    def classify(text):
        lower = text.lower()
        statuses = {int(value) for value in _HTTP_STATUS.findall(text)}
        categories = set()
        for needles, category, status in (
            (("too many requests", "rate limit", "rate_limit"), "rate_limited", 429),
            (("unauthorized", "invalid api key", "token_invalidated"), "authentication", 401),
            (("forbidden", "permission denied"), "permission", 403),
            (("timed out", "timeout"), "transport_timeout", None),
            (("connection", "stream disconnected"), "transport", None),
        ):
            if any(word in lower for word in needles):
                categories.add(category)

        for status, category in ((401, "authentication"), (403, "permission"), (429, "rate_limited")):
            if status in statuses:
                categories.add(category)
        if any(status >= 500 for status in statuses):
            categories.add("server")
        with lock:
            diag["http_statuses"] = sorted(set(diag["http_statuses"]) | statuses)
            diag["error_categories"] = sorted(set(diag["error_categories"]) | categories)
        if statuses & {401, 403, 429} or categories & {"authentication", "permission", "rate_limited"}:
            refused.set()

    def inspect_line(line, stdout):
        if not stdout:
            classify(line.decode("utf-8", errors="replace"))
            return
        try:
            event = json.loads(line)
            kind = event.get("type")
            if kind in _EVENT_TYPES:
                with lock:
                    if kind not in diag["event_types"]:
                        diag["event_types"].append(kind)
                if kind in {"error", "turn.failed"}:
                    error = event.get("error", {})
                    message = event.get("message") or (error.get("message") if isinstance(error, dict) else error)
                    if isinstance(message, str):
                        classify(message)
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass

    def read_output(pipe, stdout):
        total = 0
        pending = b""
        dropping = False
        try:
            # read1 returns available bytes; read(8192) can hide a short event
            # until EOF, precisely when timeout diagnostics are most needed.
            while block := pipe.read1(8192):
                total += len(block)
                if total > MAX_OUTPUT_BYTES:
                    oversized.set()
                    break
                if stdout:
                    chunks.append(block)
                for part in block.splitlines(keepends=True):
                    if not dropping:
                        pending += part
                        if len(pending) > 16384:
                            pending = b""
                            dropping = True
                    if part.endswith((b"\n", b"\r")):
                        if not dropping:
                            inspect_line(pending, stdout)
                        pending = b""
                        dropping = False
            if pending and not dropping:
                inspect_line(pending, stdout)
        except (OSError, ValueError):
            pass

    def send_input():
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=read_output, args=(pipe, stdout), daemon=True)
               for pipe, stdout in ((process.stdout, True), (process.stderr, False))]
    writer = threading.Thread(target=send_input, daemon=True)
    for reader in readers:
        reader.start()
    writer.start()
    deadline = started + seconds
    diag["phase"] = "process_wait"

    def check_limits():
        if refused.is_set():
            raise AuxiliaryModelError("codex_turn_failed")
        if oversized.is_set():
            raise AuxiliaryModelError("codex_output_limit")
        if time.monotonic() >= deadline:
            raise AuxiliaryModelError("timeout")

    try:
        while process.poll() is None:
            check_limits()
            time.sleep(min(.02, max(.001, deadline - time.monotonic())))
        diag["phase"] = "pipe_drain"
        for reader in readers:
            reader.join(timeout=max(.001, deadline - time.monotonic()))
        check_limits()
        if any(reader.is_alive() for reader in readers):
            raise AuxiliaryModelError("timeout")
        diag["phase"] = "complete"
        return process.returncode, b"".join(chunks)
    finally:
        diag["process_state"] = "still_running" if process.poll() is None else "exited"
        diag["exit_code"] = process.poll()
        try:
            _kill_tree(process)
        finally:
            for reader in readers:
                reader.join(timeout=.2)
            writer.join(timeout=.2)
            # Never block closing a pipe held by a surviving reader thread.
            for reader, pipe in zip(readers, (process.stdout, process.stderr)):
                if not reader.is_alive():
                    pipe.close()
            if not writer.is_alive():
                process.stdin.close()
            diag["cleanup_process_state"] = "still_running" if process.poll() is None else "exited"
            diag["cleanup_exit_code"] = process.poll()
            diag["elapsed_seconds"] = round(time.monotonic() - started, 6)


def _prepare_catalog(route: CodexCliRouteConfig, directory: Path, *, seconds: float,
                     diagnostics: dict | None = None) -> Path:
    with route.executable.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != route.executable_sha256 or digest not in VERIFIED_BINARIES:
        raise AuxiliaryModelError("codex_unverified_binary")
    # Bundled catalog is embedded public model metadata, not account/auth data.
    code, raw = _run([str(route.executable), "debug", "models", "--bundled"],
                     cwd=directory, prompt=b"", seconds=seconds, diagnostics=diagnostics)
    try:
        catalog = json.loads(raw)
        model = next(m for m in catalog["models"] if m["slug"] == MODEL)
    except (ValueError, KeyError, TypeError, StopIteration):
        raise AuxiliaryModelError("codex_catalog_invalid") from None
    if code:
        raise AuxiliaryModelError("codex_catalog_invalid")
    model.update(shell_type="disabled", apply_patch_tool_type=None,
                 experimental_supported_tools=[], supports_search_tool=False,
                 tool_mode="direct", node_repl_disabled=True, multi_agent_version="disabled",
                 include_skills_usage_instructions=False, include_plugin_usage_instructions=False,
                 include_apps_usage_instructions=False, model_messages=None,
                 base_instructions="Follow the supplied role/content messages and return only the requested JSON.")
    target = directory / "model-catalog.json"
    target.write_text(json.dumps({"models": [model]}), encoding="utf-8")
    return target


def _prepare_state(route: CodexCliRouteConfig, directory: Path, *, seconds: float,
                   diagnostics: dict | None = None) -> None:
    """Initialize owned native state without importing the user's old rollouts.

    Call after binary verification. This pinned CLI scans CODEX_HOME history on
    every fresh sqlite_home, even for ephemeral exec. Its own no-request server
    initializes that database against an empty home and exits on stdin EOF.
    The model process then reuses this state with its original login home; no
    credentials are read/copied and no upstream state marker is hand-written.
    """
    home = directory / "bootstrap-home"
    home.mkdir()
    environment = _child_env()
    environment["CODEX_HOME"] = str(home)
    command = [str(route.executable), "app-server", "--stdio",
               "-c", "sqlite_home=" + json.dumps(str(directory / "state")),
               "-c", "log_dir=" + json.dumps(str(directory / "logs")),
               "-c", "analytics.enabled=false", "-c", "otel.log_user_prompt=false"]
    for feature in _DISABLED:
        command += ["--disable", feature]
    code, _ = _run(command, cwd=directory, prompt=b"", seconds=min(seconds, 5.0),
                   diagnostics=diagnostics, environment=environment)
    try:
        state_db, = (directory / "state").glob("state_*.sqlite")
        with closing(sqlite3.connect(state_db.as_uri() + "?mode=ro", uri=True)) as db:
            ready = db.execute("SELECT status FROM backfill_state WHERE id=1").fetchone() == ("complete",)
            empty = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    except (OSError, ValueError, sqlite3.Error):
        raise AuxiliaryModelError("codex_state_init_failed") from None
    if code or not ready or not empty:
        raise AuxiliaryModelError("codex_state_init_failed")


def _command(route: CodexCliRouteConfig, directory: Path, catalog: Path) -> list[str]:
    command = [str(route.executable), "exec", "--ignore-user-config", "--ignore-rules",
               "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
               "--json", "--color", "never", "--model", route.model, "--cd", str(directory)]
    overrides = {
        "model_provider": "codex_subscription", "forced_login_method": "chatgpt", "approval_policy": "never",
        "model_catalog_json": str(catalog), "model_reasoning_effort": "low",
        "web_search": "disabled", "mcp_servers": {}, "plugins": {}, "notify": [],
        "tools.update_plan.enabled": False, "tools.experimental_request_user_input.enabled": False,
        "project_doc_max_bytes": 0, "history.persistence": "none",
        "log_dir": str(directory / "logs"), "sqlite_home": str(directory / "state"),
        "otel.log_user_prompt": False, "analytics.enabled": False,
        # Built-in IDs cannot be overridden. This CLI-owned provider uses the
        # normal ChatGPT login/default endpoint; only its retry budget differs.
        "model_providers.codex_subscription.name": "OpenAI",
        "model_providers.codex_subscription.requires_openai_auth": True,
        "model_providers.codex_subscription.wire_api": "responses",
        "model_providers.codex_subscription.request_max_retries": 0,
        "model_providers.codex_subscription.stream_max_retries": 0,
    }
    for key, value in overrides.items():
        # JSON primitives/strings are also TOML; empty mappings need TOML syntax.
        encoded = "{}" if value == {} else json.dumps(value)
        command += ["-c", f"{key}={encoded}"]
    for feature in _DISABLED:
        command += ["--disable", feature]
    return command + ["-"]


def _decode(raw: bytes, exit_code: int) -> tuple[str | None, tuple[int, int] | None, str]:
    text = None
    usage = None
    status = "codex_exit_nonzero" if exit_code else "codex_protocol"
    completed = False
    invalid = False
    try:
        for line in raw.splitlines():
            event = json.loads(line)
            kind = event.get("type")
            if kind == "turn.completed":
                values = event.get("usage", {})
                pair = (values.get("input_tokens"), values.get("output_tokens"))
                if all(type(v) is int and v >= 0 for v in pair):
                    usage = pair
                if completed:
                    invalid = True
                completed = True
            elif kind in {"item.started", "item.updated", "item.completed"}:
                item = event.get("item", {})
                if item.get("type") not in {"agent_message", "reasoning"}:
                    invalid = True
                    status = "codex_tool_event"
                if kind == "item.completed" and item.get("type") == "agent_message":
                    if text is not None or type(item.get("text")) is not str:
                        invalid = True
                    text = item.get("text")
            elif kind in {"error", "turn.failed"}:
                invalid = True
                status = "codex_turn_failed"
            elif kind not in {"thread.started", "turn.started"}:
                invalid = True
    except (ValueError, TypeError, AttributeError):
        invalid = True
    if not exit_code and not invalid and completed and text and usage is not None:
        status = "codex_success"
    return text, usage, status


class CodexCliConsolidationAdapter:
    """One CLI model call per normal worker pass; no retries or model fallback."""

    def __init__(self, route: CodexCliRouteConfig, *, ledger: SubscriptionBudgetLedger):
        self.route = route
        self.ledger = ledger
        self._calls = 0
        self._lock = threading.Lock()
        self.last_diagnostics: dict = {}

    def for_pass(self):
        """A fresh local allowance; durable daily accounting is never reset."""
        adapter = type(self)(self.route, ledger=self.ledger)
        adapter.last_diagnostics = self.last_diagnostics
        return adapter

    def propose(self, messages: list[dict], *, remaining_seconds: float) -> str:
        if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise AuxiliaryModelError("timeout")
        if not isinstance(messages, list) or not messages:
            raise AuxiliaryModelError("input_invalid")
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in {"system", "user", "assistant", "tool"}
                    or type(message["content"]) is not str):
                raise AuxiliaryModelError("input_invalid")
            if contains_secret_like_text(message["content"]):
                raise AuxiliaryModelError("sensitive_request")
        prompt = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(prompt) > self.route.subscription_budget.max_request_bytes:
            raise AuxiliaryModelError("input_invalid")
        with self._lock:
            if self._calls:
                raise AuxiliaryModelError("budget_exhausted")
            self._calls += 1
        deadline = time.monotonic() + min(float(remaining_seconds), 45.0)
        request_id = None
        status = "codex_start_failed"
        usage = None
        result = None
        pending = None
        self.last_diagnostics.clear()
        self.last_diagnostics.update(catalog={}, bootstrap={}, model={})
        try:
            with tempfile.TemporaryDirectory(prefix="scope-recall-codex-") as temporary:
                directory = Path(temporary)
                catalog = _prepare_catalog(self.route, directory, seconds=max(.001, deadline - time.monotonic() - 4),
                                           diagnostics=self.last_diagnostics["catalog"])
                _prepare_state(self.route, directory, seconds=deadline - time.monotonic() - 4,
                               diagnostics=self.last_diagnostics["bootstrap"])
                request_id = self.ledger.reserve(self.route.model, timeout_seconds=deadline - time.monotonic())
                code, raw = _run(_command(self.route, directory, catalog), cwd=directory,
                                 prompt=prompt, seconds=deadline - time.monotonic() - 4,
                                 diagnostics=self.last_diagnostics["model"])
                result, usage, status = _decode(raw, code)
                if status != "codex_success":
                    raise AuxiliaryModelError(status)
        except AuxiliaryModelError as exc:
            pending = exc
            status = exc.error_type
        except (ValueError, sqlite3.Error) as exc:
            kind = "budget_exhausted" if str(exc) == "budget_exhausted_or_meter_breach" else "budget_unavailable"
            pending = AuxiliaryModelError(kind)
        except (OSError, subprocess.SubprocessError):
            pending = AuxiliaryModelError("codex_start_failed")
        finally:
            if request_id is not None:
                try:
                    breached = self.ledger.finish(request_id, status, usage,
                                                   timeout_seconds=max(.001, deadline - time.monotonic()))
                    if breached:
                        pending = AuxiliaryModelError("meter_breach")
                except (ValueError, sqlite3.Error, OSError):
                    # Committed reservation survives; do not call this zero use.
                    pending = AuxiliaryModelError("budget_unavailable")
        if pending is not None:
            raise pending
        assert isinstance(result, str)
        return result
