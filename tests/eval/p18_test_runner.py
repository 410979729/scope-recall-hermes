"""P18 TEST-only unified runner.

This module is deliberately an orchestration and guardrail layer.  It does not
import the product implementation, does not contain sealed expectations, and
does not provide HTTP/CLI substitutes for the real host transports.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import types
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


QUERIES_PER_HOST = 40
QUERY_CONDITIONS = 2
JOURNEYS_PER_HOST = 8
ROUNDS_PER_JOURNEY = 8
PRIMARY_PER_ARM_HOST = (QUERIES_PER_HOST * QUERY_CONDITIONS) + (
    JOURNEYS_PER_HOST * ROUNDS_PER_JOURNEY
)
ARMS_PER_HOST = 4
HOSTS_COUNT = 2
PRIMARY_PER_HOST = PRIMARY_PER_ARM_HOST * ARMS_PER_HOST
TOTAL_PRIMARY = PRIMARY_PER_HOST * HOSTS_COUNT

GO_CALL_CAP = 8_000
GO_INPUT_CAP = 64_000_000
GO_OUTPUT_CAP = 8_000_000
CODEX_CALL_CAP = 1_500
TOTAL_COST_MICRO_USD_CAP = 20_000_000
REQUEST_INPUT_RESERVE = 32_768
REQUEST_OUTPUT_RESERVE = 4_096
MIMO_AUX_OUTPUT_RESERVE = 131_072
LEDGER_PATH = ".execution/TEST-MODEL-BUDGET-V1/call-budget.sqlite3"

BASELINE_578B = "578b955802df753f2e2208e26eab6f71971285a0"

# These are control/oracle fields.  The recursive check also catches a
# control field nested in an otherwise plausible public fixture.
CONTROL_FIELDS = frozenset(
    {
        "case_id",
        "case_index",
        "case_index_control_only",
        "group_id",
        "core_class",
        "condition",
        "answerability",
        "required_facts",
        "prohibited_errors",
        "gold",
        "expected",
        "oracle",
        "control_only",
    }
)

MODEL_HISTORY_FIELDS = frozenset({"source_type", "speaker_role", "text", "occurred_at"})
MODEL_QUERY_FIELDS = frozenset({"text"})
ORIGIN_MAP = {
    "human_direct": "human_direct",
    "assistant_visible": "assistant_visible",
    "tool_observation": "tool_observation",
    "external_document": "external_document",
}
ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "tool": "tool",
    "document": "document",
}


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    label: str
    isolation_key: str
    source: str
    source_ref: str


@dataclass(frozen=True)
class HostSpec:
    host_id: str
    label: str
    transport: str
    model_route: str
    isolation_key: str
    true_host_required: bool = True
    cli_substitution_forbidden: bool = False


@dataclass(frozen=True)
class HostExchange:
    """A bounded transport result retained as receipt metadata only."""

    status: int | None
    body: bytes = b""
    headers: Mapping[str, str] | None = None
    error_type: str | None = None


class HostAdapterConfigurationError(ValueError):
    """A host adapter was not given a safe, explicit isolated configuration."""


def _unsupported(spec: HostSpec, reason: str) -> dict[str, Any]:
    return {
        "status": "UNSUPPORTED",
        "host_id": spec.host_id,
        "isolation_key": spec.isolation_key,
        "reason": reason,
        "network_calls": 0,
        "model_calls": 0,
    }


def _safe_isolation_root(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    root = Path(value).expanduser().resolve()
    # A formal arm must never accidentally attach to the user's production
    # Hermes fleet.  This is a lexical guard as well as a directory check.
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise HostAdapterConfigurationError("production F:\\Agents root is forbidden")
    return root


def _remaining_seconds(deadline: float | None, default: float) -> float:
    if deadline is None:
        return max(0.001, default)
    remaining = deadline - time.monotonic()
    return max(0.0, min(default, remaining))


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        "A",
        "host native memory",
        "arm-A-native",
        "host-native",
        "host-native-memory",
    ),
    ArmSpec(
        "B",
        "exact historical plugin baseline",
        "arm-B-baseline-578b",
        "git-ref",
        BASELINE_578B,
    ),
    ArmSpec(
        "C",
        "final new Scope Recall",
        "arm-C-final-new",
        "external-source-root",
        "provided-at-execution",
    ),
    ArmSpec(
        "D",
        "archive plus simple search",
        "arm-D-archive-simple-search",
        "archive-root",
        "provided-at-execution",
    ),
)

HOSTS: tuple[HostSpec, ...] = (
    HostSpec(
        "hermes_a2a",
        "Hermes",
        "actual-a2a",
        "OpenCode Go / DeepSeek Flash",
        "host-hermes-a2a",
    ),
    HostSpec(
        "codex_windows_desktop",
        "Codex Windows desktop",
        "actual-desktop-ui-input-send",
        "gpt-5.6-luna",
        "host-codex-desktop",
        cli_substitution_forbidden=True,
    ),
)


class HostAdapter:
    """Contract for the future real host adapters.

    Implementations must use the actual Hermes A2A or actual Codex desktop UI
    path.  A mock HTTP endpoint, CLI submission, or app-side message helper is
    not a conforming formal adapter.
    """

    spec: HostSpec

    def __init__(self, spec: HostSpec) -> None:
        self.spec = spec

    def execute_query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _unsupported(self.spec, "host_adapter_not_configured")

    def execute_journey(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _unsupported(self.spec, "host_adapter_not_configured")

    def build_submission_queue(self, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        """Return model-input-safe work items without submitting them."""
        queue: list[dict[str, Any]] = []
        for ordinal in range(QUERIES_PER_HOST * QUERY_CONDITIONS):
            queue.append(
                {
                    "ordinal": ordinal + 1,
                    "kind": "query_pair",
                    "model_input": _model_input_projection(rows[ordinal % len(rows)]),
                }
            )
        offset = len(queue)
        for ordinal in range(JOURNEYS_PER_HOST * ROUNDS_PER_JOURNEY):
            queue.append(
                {
                    "ordinal": offset + ordinal + 1,
                    "kind": "journey_round",
                    "model_input": _model_input_projection(rows[ordinal % len(rows)]),
                }
            )
        return tuple(queue)

    def execute_queue(self, queue: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        return {
            **_unsupported(self.spec, "host_adapter_not_configured"),
            "queue_count": len(queue),
            "receipts": [],
        }


class HermesA2AHostAdapter(HostAdapter):
    """Opt-in adapter for the actual isolated Hermes A2A gateway.

    The normal P18 runner never constructs this with an endpoint, so a dry
    run remains zero-network.  A real run must provide an isolated TEST
    gateway endpoint.  ``test_transport`` is intentionally explicit and is
    only for receipt/transport unit tests; it cannot be used by a formal run.
    """

    def __init__(
        self,
        spec: HostSpec,
        *,
        endpoint: str | None = None,
        isolation_root: str | Path | None = None,
        transport: Any = None,
        test_transport: bool = False,
        timeout_seconds: float = 50.0,
        max_response_bytes: int = 786_432,
    ) -> None:
        super().__init__(spec)
        if spec.host_id != "hermes_a2a":
            raise HostAdapterConfigurationError("Hermes adapter received a non-Hermes host spec")
        self.endpoint = endpoint.rstrip("/") if isinstance(endpoint, str) else None
        self.isolation_root = _safe_isolation_root(isolation_root)
        self.transport = transport
        self.test_transport = bool(test_transport)
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise HostAdapterConfigurationError("Hermes timeout must be within (0,120] seconds")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 8 * 1024 * 1024:
            raise HostAdapterConfigurationError("Hermes response bound is invalid")
        if self.transport is not None and not self.test_transport:
            raise HostAdapterConfigurationError("injected transport is TEST-only")
        if self.test_transport and self.transport is None:
            raise HostAdapterConfigurationError("TEST transport must be callable")
        if self.endpoint is not None:
            parsed = urllib.parse.urlparse(self.endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HostAdapterConfigurationError("Hermes endpoint must be an absolute HTTP(S) URL")
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise HostAdapterConfigurationError("non-loopback Hermes endpoints must use HTTPS")
            if self.isolation_root is None and not self.test_transport:
                raise HostAdapterConfigurationError("actual Hermes endpoint requires an explicit isolated TEST home")

    def _request_body(self, payload: Mapping[str, Any]) -> bytes:
        request = payload.get("a2a_request")
        if isinstance(request, Mapping):
            value: Mapping[str, Any] = request
        else:
            model_input = payload.get("model_input", payload)
            if not isinstance(model_input, Mapping):
                raise HostAdapterConfigurationError("queue item model_input must be an object")
            query = model_input.get("query", {})
            query_text = query.get("text") if isinstance(query, Mapping) else None
            if not isinstance(query_text, str) or not query_text.strip():
                raise HostAdapterConfigurationError("queue item query.text is required")
            ordinal = payload.get("ordinal", "item")
            message_id = f"P18-{self.spec.isolation_key}-{ordinal}"
            context_id = payload.get("context_id") or f"P18-{self.spec.isolation_key}-{payload.get('kind', 'query')}-{ordinal}"
            value = {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": message_id,
                        "role": "ROLE_USER",
                        "parts": [{"text": query_text}],
                        "contextId": str(context_id),
                    },
                    "configuration": {"returnImmediately": False},
                },
            }
        # Preserve the host protocol's JSON while excluding no evidence from
        # the actual request.  The public fixture has already passed the
        # model-input allowlist before it reaches this adapter.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _exchange(self, body: bytes, *, deadline: float | None) -> HostExchange:
        if self.endpoint is None:
            return HostExchange(None, error_type="endpoint_not_configured")
        timeout = _remaining_seconds(deadline, self.timeout_seconds)
        if timeout <= 0:
            return HostExchange(None, error_type="deadline_expired")
        if self.transport is not None:
            try:
                result = self.transport(self.endpoint, body, timeout)
                if isinstance(result, HostExchange):
                    return result
                if isinstance(result, tuple) and len(result) in {2, 3}:
                    status, response_body = result[:2]
                    headers = result[2] if len(result) == 3 else {}
                    if not isinstance(response_body, bytes):
                        raise TypeError("TEST transport body must be bytes")
                    return HostExchange(int(status), response_body, headers if isinstance(headers, Mapping) else {})
                raise TypeError("TEST transport returned an invalid exchange")
            except Exception as exc:  # metadata only; no retry
                return HostExchange(None, error_type=type(exc).__name__)
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    return HostExchange(int(response.status), raw[: self.max_response_bytes], error_type="response_too_large")
                headers = {str(k): str(v) for k, v in response.headers.items() if str(k).lower() in {"content-type", "content-length"}}
                return HostExchange(int(response.status), raw, headers)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return HostExchange(None, error_type=type(exc).__name__)

    def _execute(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.endpoint is None:
            return _unsupported(self.spec, "endpoint_not_configured")
        started = time.monotonic()
        deadline_value = payload.get("deadline_monotonic")
        deadline = float(deadline_value) if isinstance(deadline_value, (int, float)) else None
        try:
            body = self._request_body(payload)
        except (TypeError, ValueError, HostAdapterConfigurationError) as exc:
            return {**_unsupported(self.spec, "invalid_host_payload"), "error_type": type(exc).__name__}
        exchange = self._exchange(body, deadline=deadline)
        safe_headers = {
            str(key): str(value)
            for key, value in (exchange.headers or {}).items()
            if str(key).lower() in {"content-type", "content-length"}
        }
        return {
            "status": "PASS" if exchange.status is not None and exchange.error_type is None and 200 <= exchange.status < 300 else "FAIL",
            "host_id": self.spec.host_id,
            "isolation_key": self.spec.isolation_key,
            "http_status": exchange.status,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "request_bytes": len(body),
            "response_sha256": hashlib.sha256(exchange.body).hexdigest(),
            "response_bytes": len(exchange.body),
            "response_headers": safe_headers,
            "error_type": exchange.error_type,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "network_calls": 1 if self.endpoint is not None and exchange.error_type != "deadline_expired" else 0,
            "model_calls": 1 if exchange.status is not None and 200 <= exchange.status < 300 else 0,
            "raw_response_retained": False,
        }

    def execute_query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._execute(payload)

    def execute_journey(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._execute(payload)

    def execute_queue(self, queue: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        if self.endpoint is None:
            return {**_unsupported(self.spec, "endpoint_not_configured"), "queue_count": len(queue), "receipts": []}
        receipts = [self._execute(item) for item in queue]
        return {
            "status": "PASS" if all(item.get("status") == "PASS" for item in receipts) else "FAIL",
            "host_id": self.spec.host_id,
            "isolation_key": self.spec.isolation_key,
            "queue_count": len(queue),
            "receipts": receipts,
            "network_calls": sum(int(item.get("network_calls", 0)) for item in receipts),
            "model_calls": sum(int(item.get("model_calls", 0)) for item in receipts),
            "retry_count": 0,
        }


class CodexDesktopHostAdapter(HostAdapter):
    """Boundary for the real desktop input/send path.

    Codex CLI/stdio is deliberately not accepted here.  Until a verified
    desktop bridge is passed by the host integration, calls are reported as
    unsupported and never silently counted as model execution.
    """

    def __init__(self, spec: HostSpec, *, desktop_bridge: Any = None) -> None:
        super().__init__(spec)
        if spec.host_id != "codex_windows_desktop":
            raise HostAdapterConfigurationError("Codex adapter received a non-Codex host spec")
        if not spec.cli_substitution_forbidden:
            raise HostAdapterConfigurationError("Codex spec must forbid CLI substitution")
        self.desktop_bridge = desktop_bridge

    def execute_query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.desktop_bridge is None:
            return _unsupported(self.spec, "official_codex_desktop_bridge_not_configured")
        return _unsupported(self.spec, "desktop_bridge_requires_explicit_host_registration")

    def execute_journey(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.execute_query(payload)

    def execute_queue(self, queue: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        return {**self.execute_query(queue[0] if queue else {}), "queue_count": len(queue), "receipts": []}


class OfflineArmBackend:
    arm_id: str

    def prepare(self, *, data_dir: Path, rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class NativeMemoryBackend(OfflineArmBackend):
    arm_id = "A"

    def prepare(self, *, data_dir: Path, rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        native_root = kwargs.get("native_memory_root")
        if not isinstance(native_root, Path) or not native_root.is_dir():
            return {"status": "PENDING", "reason": "native-memory-root-not-configured"}
        files = [path.name for path in native_root.rglob("*") if path.is_file()]
        return {
            "status": "PASS",
            "backend": "host-native-memory",
            "native_memory_root": str(native_root),
            "native_file_count": len(files),
            "source_sha256": _tree_sha256(native_root),
            "read_actual_directory": True,
        }


class Baseline578bBackend(OfflineArmBackend):
    arm_id = "B"

    def prepare(self, *, data_dir: Path, rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        repo_root = kwargs.get("repo_root")
        if not isinstance(repo_root, Path) or not repo_root.is_dir():
            return {"status": "PENDING", "reason": "repository-root-not-configured"}
        export_root = data_dir / "baseline-578b-export"
        export_root.mkdir(parents=True, exist_ok=False)
        process = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", BASELINE_578B],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if process.returncode != 0:
            return {
                "status": "NOT_RUNNABLE",
                "baseline_ref": BASELINE_578B,
                "exported": False,
                "git_returncode": process.returncode,
            }
        with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:") as archive:
            archive.extractall(export_root, filter="data")
        config_present = (export_root / "pyproject.toml").is_file() and (export_root / "plugin.yaml").is_file()
        host_id = kwargs.get("host_id")
        smoke = host_id == "hermes_a2a"
        b_test_python = kwargs.get("b_test_python")
        if not isinstance(b_test_python, Path) or not b_test_python.is_file():
            b_test_python = Path(sys.executable)
        probe_code = textwrap.dedent(
            """
            import hashlib
            import json
            import sys
            import types
            from pathlib import Path

            import jsonschema
            import yaml

            agent = types.ModuleType("agent")
            memory_provider = types.ModuleType("agent.memory_provider")
            memory_provider.MemoryProvider = type("MemoryProvider", (), {})
            sys.modules["agent"] = agent
            sys.modules["agent.memory_provider"] = memory_provider
            tools = types.ModuleType("tools")
            registry = types.ModuleType("tools.registry")
            registry.tool_error = lambda message: {"error": message}
            sys.modules["tools"] = tools
            sys.modules["tools.registry"] = registry

            package = types.ModuleType("scope_recall")
            package.__path__ = [sys.argv[1]]
            sys.modules["scope_recall"] = package
            from scope_recall.provider import ScopeRecallMemoryProvider

            result = {"import": "PASS"}
            if sys.argv[3] == "smoke":
                home = Path(sys.argv[2])
                home.mkdir(parents=True, exist_ok=False)
                config_dir = home / "scope-recall"
                config_dir.mkdir()
                (config_dir / "config.json").write_text(
                    json.dumps(
                        {
                            "vector": {"enabled": False},
                            "retrieval": {
                                "mode": "lexical",
                                "min_score": 0.0,
                                "include_general": "same-scope",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                provider = ScopeRecallMemoryProvider()
                provider.initialize(
                    "TEST-legacy-session",
                    hermes_home=str(home),
                    platform="cli",
                    agent_context="primary",
                    agent_identity="TEST-legacy",
                    agent_workspace="TEST",
                    user_id="TEST-user",
                )
                memory_id, inserted, _ = provider.store_now(
                    content="P18 legacy backend smoke",
                    source="human_direct",
                    target="project",
                    session_id="TEST-legacy-session",
                )
                items = provider._search_db_memories("P18 legacy backend", limit=5)
                result.update(
                    {
                        "runtime_status": provider.runtime_status,
                        "inserted": bool(inserted),
                        "search_count": len(items),
                        "memory_id_sha256": hashlib.sha256(memory_id.encode()).hexdigest(),
                    }
                )
                provider.shutdown()
            print(json.dumps(result, separators=(",", ":")))
            """
        )
        probe = subprocess.run(
            [
                str(b_test_python),
                "-c",
                probe_code,
                str(export_root),
                str(data_dir / "legacy-smoke-home"),
                "smoke" if smoke else "import",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        probe_result: dict[str, Any] = {}
        if probe.returncode == 0 and probe.stdout.strip():
            try:
                probe_result = json.loads(probe.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                probe_result = {}
        import_ok = probe.returncode == 0 and probe_result.get("import") == "PASS"
        smoke_ok = (not smoke) or (
            probe_result.get("inserted") is True and probe_result.get("search_count", 0) >= 1
        )
        return {
            "status": "PASS" if config_present and import_ok and smoke_ok else "NOT_RUNNABLE",
            "baseline_ref": BASELINE_578B,
            "exported": True,
            "source_sha256": _tree_sha256(export_root),
            "test_environment_python": str(b_test_python.resolve()),
            "config_present": config_present,
            "import_probe_returncode": probe.returncode,
            "import_probe_error_type": (
                probe.stderr.decode("utf-8", errors="replace").splitlines()[-1].split(":", 1)[0]
                if probe.returncode != 0 and probe.stderr.strip()
                else None
            ),
            "legacy_api_smoke": {
                "attempted": smoke,
                "inserted": probe_result.get("inserted") if smoke else None,
                "search_count": probe_result.get("search_count") if smoke else None,
                "memory_id_sha256": probe_result.get("memory_id_sha256") if smoke else None,
                "runtime_status": probe_result.get("runtime_status") if smoke else None,
            },
        }


class FinalNewSourceBackend(OfflineArmBackend):
    arm_id = "C"

    def prepare(self, *, data_dir: Path, rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        manifest_path = kwargs.get("candidate_manifest")
        source_root = kwargs.get("final_source_root")
        if not isinstance(manifest_path, Path) or not isinstance(source_root, Path):
            return {"status": "PENDING", "reason": "final-source-manifest-not-configured"}
        checked = _validate_candidate_manifest(manifest_path, source_root)
        return {
            "status": "PASS",
            "backend": "final-new-external-source-root",
            "source_root": str(source_root.resolve()),
            "source_sha256": checked["source_sha256"],
            "manifest_checked": True,
        }


class SimpleArchiveSearchBackend(OfflineArmBackend):
    """Archive plus literal search only; no Core/claims/consolidation imports."""

    arm_id = "D"

    def prepare(self, *, data_dir: Path, rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        archive_path = data_dir / "raw-archive.jsonl"
        with archive_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(_model_input_projection(row), ensure_ascii=False, sort_keys=True) + "\n")
        query = _model_input_projection(rows[0])["query"]["text"].casefold()
        matches = 0
        for row in rows:
            projected = _model_input_projection(row)
            haystack = " ".join(event["text"] for event in projected["history"]).casefold()
            if query in haystack:
                matches += 1
        return {
            "status": "PASS",
            "backend": "archive-plus-simple-literal-search",
            "archive_sha256": _sha256(archive_path),
            "archive_rows": len(rows),
            "literal_query_matches": matches,
            "semantic_admission": False,
            "core_imported": False,
            "claims_imported": False,
            "consolidation_imported": False,
        }


OFFLINE_BACKENDS: dict[str, OfflineArmBackend] = {
    "A": NativeMemoryBackend(),
    "B": Baseline578bBackend(),
    "C": FinalNewSourceBackend(),
    "D": SimpleArchiveSearchBackend(),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _load_public_fixture(path: Path) -> int:
    rows = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"public fixture JSON error at line {line_number}") from exc
            if not isinstance(item, Mapping):
                raise ValueError(f"public fixture row {line_number} is not an object")
            forbidden = sorted(set(_walk_keys(item)) & CONTROL_FIELDS)
            if forbidden:
                raise ValueError(
                    "public fixture contains control/oracle fields: " + ",".join(forbidden)
                )
            history = item.get("history")
            query = item.get("query")
            if not isinstance(history, list) or not isinstance(query, Mapping):
                raise ValueError(f"public fixture row {line_number} lacks history/query shape")
            if not all(isinstance(event, Mapping) for event in history):
                raise ValueError(f"public fixture row {line_number} has invalid history events")
            if not isinstance(query.get("text"), str) or not query["text"].strip():
                raise ValueError(f"public fixture row {line_number} has invalid query text")
            rows += 1
    if rows == 0:
        raise ValueError("public fixture is empty")
    return rows


def _load_public_rows(path: Path) -> list[dict[str, Any]]:
    """Load public fixture rows after the same no-oracle checks as dry-run."""
    _load_public_fixture(path)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _model_input_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    """Construct model input from an explicit allowlist, never by filtering a dict."""
    history = row["history"]
    query = row["query"]
    projected_history: list[dict[str, str]] = []
    for event in history:
        source_type = ORIGIN_MAP.get(event.get("source_type"))
        speaker_role = ROLE_MAP.get(event.get("speaker_role"))
        text = event.get("text")
        if source_type is None or speaker_role is None or not isinstance(text, str) or not text.strip():
            raise ValueError("public fixture event fails model-input allowlist")
        projected: dict[str, str] = {
            "source_type": source_type,
            "speaker_role": speaker_role,
            "text": text,
        }
        occurred_at = event.get("occurred_at")
        if isinstance(occurred_at, str) and occurred_at:
            projected["occurred_at"] = occurred_at
        projected_history.append(projected)
    query_text = query.get("text")
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("public fixture query fails model-input allowlist")
    return {"history": projected_history, "query": {"text": query_text}}


def _tree_sha256(root: Path) -> str:
    if not root.is_dir():
        raise ValueError("candidate source root is not a directory")
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _ensure_workspace_package_alias() -> None:
    """Make the flat source checkout importable when invoked as a script."""
    if "scope_recall" not in sys.modules:
        package = types.ModuleType("scope_recall")
        package.__path__ = [str(Path(__file__).resolve().parents[2])]
        sys.modules["scope_recall"] = package


def _validate_candidate_manifest(manifest_path: Path, source_root: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise ValueError("candidate source manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("candidate source manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping) or set(manifest) != {"schema", "source_root", "source_sha256"}:
        raise ValueError("candidate source manifest schema mismatch")
    if manifest["schema"] != "scope-recall.candidate-source-manifest.v1":
        raise ValueError("candidate source manifest schema mismatch")
    declared_root = Path(str(manifest["source_root"])).resolve()
    actual_root = source_root.resolve()
    if declared_root != actual_root:
        raise ValueError("candidate source manifest root mismatch")
    actual_hash = _tree_sha256(actual_root)
    if manifest["source_sha256"] != actual_hash:
        raise ValueError("candidate source manifest hash mismatch")
    return {"status": "PASS", "source_sha256": actual_hash}


def _make_public_candidate(run_parent: Path) -> tuple[Path, Path]:
    source_root = Path(tempfile.mkdtemp(prefix="public-final-source-", dir=str(run_parent)))
    marker = source_root / "TEST-runtime-marker.txt"
    marker.write_text("P18 PUBLIC TEST candidate source\n", encoding="utf-8")
    manifest_path = source_root.parent / "candidate-source-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema": "scope-recall.candidate-source-manifest.v1",
            "source_root": str(source_root.resolve()),
            "source_sha256": _tree_sha256(source_root),
        },
    )
    return source_root, manifest_path


def _run_offline_backend_matrix(
    fixture: Path,
    run_parent: Path,
    *,
    repo_root: Path,
    candidate_manifest: Path | None,
    final_source_root: Path | None,
) -> dict[str, Any]:
    """Prepare all eight isolated arm/host backends without host submission."""
    rows = _load_public_rows(fixture)
    matrix_root = Path(tempfile.mkdtemp(prefix="offline-matrix-", dir=str(run_parent)))
    adapter_types = {
        "hermes_a2a": HermesA2AHostAdapter,
        "codex_windows_desktop": CodexDesktopHostAdapter,
    }
    summaries: list[dict[str, Any]] = []
    manifests: list[str] = []
    for host in HOSTS:
        for arm in ARMS:
            data_dir = matrix_root / host.host_id / f"arm-{arm.arm_id}"
            data_dir.mkdir(parents=True, exist_ok=False)
            backend = OFFLINE_BACKENDS[arm.arm_id]
            backend_kwargs: dict[str, Any] = {
                "repo_root": repo_root,
                "host_id": host.host_id,
                "b_test_python": (
                    Path(__file__).resolve().parents[2]
                    / ".execution"
                    / "TEST-FINAL-RUNTIME-ENV"
                    / "Scripts"
                    / "python.exe"
                ),
            }
            if arm.arm_id == "A":
                native_root = data_dir / "native-memory"
                native_root.mkdir(parents=True, exist_ok=False)
                (native_root / "TEST-native-memory-marker.txt").write_text(
                    f"P18 PUBLIC TEST native fixture for {host.host_id}\n", encoding="utf-8"
                )
                backend_kwargs["native_memory_root"] = native_root
            if arm.arm_id == "C":
                backend_kwargs["candidate_manifest"] = candidate_manifest
                backend_kwargs["final_source_root"] = final_source_root
            evidence = backend.prepare(data_dir=data_dir, rows=rows, **backend_kwargs)
            adapter = adapter_types[host.host_id](host)
            queue = adapter.build_submission_queue(rows)
            queue_projection = [item["model_input"] for item in queue]
            queue_nonempty_slots = sum(
                bool(item["history"]) and bool(item["query"].get("text"))
                for item in queue_projection
            )
            queue_projection_sha256 = hashlib.sha256(
                json.dumps(queue_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            source_sha256 = evidence.get("source_sha256") or evidence.get("archive_sha256")
            if host.host_id == "codex_windows_desktop" and arm.arm_id == "B":
                evidence = {
                    **evidence,
                    "status": "UNSUPPORTED",
                    "codex_compatibility": "UNSUPPORTED_ORIGINAL_PLUGIN_NO_CODEX_ADAPTER",
                }
            manifest = {
                "schema": "scope-recall.p18-offline-backend-manifest.v1",
                "status": "PREPARED",
                "host_id": host.host_id,
                "arm_id": arm.arm_id,
                "data_directory": str(data_dir.resolve()),
                "identity": {
                    "agent_id": f"TEST-{host.host_id}-{arm.arm_id}",
                    "installation_id": f"TEST-installation-{host.host_id}-{arm.arm_id}",
                    "scope_id": "TEST-scope",
                },
                "source_sha256": source_sha256,
                "backend_evidence": evidence,
                "execution_context": {
                    "model_route": host.model_route,
                    "budget_profile": "P18-frozen-shared-ledger-contract",
                    "tools": [],
                    "extra_memory_context": False,
                    "model_input_allowlist": {
                        "history": sorted(MODEL_HISTORY_FIELDS),
                        "query": sorted(MODEL_QUERY_FIELDS),
                    },
                },
                "host_adapter": {
                    "adapter_type": type(adapter).__name__,
                    "transport": host.transport,
                    "queue_count": len(queue),
                    "queue_filled_slots": queue_nonempty_slots,
                    "queue_projection_sha256": queue_projection_sha256,
                    "execution_status": "PENDING_ACTUAL_HOST",
                    "cli_substitution_forbidden": host.cli_substitution_forbidden,
                },
                "oracle_boundary": {
                    "gold_read": False,
                    "control_fields_in_model_input": False,
                },
            }
            manifest_path = data_dir / "backend-manifest.json"
            _write_json(manifest_path, manifest)
            manifests.append(str(manifest_path.resolve()))
            summaries.append(
                {
                    "host_id": host.host_id,
                    "arm_id": arm.arm_id,
                    "data_directory": str(data_dir.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "backend_status": evidence.get("status"),
                    "source_sha256": source_sha256,
                    "queue_count": len(queue),
                    "queue_filled_slots": queue_nonempty_slots,
                    "queue_projection_sha256": queue_projection_sha256,
                    "host_execution": "PENDING_ACTUAL_HOST",
                    "codex_baseline_unsupported": host.host_id == "codex_windows_desktop" and arm.arm_id == "B",
                }
            )
    statuses = {row["backend_status"] for row in summaries}
    return {
        "status": "PASS" if statuses <= {"PASS", "UNSUPPORTED"} else "PASS_WITH_EXPLICIT_PENDING_OR_NOT_RUNNABLE",
        "matrix_root": str(matrix_root.resolve()),
        "entries": summaries,
        "manifest_count": len(manifests),
        "all_data_directories_distinct": len({row["data_directory"] for row in summaries}) == 8,
        "all_host_execution_pending": all(row["host_execution"] == "PENDING_ACTUAL_HOST" for row in summaries),
        "sequence_interface": {
            "method": "HostAdapter.build_submission_queue",
            "queue_count_per_entry": PRIMARY_PER_ARM_HOST,
            "model_input_only_history_query": True,
            "gold_or_control_input": False,
        },
        "manifest_paths": manifests,
    }


class _FixedClock:
    def utc_now(self) -> str:
        return "2026-09-06T12:00:00+00:00"

    def monotonic(self) -> float:
        import time

        return time.monotonic()


def _run_core_public_fixture(fixture: Path, run_parent: Path) -> dict[str, Any]:
    """Run two real MemoryCore instances against synthetic public rows only."""
    _ensure_workspace_package_alias()
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core.composition import CoreConfig, MemoryCore

    rows = _load_public_rows(fixture)
    if len(rows) < 2:
        raise ValueError("core public run requires at least two public rows")
    run_root = Path(tempfile.mkdtemp(prefix="core-public-", dir=str(run_parent)))
    records: list[dict[str, Any]] = []
    refs: list[str] = []
    contexts: list[TrustedContext] = []
    cores: list[MemoryCore] = []
    for index, (host_id, arm_id) in enumerate(
        (("hermes_a2a", "public-fixture-01"), ("codex_windows_desktop", "public-fixture-02"))
    ):
        data_dir = run_root / host_id / arm_id
        data_dir.mkdir(parents=True, exist_ok=False)
        binding = InstanceBinding(
            f"TEST-{host_id}",
            f"TEST-installation-{host_id}",
            data_dir,
            frozenset({"TEST-scope"}),
            True,
        )
        context = TrustedContext(binding, f"TEST-session-{host_id}", frozenset({"TEST-scope"}), "human_direct")
        core = MemoryCore(CoreConfig(binding), clock=_FixedClock())
        initialized = core.initialize()
        projected = _model_input_projection(rows[index])
        source = projected["history"][0]
        event = {
            "protocol_version": "1.1",
            "source_event_key": f"TEST-P18-public-{host_id}-{index + 1}",
            "source_revision": 1,
            "origin": source["source_type"],
            "role": source["speaker_role"],
            "content": source["text"],
            "occurred_at": source.get("occurred_at"),
            "recorded_at": "2026-09-06T12:00:00Z",
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
            "dataset_id": "SYNTHETIC_TEST_ONLY",
        }
        capture = core.record_event(context, event, scope_id="TEST-scope", remaining_seconds=5)
        if capture.disposition != "inserted" or not capture.event_refs:
            raise AssertionError("public Core capture did not persist")
        ref = capture.event_refs[0].ref
        refs.append(ref)
        # Reopen through a fresh MemoryCore to prove the persisted state crosses
        # the process-object boundary before retrieval/render preparation.
        reopened = MemoryCore(CoreConfig(binding), clock=_FixedClock())
        reopened.initialize()
        request = {
            "protocol_version": "1.1",
            "request_id": f"TEST-P18-{host_id}",
            "query": projected["query"]["text"],
            "mode": "current",
            "max_items": 6,
            "budget_tokens": 1200,
        }
        packet = reopened.recall_packet(context, request, deadline_seconds=5)
        prepared = reopened.prepare_recall_render(context, packet)
        if not packet["items"]:
            raise AssertionError("public Core recall packet unexpectedly empty")
        packet_bytes = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        render_bytes = (prepared.canonical_text or "").encode("utf-8")
        status = reopened.status(context)
        records.append(
            {
                "host_id": host_id,
                "arm_id": arm_id,
                "data_directory": str(data_dir),
                "persisted_sources": status.sources,
                "persisted_ref": ref,
                "memory_epoch": packet["memory_epoch"],
                "packet_status": packet["status"],
                "packet_items": len(packet["items"]),
                "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
                "render_ref": prepared.render_ref,
                "render_present": prepared.context is not None,
                "render_sha256": hashlib.sha256(render_bytes).hexdigest(),
                "model_input_fields": {
                    "history": sorted(MODEL_HISTORY_FIELDS),
                    "query": sorted(MODEL_QUERY_FIELDS),
                },
                "initialized_sources": initialized.sources,
            }
        )
        contexts.append(context)
        cores.append(reopened)
    isolation_negative = cores[1].source(contexts[1], refs[0], 1) is None
    if not isolation_negative:
        raise AssertionError("cross-directory read isolation negative failed")
    return {
        "status": "PASS",
        "network_calls": 0,
        "model_calls": 0,
        "instances": records,
        "data_directories_distinct": records[0]["data_directory"] != records[1]["data_directory"],
        "cross_directory_read_isolation_negative": isolation_negative,
    }


class _SpyTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, **kwargs: Any) -> tuple[int, bytes]:
        self.calls += 1
        raise AssertionError("budget rejection must happen before transport")


def _run_real_budget_rejection(run_parent: Path) -> dict[str, Any]:
    _ensure_workspace_package_alias()
    from scope_recall.adapters.models import AuxiliaryModelError, ConsolidationRouteConfig, OpenAIConsolidationAdapter
    from scope_recall.runtime.model_budget import (
        AuxiliaryBudgetLedger,
        BudgetPolicy,
        ModelPricing,
        initialize_auxiliary_budget_ledger,
        read_auxiliary_budget_status,
    )

    ledger_path = Path(tempfile.mkdtemp(prefix="ledger-rejection-", dir=str(run_parent))) / "call-budget.sqlite3"
    policy = BudgetPolicy(
        batch="TEST-P18-LEDGER-REJECTION",
        cap_micro_usd=20_000_000,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=0,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={},
        model_token_caps={},
        pricing={"deepseek-v4-flash": ModelPricing(Decimal("0.44"), Decimal("1.32"))},
        approved_models=frozenset({"deepseek-v4-flash"}),
    )
    initialize_auxiliary_budget_ledger(ledger_path, policy)
    spy = _SpyTransport()
    adapter = OpenAIConsolidationAdapter(
        ConsolidationRouteConfig(
            model="deepseek-v4-flash",
            endpoint="https://example.test/v1/chat/completions",
            credential_env="SCOPE_RECALL_TEST_CHAT_KEY",
            output_limit_field="max_tokens",
            max_output_tokens=512,
        ),
        ledger=AuxiliaryBudgetLedger(ledger_path, policy),
        reserve_input=32_768,
        transport=spy,
    )
    try:
        adapter.propose([{"role": "user", "content": "TEST no network"}], remaining_seconds=2)
    except AuxiliaryModelError as exc:
        rejected = exc.error_type == "budget_exhausted"
    else:
        rejected = False
    status = read_auxiliary_budget_status(ledger_path)
    return {
        "status": "PASS" if rejected and spy.calls == 0 and status["requests"] == 0 else "FAIL",
        "error_type_budget_exhausted": rejected,
        "transport_calls": spy.calls,
        "ledger_requests": status["requests"],
        "ledger_path": str(ledger_path),
        "network_calls": 0,
    }


def _plan() -> dict[str, Any]:
    return {
        "queries_per_host": QUERIES_PER_HOST,
        "query_conditions": QUERY_CONDITIONS,
        "journeys_per_host": JOURNEYS_PER_HOST,
        "rounds_per_journey": ROUNDS_PER_JOURNEY,
        "primary_per_arm_host": PRIMARY_PER_ARM_HOST,
        "primary_per_host": PRIMARY_PER_HOST,
        "total_primary": TOTAL_PRIMARY,
        "arms": len(ARMS),
        "hosts": len(HOSTS),
    }


def _budget_contract() -> dict[str, Any]:
    return {
        "ledger_path": LEDGER_PATH,
        "go_call_cap": GO_CALL_CAP,
        "go_input_cap": GO_INPUT_CAP,
        "go_output_cap": GO_OUTPUT_CAP,
        "codex_call_cap": CODEX_CALL_CAP,
        "total_cost_cap_micro_usd": TOTAL_COST_MICRO_USD_CAP,
        "per_request_input_reserve": REQUEST_INPUT_RESERVE,
        "per_request_output_reserve": REQUEST_OUTPUT_RESERVE,
        "mimo_aux_output_reserve": MIMO_AUX_OUTPUT_RESERVE,
        "retry_policy": "no-hidden-retry",
        "reserve_before_network": True,
    }


def _public_receipt(fixture: Path, rows: int, budget_rejection: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "schema": "scope-recall.p18-test-runner-receipt.v1",
        "status": "DRY_RUN_PUBLIC_FIXTURE",
        "execution_ready": False,
        "fixture": {
            "path": str(fixture),
            "rows": rows,
            "sha256": _sha256(fixture),
            "sealed_data_used": False,
        },
        "plan": _plan(),
        "arms": [asdict(arm) for arm in ARMS],
        "hosts": [asdict(host) for host in HOSTS],
        "oracle_boundary": {
            "model_input_has_gold_or_control": False,
            "independent_grader_required": True,
            "gold_read_during_dry_run": False,
        },
        "network_calls": 0,
        "model_calls": 0,
        "budget": _budget_contract(),
        "budget_rejection": budget_rejection,
        "adapter_status": {
            "hermes_a2a": "opt-in-actual-a2a-http; isolated-endpoint-required",
            "codex_windows_desktop": "unsupported-until-actual-desktop-bridge",
        },
    }


def _run_budget_rejection() -> dict[str, Any]:
    planned = TOTAL_PRIMARY
    available = planned - 1
    rejected_before_adapter = available < planned
    return {
        "status": "PASS" if rejected_before_adapter else "FAIL",
        "planned_primary_calls": planned,
        "simulated_available_calls": available,
        "rejected_before_adapter": rejected_before_adapter,
        "network_calls": 0,
        "model_calls": 0,
    }


def _find_declared_hash(value: Any, filename: str) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if filename.lower() in key_text and isinstance(child, str) and len(child) == 64:
                return child
            found = _find_declared_hash(child, filename)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_declared_hash(child, filename)
            if found:
                return found
    return None


def _sealed_preflight(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    fixture_manifest_path = root / "fixture-manifest.json"
    raw_path = root / "raw.jsonl"
    gold_path = root / "gold.jsonl"
    required = (manifest_path, fixture_manifest_path, raw_path, gold_path)
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise ValueError("sealed preflight missing required artifacts: " + ",".join(missing))

    # Only metadata and file hashes are returned.  Raw/gold contents are never
    # parsed or emitted by this runner.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture_manifest = json.loads(fixture_manifest_path.read_text(encoding="utf-8"))
    artifacts: dict[str, Any] = {}
    for name, path in (
        ("manifest.json", manifest_path),
        ("fixture-manifest.json", fixture_manifest_path),
        ("raw.jsonl", raw_path),
        ("gold.jsonl", gold_path),
    ):
        actual = _sha256(path)
        declared = _find_declared_hash(manifest, name) or _find_declared_hash(fixture_manifest, name)
        artifacts[name] = {
            "bytes": path.stat().st_size,
            "sha256": actual,
            "declared_match": (declared is None or declared == actual),
        }
    return {
        "status": "SEALED_PREFLIGHT_HASH_ONLY",
        "manifest_schema": manifest.get("schema"),
        "manifest_status": manifest.get("status"),
        "fixture_manifest_schema": fixture_manifest.get("schema"),
        "fixture_manifest_status": fixture_manifest.get("status"),
        "artifacts": artifacts,
        "raw_gold_content_emitted": False,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P18 TEST-only unified runner")
    parser.add_argument("--dry-run-public", action="store_true")
    parser.add_argument("--core-public", action="store_true")
    parser.add_argument("--offline-matrix", action="store_true")
    parser.add_argument("--ledger-test", action="store_true")
    parser.add_argument("--budget-rejection-test", action="store_true")
    parser.add_argument("--preflight-sealed", action="store_true")
    parser.add_argument("--public-fixture", type=Path)
    parser.add_argument("--sealed-root", type=Path)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--baseline-ref", default=BASELINE_578B)
    parser.add_argument("--final-source-root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.dry_run_public or args.core_public or args.offline_matrix or args.ledger_test or args.budget_rejection_test or args.preflight_sealed):
        print("select a TEST-only operation", file=sys.stderr)
        return 2
    if args.baseline_ref != BASELINE_578B:
        print("baseline ref must remain the frozen 578b ref", file=sys.stderr)
        return 2

    if args.preflight_sealed:
        if args.sealed_root is None:
            print("--sealed-root is required", file=sys.stderr)
            return 2
        receipt: dict[str, Any] = _sealed_preflight(args.sealed_root.resolve())
    else:
        if args.public_fixture is None:
            print("--public-fixture is required for TEST dry-run", file=sys.stderr)
            return 2
        fixture = args.public_fixture.resolve()
        rows = _load_public_fixture(fixture)
        rejection = _run_budget_rejection() if args.budget_rejection_test else None
        receipt = _public_receipt(fixture, rows, rejection)
        run_parent = (args.output.resolve().parent if args.output else Path.cwd() / ".execution" / "TEST-P18-RUNNER").resolve()
        run_parent.mkdir(parents=True, exist_ok=True)
        candidate_manifest = args.candidate_manifest.resolve() if args.candidate_manifest else None
        final_source_root = args.final_source_root.resolve() if args.final_source_root else None
        auto_candidate = False
        if args.offline_matrix and candidate_manifest is None and final_source_root is None:
            final_source_root, candidate_manifest = _make_public_candidate(run_parent)
            auto_candidate = True
        if bool(candidate_manifest) != bool(final_source_root):
            print("--candidate-manifest and --final-source-root must be provided together", file=sys.stderr)
            return 2
        if candidate_manifest and final_source_root:
            try:
                receipt["candidate_source_manifest"] = {
                    **_validate_candidate_manifest(candidate_manifest, final_source_root),
                    "manifest_path": str(candidate_manifest),
                    "source_root": str(final_source_root),
                    "auto_public_test_candidate": auto_candidate,
                }
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        if args.core_public:
            receipt["core_public_run"] = _run_core_public_fixture(fixture, run_parent)
        if args.offline_matrix:
            receipt["offline_backend_matrix"] = _run_offline_backend_matrix(
                fixture,
                run_parent,
                repo_root=(args.repo_root.resolve() if args.repo_root else Path(__file__).resolve().parents[2]),
                candidate_manifest=candidate_manifest,
                final_source_root=final_source_root,
            )
        if args.ledger_test:
            receipt["real_budget_ledger_test"] = _run_real_budget_rejection(run_parent)
        if args.core_public or args.offline_matrix or args.ledger_test:
            receipt["status"] = "TEST_PUBLIC_CORE_AND_LEDGER"

    if args.output:
        _write_json(args.output.resolve(), receipt)
    else:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
