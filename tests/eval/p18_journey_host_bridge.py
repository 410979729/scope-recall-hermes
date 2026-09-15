"""Formal host bridge used by ``p18_journey_execution``.

The journey executor owns ordering and local controls.  This module owns only
the real host turn boundary: a verified Hermes A2A or Codex app-server
transport, observed session/thread identifiers, and one immutable formal
evidence receipt.  It has no fixture or semantic-scoring path.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Protocol

from p18_codex_appserver_transport import (
    CodexAppServerTransport,
    _JsonlPeer,
    _hook_admission,
    _configured_hook_admission,
    _spawn,
    _authenticate_test_peer,
    _trust_test_hooks,
    verified_usage_baseline_from_receipt,
    VerifiedUsageBaseline,
)
from p18_formal_evidence import FormalEvidenceWriter, verify_formal_run_config
from p18_formal_runner import _append_formal_transport_operation
from p18_hermes_a2a_transport import HermesA2ATransport


class JourneyHostBridgeError(ValueError):
    """Configuration or observed-host contract failure."""


class SessionControlPort(Protocol):
    """Explicit host-owner controls; no session is guessed from model text."""

    def new_session(self, alias: str) -> Mapping[str, Any]: ...

    def quiesce(self) -> None: ...

    def resume(self) -> None: ...

    def quiesce_workers(self) -> None: ...


class CodexAppServerSessionControl:
    """Concrete zero-model Codex lifecycle control.

    ``thread/start`` is the only operation used by ``new_session``.  It
    never sends a turn. Empty threads are not yet persisted by app-server,
    so the owned peer stays alive until the first natural turn consumes it.
    Later turns resume the persisted thread through the normal transport.
    """

    def __init__(self, transport: CodexAppServerTransport, data_directory=None) -> None:
        from p18_owned_host_lifecycle import WorkerPause
        self.transport = transport
        self._peers: dict[str, _JsonlPeer] = {}
        self._quiesced = False
        self._jobs = []
        self.pause = WorkerPause(data_directory)
        self.zero_usage_sessions = set()
        transport.process_observer = self.watch_process

    def watch_process(self, process):
        from scope_recall.runtime.worker_watchdog import _OwnedWindowsJob
        job = _OwnedWindowsJob()
        try:
            job.assign(process)
        except Exception:
            process.kill()
            process.wait(timeout=5)
            job.close()
            raise
        self._jobs.append(job)

    def new_session(self, alias: str) -> Mapping[str, Any]:
        config = self.transport.config
        process = _spawn(config)
        self.watch_process(process)
        peer = _JsonlPeer(process, None)
        peer.start()
        deadline = time.monotonic() + min(config.timeout_seconds, 15.0)
        try:
            def request(method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
                request_id = peer.next_id()
                peer.send({"id": request_id, "method": method, "params": dict(params or {})})
                response = peer.wait_response(request_id, deadline, lambda _message: None)
                if not isinstance(response, Mapping) or not isinstance(response.get("result"), Mapping):
                    # Persist bounded metadata for setup failures, which occur
                    # before a formal turn receipt exists. Never store stdin.
                    diagnostic = {"method": method, "process_exit": process.poll(),
                        "stderr": peer.stderr_summary(), "response_error_code":
                        (response or {}).get("error", {}).get("code") if isinstance(response, Mapping) else None}
                    home = Path((getattr(config, "environment", None) or {}).get("CODEX_HOME", str(config.cwd)))
                    if home.is_dir() and any(part.upper().startswith("TEST") for part in home.parts):
                        target = home / f"TEST-session-start-failure-{time.time_ns()}.json"
                        target.write_text(json.dumps(diagnostic, sort_keys=True), encoding="utf-8")
                    raise JourneyHostBridgeError(f"codex_{method.replace('/', '_')}_failed:exit={process.poll()}")
                return response["result"]

            initialized = request("initialize", {"clientInfo": {"name": "scope-recall-p18", "version": "TEST"}, "capabilities": {"experimentalApi": True}})
            peer.send({"method": "initialized"})
            _authenticate_test_peer(config, request)
            _trust_test_hooks(config, request)
            # hooks/list is part of arm admission.  It is intentionally
            # observed before thread/start, while no model turn is running.
            hooks = request("hooks/list", {"cwds": [str(config.cwd)]})
            policy = getattr(config, "expected_hooks_policy", None)
            if policy is not None:
                errors = _configured_hook_admission(hooks, config)
                if errors:
                    raise JourneyHostBridgeError("codex_hook_admission_failed")
            started = request("thread/start", {"cwd": str(config.cwd), "model": config.model, "ephemeral": False, "approvalPolicy": "never", "sandbox": "danger-full-access"})
            thread = started.get("thread") if isinstance(started.get("thread"), Mapping) else started
            session_id = thread.get("id") if isinstance(thread, Mapping) else None
            if not isinstance(session_id, str) or not session_id.strip():
                raise JourneyHostBridgeError("codex_thread_start_id_missing")
            self._peers[session_id] = peer
            if not hasattr(self.transport, "_started_peers"):
                self.transport._started_peers = {}
            self.transport._started_peers[session_id] = (peer, initialized)
            self.zero_usage_sessions.add(session_id)
            return {"session_id": session_id, "owned_process": True}
        except Exception:
            peer.close()
            raise

    def quiesce(self) -> None:
        peers = tuple(self._peers.values())
        for session_id in self._peers:
            self.transport._started_peers.pop(session_id, None)
        self._peers.clear()
        for peer in peers:
            peer.close()
        for job in self._jobs:
            job.close()
        self._jobs.clear()
        self._quiesced = True

    def resume(self) -> None:
        self._quiesced = False

    def quiesce_workers(self) -> None:
        self.quiesce()
        self.pause.acquire()

    def close(self) -> None:
        self.quiesce()
        self.pause.close()


class HermesRoutingSessionControl:
    """Owned gateway lifecycle plus its configured idle-reset boundary.

    All aliases retain the same A2A context/audience. Waiting for the frozen
    idle policy makes the next public message/send create a genuine session;
    no routing row is edited and no private reset API is called.
    """
    def __init__(self, *, state_db, routes, config_root, process_owner=None, idle_seconds=60):
        self.state_db = _test_path(state_db)
        self.config_root = _test_path(config_root, directory=True)
        if not self.state_db.is_relative_to(self.config_root):
            raise JourneyHostBridgeError("hermes_state_db_outside_TEST_root")
        self.routes = {str(k): dict(v) for k, v in routes.items()}
        self.process_owner = process_owner
        self.idle_seconds = float(idle_seconds)
        if not 1 <= self.idle_seconds <= 3600:
            raise JourneyHostBridgeError("hermes_idle_policy_invalid")
        self.last_turn = None
        self.opened_aliases = set()

    def new_session(self, alias):
        route = self.routes.get(alias)
        if not isinstance(route, Mapping):
            raise JourneyHostBridgeError("hermes_session_route_missing")
        context = _id(route.get("context_id"), "context_id")
        if alias in self.opened_aliases:
            raise JourneyHostBridgeError("session_alias_reused")
        if self.last_turn is not None:
            deadline = self.last_turn + self.idle_seconds + 1
            while time.monotonic() < deadline:
                time.sleep(min(.25, deadline-time.monotonic()))
        self.opened_aliases.add(alias)
        return {"session_id": None, "pending_context_id": context, "context_id": context,
                "owned_process": self.process_owner is not None}

    def observe(self, alias):
        route = self.routes[alias]
        if not self.state_db.is_file():
            raise JourneyHostBridgeError("hermes_session_not_observed")
        with sqlite3.connect(self.state_db.as_uri()+"?mode=ro", uri=True, timeout=2) as db:
            rows = db.execute("SELECT scope,session_key,entry_json FROM gateway_routing WHERE scope=? AND session_key=?",
                              (route["scope"],route["session_key"])).fetchall()
        if len(rows) != 1:
            raise JourneyHostBridgeError("hermes_session_not_observed")
        entry = json.loads(rows[0][2])
        session_id = _id(entry.get("session_id"), "hermes_actual_session_id")
        if session_id == route["context_id"] or entry.get("session_key") != route["session_key"]:
            raise JourneyHostBridgeError("hermes_gateway_session_mismatch")
        self.last_turn = time.monotonic()
        return {"session_id": session_id, "context_id": route["context_id"],
                "session_key": route["session_key"], "scope": rows[0][0],
                "owned_process": self.process_owner is not None}

    def quiesce(self):
        self.process_owner.quiesce()

    def resume(self):
        self.process_owner.resume()

    def quiesce_workers(self):
        self.process_owner.quiesce_workers()

    def close(self):
        if self.process_owner is not None:
            self.process_owner.close()


class HermesRoutingSessionObserver:
    """Re-read the isolated route after a turn and verify its session id."""

    def __init__(self, control: HermesRoutingSessionControl) -> None:
        self.control = control

    def __call__(self, alias: str, expected_session_id: str | None, result: Mapping[str, Any]) -> Mapping[str, Any]:
        observed = self.control.observe(alias)
        if expected_session_id is None:
            if observed.get("pending_session") is True or isinstance(observed.get("pending_context_id"), str):
                raise JourneyHostBridgeError("hermes_session_not_observed")
        elif observed.get("session_id") != expected_session_id:
            raise JourneyHostBridgeError("observed_session_mismatch")
        context_id = result.get("context_id")
        if context_id is not None and context_id != observed.get("context_id"):
            raise JourneyHostBridgeError("observed_context_mismatch")
        return observed


class JourneySourceRefsProvider:
    """Read only this turn's actual Core rows; baseline has no Core refs."""
    def __init__(self, database_path=None):
        self.database_path = Path(database_path).resolve() if database_path else None
        self.watermark = None

    def begin(self):
        self.watermark = None
        if self.database_path is None:
            return
        with sqlite3.connect(self.database_path.as_uri()+"?mode=ro", uri=True, timeout=.15) as db:
            self.watermark = db.execute("SELECT coalesce(max(rowid),0) FROM source_events").fetchone()[0]

    def __call__(self, operation_id, session_id):
        if self.database_path is None:
            return ()
        if self.watermark is None:
            raise JourneyHostBridgeError("source_capture_watermark_unavailable")
        with sqlite3.connect(self.database_path.as_uri()+"?mode=ro", uri=True, timeout=.15) as db:
            rows = db.execute("SELECT event_id,source_revision FROM source_events WHERE rowid>? AND session_id=? ORDER BY rowid LIMIT 257",
                              (self.watermark, session_id)).fetchall()
        if len(rows)>256:
            raise JourneyHostBridgeError("source_capture_bound")
        return tuple(f"{ref}@{revision}" for ref,revision in rows)


SourceRefsProvider = Callable[[str, str], Sequence[str]]
SessionObserver = Callable[[str, str | None, Mapping[str, Any]], Mapping[str, Any]]
UsageProvider = Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]
AttachmentEncoder = Callable[[Mapping[str, Any], Path], str]

_HEX64 = frozenset("0123456789abcdef")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _test_path(value: str | Path, *, directory: bool = False) -> Path:
    path = Path(value).expanduser().resolve()
    lowered = str(path).replace("/", "\\").lower()
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\") or "test" not in lowered:
        raise JourneyHostBridgeError("TEST_isolation_required")
    if directory and not path.is_dir():
        raise JourneyHostBridgeError("TEST_output_root_missing")
    return path


def _id(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise JourneyHostBridgeError(f"{name}_missing")
    return value


def _query(model_input: Mapping[str, Any], workspace: Path, encoder: AttachmentEncoder | None) -> str:
    if set(model_input) != {"query", "attachments"}:
        raise JourneyHostBridgeError("natural_input_schema_invalid")
    query = model_input.get("query")
    attachments = model_input.get("attachments")
    if type(query) is not str or not query.strip() or not isinstance(attachments, list):
        raise JourneyHostBridgeError("natural_input_invalid")
    if attachments:
        if encoder is None:
            raise JourneyHostBridgeError("attachments_require_explicit_host_encoder")
        query = encoder(model_input, workspace)
        if type(query) is not str or not query.strip():
            raise JourneyHostBridgeError("host_attachment_encoder_invalid")
    return query


class _BaseJourneyHostBridge:
    transport: Any
    host_id: str

    def __init__(
        self,
        *,
        transport: Any,
        formal_config_path: str | Path,
        output_root: str | Path,
        session_control: SessionControlPort | None,
        source_refs_provider: SourceRefsProvider,
        session_observer: SessionObserver | None = None,
        usage_provider: UsageProvider | None = None,
        attachment_encoder: AttachmentEncoder | None = None,
    ) -> None:
        if not callable(getattr(session_control, "new_session", None)):
            raise JourneyHostBridgeError("session_control_required")
        if not callable(source_refs_provider):
            raise JourneyHostBridgeError("source_refs_provider_required")
        self.transport = transport
        self.formal_config_path = _test_path(formal_config_path)
        self.output_root = _test_path(output_root, directory=True)
        self.session_control = session_control
        self.source_refs_provider = source_refs_provider
        self.session_observer = session_observer
        self.usage_provider = usage_provider
        self.attachment_encoder = attachment_encoder
        readiness = verify_formal_run_config(self.formal_config_path)
        if not readiness.formal_execution_allowed:
            raise JourneyHostBridgeError("formal_config_not_ready")
        config_root = Path(readiness.details["config_path"]).resolve().parent
        try:
            self.output_root.relative_to(config_root)
        except ValueError as exc:
            raise JourneyHostBridgeError("formal_output_outside_config_root") from exc
        self.readiness = readiness
        self.writer = FormalEvidenceWriter(self.output_root, self.formal_config_path)
        operations = readiness.details.get("operations")
        if not isinstance(operations, Mapping):
            raise JourneyHostBridgeError("formal_operation_map_missing")
        self.operations = dict(operations)
        self._sessions: dict[str, str | None] = {}
        self._pending_aliases: set[str] = set()
        self._pending_contexts: dict[str, str] = {}
        self._contexts: dict[str, str] = {}
        self._owned_process: dict[str, bool] = {}

    def new_session(self, alias: str) -> dict[str, Any]:
        alias = _id(alias, "session_alias")
        if alias in self._sessions:
            raise JourneyHostBridgeError("session_alias_reused")
        result = self.session_control.new_session(alias)
        if not isinstance(result, Mapping):
            raise JourneyHostBridgeError("new_session_receipt_invalid")
        pending_context = result.get("pending_context_id")
        pending = result.get("pending_session") is True or isinstance(pending_context, str)
        raw_session_id = result.get("session_id")
        if pending:
            context_id = _id(pending_context or result.get("context_id"), "pending_context_id")
            session_id = None
        else:
            session_id = _id(raw_session_id, "session_id")
        if session_id is not None and session_id in self._sessions.values():
            raise JourneyHostBridgeError("new_session_not_distinct")
        context_id = result.get("context_id") or (pending_context if pending else None)
        if context_id is not None:
            context_id = _id(context_id, "context_id")
        if self.host_id == "hermes_a2a" and context_id is None:
            raise JourneyHostBridgeError("hermes_context_id_required")
        self._sessions[alias] = session_id
        if pending:
            self._pending_aliases.add(alias)
            assert context_id is not None
            self._pending_contexts[alias] = context_id
        if context_id is not None and session_id is not None:
            self._contexts[session_id] = context_id
        if session_id is not None:
            self._owned_process[session_id] = result.get("owned_process") is True
        return {"session_id": session_id, **({"pending_context_id": context_id} if pending else {})}

    def _operation(self, operation_id: str) -> Mapping[str, Any]:
        operation = self.operations.get(operation_id)
        if not isinstance(operation, Mapping):
            raise JourneyHostBridgeError("operation_not_in_frozen_config")
        if operation.get("host_id") != self.host_id:
            raise JourneyHostBridgeError("operation_host_mismatch")
        return operation

    def _usage(self, operation_id: str, result: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if self.usage_provider is not None:
            value = self.usage_provider(operation_id, result)
            if value is not None and not isinstance(value, Mapping):
                raise JourneyHostBridgeError("formal_usage_provider_invalid")
            return value
        value = result.get("formal_usage")
        return value if isinstance(value, Mapping) else None

    def _raw_path(self, operation_id: str) -> tuple[Path, Path]:
        operation_root = self.output_root / "operations" / operation_id
        operation_root.mkdir(parents=True, exist_ok=False)
        logical = operation_root / "response.bin"
        transport_path = logical
        if isinstance(self.transport, HermesA2ATransport):
            try:
                logical.relative_to(self.transport.config.isolation_root)
            except ValueError:
                transport_path = self.transport.config.isolation_root / ".p18-raw" / operation_id / "response.bin"
        return logical, transport_path

    def _persist_turn(self, *, operation_id: str, query: str, session_id: str | None, source_refs: Sequence[str], result: Mapping[str, Any], logical_raw: Path, transport_raw: Path, started: float) -> dict[str, Any]:
        result = dict(result)
        if transport_raw != logical_raw and transport_raw.is_file() and not logical_raw.exists():
            logical_raw.write_bytes(transport_raw.read_bytes())
        result["formal_usage"] = self._usage(operation_id, result)
        receipt_path = logical_raw.parent / "transport-receipt.json"
        receipt_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result["session_id"] = session_id
        item = {
            "operation_id": operation_id,
            "query": query,
            "query_sha256": _sha256(query.encode("utf-8")),
            "source_capture_refs": list(source_refs),
            "session_id": session_id,
            "turn_id": result.get("task_id") or (result.get("association") or {}).get("turn_id"),
            "owned_process": self._owned_process.get(session_id) is True
            or (self.host_id == "codex_windows_appserver_native_hooks_v2"
                and isinstance(result.get("process"), Mapping)
                and isinstance(result["process"].get("pid"), int)),
            "context_id": result.get("context_id") or self._contexts.get(session_id),
            "usage": result.get("formal_usage"),
        }
        frozen = self._operation(operation_id)
        delivery_gaps = result.get("delivery_gaps") or []
        if delivery_gaps:
            gap_path = logical_raw.parent / "delivery-gap.json"
            gap_payload = {"operation_id": operation_id, "fault": frozen.get("fault"),
                           "gaps": delivery_gaps, "source_capture_refs": list(source_refs)}
            gap_path.write_bytes(json.dumps(gap_payload, ensure_ascii=False).encode("utf-8"))
            config_root = self.formal_config_path.parent
            item["delivery"] = {"status": "failed", "context_sha256": _sha256(gap_path.read_bytes()),
                                "artifact_path": str(gap_path.relative_to(config_root))}
            if frozen.get("fault") != "sqlite_unavailable":
                # Ordinary capture errors are not the approved J08 degraded
                # host-completion scenario. Keep paid usage and raw response.
                result.setdefault("errors", []).append({"kind": "delivery", "error_type": "unexpected_source_capture_gap"})
                receipt_path.write_bytes(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        evidence_path = _append_formal_transport_operation(
            self.writer,
            self.readiness,
            frozen=frozen,
            item=item,
            result=result,
            response_path=receipt_path,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            transport=self.transport,
        )
        return {
            "session_id": session_id,
            "formal_evidence_path": str(evidence_path),
            "source_refs": list(source_refs),
            "transport_status": result.get("transport_status") or result.get("status"),
            "request_id": frozen.get("request_id"),
            "turn_id": result.get("task_id") or (result.get("association") or {}).get("turn_id"),
            "capability_gaps": result.get("capability_gaps", []),
            "session_lineage": result.get("session_lineage", []),
            "attachment_entry": result.get("attachment_entry"),
        }

    def _observe(self, alias: str, session_id: str | None, result: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if self.session_observer is None:
            if self.host_id == "hermes_a2a":
                raise JourneyHostBridgeError("hermes_session_observer_required")
            association = result.get("association")
            if not isinstance(association, Mapping) or association.get("thread_id") != session_id:
                raise JourneyHostBridgeError("codex_thread_observation_missing")
            return None
        observed = self.session_observer(alias, session_id, result)
        if not isinstance(observed, Mapping):
            raise JourneyHostBridgeError("observed_session_mismatch")
        observed_session = observed.get("session_id")
        if session_id is None and alias in self._pending_aliases:
            if not isinstance(observed_session, str) or not observed_session.strip():
                raise JourneyHostBridgeError("hermes_actual_session_id_required")
            self._pending_aliases.remove(alias)
            self._sessions[alias] = observed_session
            self._contexts[observed_session] = self._pending_contexts.pop(alias)
            self._owned_process[observed_session] = observed.get("owned_process") is True
            return observed
        if observed_session != session_id:
            if self.host_id != "hermes_cli_local_input_v1" or session_id not in observed.get("session_lineage", []):
                raise JourneyHostBridgeError("observed_session_mismatch")
            self._sessions[alias] = observed_session
            self._contexts[observed_session] = self._contexts[session_id]
            self._owned_process[observed_session] = observed.get("owned_process") is True
        return observed

    def execute_turn(self, *, operation_id: str, session_alias: str, session_id: str | None, model_input: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
        operation = self._operation(_id(operation_id, "operation_id"))
        del operation  # validation above binds the operation to this host/arm
        if session_id is None and session_alias not in self._pending_aliases:
            # The first natural turn may be the first lifecycle operation in a
            # journey.  Create the real host session before dispatching it.
            session_id = self.new_session(session_alias)["session_id"]
        elif session_id != self._sessions.get(session_alias):
            raise JourneyHostBridgeError("session_not_bound_to_alias")
        query = _query(model_input, workspace.resolve(), self.attachment_encoder)
        logical_raw, transport_raw = self._raw_path(operation_id)
        started = time.perf_counter()
        budget = getattr(self.transport, "budget", None)
        if callable(getattr(budget, "begin_operation", None)):
            budget.begin_operation(operation_id)
        source_gap = None
        try:
            self.source_refs_provider.begin()
        except AttributeError:
            pass  # explicit test double; production factory always supplies the observer
        except Exception as exc:
            source_gap = type(exc).__name__
        result: Mapping[str, Any]
        try:
            result = self._dispatch(operation_id, query, session_id, session_alias, transport_raw, budget)
        except Exception as exc:
            result = {"status": "FAILED", "formal_evaluation": False, "fixture_mode": False, "errors": [{"kind": "transport", "error_type": type(exc).__name__}]}
        if callable(getattr(budget, "formal_usage", None)):
            result = dict(result)
            try:
                usage = budget.formal_usage()
                if usage is not None:
                    result["formal_usage"] = usage
            except Exception as exc:
                result.setdefault("errors", []).append({"kind":"accounting", "error_type":type(exc).__name__})
                result["status"] = result["transport_status"] = "FAILED"
        try:
            observed = self._observe(session_alias, session_id, result)
        except Exception as exc:
            # The request may already be charged. Persist its raw response,
            # ledger and missing-ID failure instead of losing the operation
            # to a routing/observer exception after dispatch.
            result = dict(result)
            result.setdefault("errors", []).append({"kind": "session_observation", "error_type": type(exc).__name__})
            return self._persist_turn(operation_id=operation_id, query=query, session_id=session_id,
                                      source_refs=(), result=result, logical_raw=logical_raw,
                                      transport_raw=transport_raw, started=started)
        if isinstance(observed, Mapping):
            session_id = observed.get("session_id")
        if session_id is None:
            session_id = observed.get("session_id") if isinstance(observed, Mapping) else None
            if not isinstance(session_id, str) or not session_id.strip():
                raise JourneyHostBridgeError("hermes_actual_session_id_required")
        # Source capture is a host/Core side effect and may only become
        # visible after the turn.  Empty refs are retained as a failed or
        # incomplete formal-evidence condition; they are never fabricated.
        try:
            lineage = result.get("session_lineage") if self.host_id == "hermes_cli_local_input_v1" else None
            refs = tuple(dict.fromkeys(ref for sid in (lineage or [session_id])
                                     for ref in self.source_refs_provider(operation_id, sid)))
        except Exception as exc:
            result = dict(result)
            result.setdefault("delivery_gaps", []).append({"kind": "source_observation", "error_type": type(exc).__name__})
            refs = ()
        if any(
            type(ref) is not str
            or "@" not in ref
            or not ref.rsplit("@", 1)[1].isdigit()
            or int(ref.rsplit("@", 1)[1]) < 1
            for ref in refs
        ):
            result = dict(result)
            result.setdefault("delivery_gaps", []).append({"kind": "source_observation", "error_type": "invalid_source_ref"})
            refs = ()
        if source_gap:
            result = dict(result)
            result.setdefault("delivery_gaps", []).append({"kind": "source_capture_begin", "error_type": source_gap})
        if self._operation(operation_id).get("arm_id") == "C" and not refs:
            result = dict(result)
            result.setdefault("delivery_gaps", []).append({"kind": "capture", "error_type": "no_persisted_Core_source_this_turn"})
        if model_input["attachments"]:
            result = dict(result)
            # A natural path is real host input, but does not itself prove a
            # file tool read or a plugin-managed retained attachment. Keep
            # these questions visible in the result for independent scoring.
            result["attachment_entry"] = {
                "mode": "authorized_local_file_path",
                "files": [{"workspace_path": row["workspace_path"], "sha256": row["asset"]["sha256"]}
                          for row in model_input["attachments"]],
                "file_tool_read": "REQUIRES_ACTUAL_HOST_TRACE",
                "managed_attachment": "NOT_VERIFIED",
            }
            result.setdefault("capability_gaps", []).append("managed_attachment_retention_and_purge_not_verified")
        return self._persist_turn(operation_id=operation_id, query=query, session_id=session_id, source_refs=refs, result=result, logical_raw=logical_raw, transport_raw=transport_raw, started=started)

    def _dispatch(self, operation_id: str, query: str, session_id: str | None, session_alias: str, raw_path: Path, budget: Any) -> Mapping[str, Any]:
        raise NotImplementedError

    def quiesce(self) -> None:
        method = getattr(self.session_control, "quiesce", None)
        if not callable(method):
            raise JourneyHostBridgeError("quiesce_control_missing")
        method()
        controls = getattr(self, "journey_controls", None)
        if controls is not None and controls.runtime is not None:
            controls.runtime.close()

    def resume(self) -> None:
        method = getattr(self.session_control, "resume", None)
        if not callable(method):
            raise JourneyHostBridgeError("resume_control_missing")
        controls = getattr(self, "journey_controls", None)
        if controls is not None and controls.runtime is not None and controls.runtime._closed:
            from scope_recall.runtime.instance import build_runtime_instance
            controls.runtime = build_runtime_instance(controls.runtime.config)
            controls.core = controls.runtime.core
        method()

    def quiesce_workers(self) -> None:
        method = getattr(self.session_control, "quiesce_workers", None)
        if not callable(method):
            raise JourneyHostBridgeError("quiesce_workers_control_missing")
        method()

    def close(self) -> None:
        method = getattr(self.session_control, "close", None)
        if callable(method):
            method()
        controls = getattr(self, "journey_controls", None)
        if controls is not None and controls.runtime is not None:
            controls.runtime.close()


class HermesCLISessionControl:
    """One official CLI process per turn; fresh sessions are observed after chat."""
    def __init__(self, transport):
        self.transport = transport
        self.process_owner = transport.owner
        self.aliases = set()
        self.ids = {}

    def new_session(self, alias):
        if alias in self.aliases:
            raise JourneyHostBridgeError("session_alias_reused")
        self.aliases.add(alias)
        return {"session_id": None, "pending_context_id": "cli-local", "owned_process": True}

    def observe(self, alias, prior, result):
        from p18_hermes_cli_transport import validate_cli_capture
        observed = validate_cli_capture(result, self.transport.root)
        actual = observed["session_id"]
        if result.get("requested_session_id") != prior or any(a != alias and sid == actual for a, sid in self.ids.items()):
            raise JourneyHostBridgeError("CLI_observed_session_reused_or_wrong_resume")
        self.ids[alias] = actual
        return {**observed, "owned_process": True, "session_lineage": result["session_lineage"]}

    def resume(self):
        self.transport.owner.resume()

    def quiesce(self):
        self.transport.owner.quiesce()

    def quiesce_workers(self):
        self.transport.owner.quiesce_workers()

    def close(self):
        self.transport.owner.close()


class HermesCLIJourneyHostBridge(_BaseJourneyHostBridge):
    host_id = "hermes_cli_local_input_v1"

    def _dispatch(self, operation_id, query, session_id, session_alias, raw_path, budget):
        return self.transport.execute(query, operation_id=operation_id,
            request_id=self._operation(operation_id)["request_id"], session_id=session_id, output=raw_path.parent)


class HermesJourneyHostBridge(_BaseJourneyHostBridge):
    host_id = "hermes_a2a"

    def __init__(self, *, transport: HermesA2ATransport, **kwargs: Any) -> None:
        if not isinstance(transport, HermesA2ATransport):
            raise JourneyHostBridgeError("hermes_transport_required")
        super().__init__(transport=transport, **kwargs)

    def _dispatch(self, operation_id: str, query: str, session_id: str | None, session_alias: str, raw_path: Path, budget: Any) -> Mapping[str, Any]:
        frozen = self._operation(operation_id)
        context_id = self._contexts.get(session_id) if session_id is not None else self._pending_contexts.get(session_alias)
        if context_id is None:
            raise JourneyHostBridgeError("hermes_context_id_required")
        if callable(getattr(budget, "set_operation", None)):
            budget.set_operation(operation_id=operation_id, request_id=str(frozen["request_id"]), task_id=operation_id, context_id=context_id)
        return self.transport.execute(query, context_id=context_id, message_id=str(frozen["request_id"]), raw_response_path=raw_path)


class CodexJourneyHostBridge(_BaseJourneyHostBridge):
    host_id = "codex_windows_appserver_native_hooks_v2"

    def __init__(self, *, transport: CodexAppServerTransport, session_control: SessionControlPort | None = None, simple_capture_path: Path | None = None, **kwargs: Any) -> None:
        if not isinstance(transport, CodexAppServerTransport):
            raise JourneyHostBridgeError("codex_transport_required")
        if session_control is None:
            session_control = CodexAppServerSessionControl(transport)
        kwargs["session_control"] = session_control
        self._usage_baselines = {}
        self._simple_capture_path = simple_capture_path
        super().__init__(transport=transport, **kwargs)

    def _dispatch(self, operation_id: str, query: str, session_id: str | None, session_alias: str, raw_path: Path, budget: Any) -> Mapping[str, Any]:
        if session_id is None:
            raise JourneyHostBridgeError("codex_session_id_required")
        baseline = self._usage_baselines.get(session_id)
        if session_id in self.session_control.zero_usage_sessions:
            # Observed thread/start performed zero model turns. Once this
            # submission starts, only later verified receipts can be a baseline.
            self.session_control.zero_usage_sessions.remove(session_id)
            baseline = VerifiedUsageBaseline(session_id, 0, 0)
        capture = self._simple_capture_path
        before = capture.read_bytes() if capture is not None and capture.is_file() else b""
        result = self.transport.run_turn(query, thread_id=session_id, raw_capture_path=raw_path,
                                         usage_baseline=baseline)
        if capture is not None:
            from p18_codex_simple_capture import observe_simple_capture
            result = dict(result)
            observed = observe_simple_capture(capture, before, query, session_id, result, raw_path.parent)
            result["simple_search_callback"] = observed
            if observed["status"] != "OBSERVED":
                result["status"] = "FAIL"
                result.setdefault("errors", []).append({"kind": "capture", "error_type": "native_simple_search_callback_not_observed"})
        try:
            self._usage_baselines[session_id] = verified_usage_baseline_from_receipt(result)
        except (ValueError, RuntimeError):
            self._usage_baselines.pop(session_id, None)
        return result


__all__ = [
    "CodexAppServerSessionControl",
    "CodexJourneyHostBridge",
    "HermesRoutingSessionControl",
    "HermesRoutingSessionObserver",
    "HermesJourneyHostBridge",
    "JourneyHostBridgeError",
    "JourneySourceRefsProvider",
    "SessionControlPort",
]
