"""Actual classic Hermes CLI, owned metered child, and public session export.

No import starts a process. Formal config and installed arm binding are
checked before dispatch. This transport does not score semantic correctness.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile

from p18_owned_host_lifecycle import OwnedHermesProcess

METHOD_ID = "hermes_cli_local_input_v1"
METHOD_SHA256 = "119f596efd98476a87401775b692bf0dd7718e72949e0f1813571720c85c57af"
MODEL = "deepseek-v4-flash"
TRANSPORT = "hermes_official_classic_cli"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,239}\Z")
_COUNTERS = ("input_tokens", "output_tokens", "api_call_count", "cache_read_tokens", "cache_write_tokens")
MAX_OUTPUT_BYTES = 2_097_152
DEFAULT_CHAT_TIMEOUT_SECONDS = 90.0


def _chat_timeout_seconds() -> float:
    raw = os.environ.get("SCOPE_RECALL_HERMES_CLI_CHAT_TIMEOUT_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_CHAT_TIMEOUT_SECONDS
    value = float(raw)
    if not 0 < value <= 7200:
        raise ValueError("SCOPE_RECALL_HERMES_CLI_CHAT_TIMEOUT_SECONDS")
    return value


SOURCE_RECEIPT_SHA256 = (
    "9898888e0e37381fcedca917d0dd31cbaeeebdcb03c4636cb66f4f1103b7ed47"
)


_VERIFIED_TREES = {}
_VERIFIED_FILES = {}
_FILE_MUTATION_VERIFIER = re.compile(
    r"\n+⚠️ File-mutation verifier:.*\Z",
    flags=re.DOTALL,
)


def _visible_cli_answer(text: str) -> str:
    """Drop Hermes CLI's trailing file-mutation verifier banner only.

    The banner is appended to stdout after a failed write_file; it is not
    stored in the public session export. Do not invent answer text.
    """
    if not isinstance(text, str) or not text:
        return text
    stripped = _FILE_MUTATION_VERIFIER.sub("", text)
    return stripped if stripped else text


def _cached_file_sha(path):
    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    digest = _VERIFIED_FILES.get(key)
    if digest is None:
        digest = _sha(resolved.read_bytes())
        _VERIFIED_FILES[key] = digest
    return digest


def _cached_tree_sha(path, expected):
    from p18_host_arm_binding import _tree_sha256

    resolved = Path(path).resolve()
    key = (str(resolved), expected)
    digest = _VERIFIED_TREES.get(key)
    if digest is None:
        digest = _tree_sha256(resolved)
        if digest == expected:
            _VERIFIED_TREES[key] = digest
    return digest


def verify_binding(binding):
    """Zero-process validation of the installed, frozen CLI launch files."""
    repo = Path(__file__).resolve().parents[2]
    receipt = repo / ".execution/TEST-G0-HERMES-CLI-METHOD-v1/receipt.json"
    if _cached_file_sha(receipt) != SOURCE_RECEIPT_SHA256:
        raise HermesCLIError("frozen_CLI_source_receipt_changed")
    fixed = binding["fixed_host"]
    source = Path(fixed["source"]["path"]).resolve()
    source_key = (str(source), fixed["source"]["sha256"], SOURCE_RECEIPT_SHA256)
    if source_key not in _VERIFIED_TREES:
        for reference in _json(receipt)["files"]:
            audited = Path(reference["path"])
            # The adjudication explicitly pins this source snapshot, not a host
            # with a merely matching version label.
            if (
                not audited.resolve().is_relative_to(source)
                or _cached_file_sha(audited) != reference["sha256"]
            ):
                raise HermesCLIError("frozen_CLI_source_file_changed")
        if _cached_tree_sha(source, fixed["source"]["sha256"]) != fixed["source"]["sha256"]:
            raise HermesCLIError("CLI_source_tree_changed")
        _VERIFIED_TREES[source_key] = True
    if _cached_file_sha(fixed["runtime_python_path"]) != fixed.get(
        "runtime_python_sha256"
    ):
        raise HermesCLIError("CLI_runtime_python_changed")
    for key in ("config_artifact", "environment_artifact", "runtime_config_artifact"):
        ref = binding["launch_contract"][key]
        if _cached_file_sha(ref["path"]) != ref["sha256"]:
            raise HermesCLIError("CLI_launch_file_changed")
    loader = binding.get("loader", {})
    for key in ("plugin_directory", "candidate_install"):
        if (
            isinstance(loader.get(key), dict)
            and _cached_tree_sha(Path(loader[key]["path"]), loader[key]["sha256"])
            != loader[key]["sha256"]
        ):
            raise HermesCLIError("CLI_installed_loader_changed")


class HermesCLIError(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    return json.loads(Path(path).read_bytes())


def _meter_port(config):
    provider = config.get("model", {}).get("provider")
    matches = [item for item in config.get("custom_providers", []) if item.get("name") == provider]
    if len(matches) != 1:
        raise HermesCLIError("unique_frozen_CLI_meter_provider_required")
    value = matches[0].get("base_url")
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{5})/v1", value or "") if isinstance(value, str) else None
    if match is None or not 29991 <= int(match.group(1)) <= 30100:
        raise HermesCLIError("frozen_CLI_meter_loopback_port_required")
    return int(match.group(1))


def frozen_meter_port(binding):
    """Read only the hash-bound provider URL; no environment override."""
    ref = binding["launch_contract"]["config_artifact"]
    path = Path(binding["roots"]["config_path"])
    if path.resolve() != Path(ref["path"]).resolve() or _sha(path.read_bytes()) != ref["sha256"]:
        raise HermesCLIError("CLI_launch_file_changed")
    return _meter_port(_json(path))


def _ref(path, root):
    return {
        "path": str(path.resolve().relative_to(root)),
        "sha256": _sha(path.read_bytes()),
    }


def _read_ref(ref, root):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise HermesCLIError("CLI_artifact_required")
    relative = Path(ref["path"])
    path = (root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(root)
    ):
        raise HermesCLIError("CLI_artifact_outside_bundle")
    raw = path.read_bytes()
    if _sha(raw) != ref["sha256"]:
        raise HermesCLIError("CLI_artifact_hash_mismatch")
    return raw


def parse_export(raw, expected_id):
    rows = [
        json.loads(line)
        for line in raw.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    if (
        len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("id") != expected_id
    ):
        raise HermesCLIError("exact_public_session_export_required")
    row = rows[0]
    if not isinstance(row.get("messages"), list):
        raise HermesCLIError("export_messages_missing")
    return row


def usage_delta(before, after):
    """Normalize frozen Hermes disjoint input buckets to upstream prompt totals.

    Hermes usage_pricing.CanonicalUsage.prompt_tokens is uncached input plus
    cache read and cache write. All cumulative buckets must be present and
    monotonic; missing cache counters cannot justify a ledger discrepancy.
    """
    totals = {key: 0 for key in _COUNTERS}
    if not set(before) <= set(after):
        return {"status": "unknown", "reason": "physical_lineage_missing"}
    for session_id, row in after.items():
        old = before.get(session_id, {key: 0 for key in _COUNTERS})
        for key in _COUNTERS:
            new_value, old_value = row.get(key), old.get(key)
            if (
                type(new_value) is not int
                or type(old_value) is not int
                or old_value < 0
                or new_value < old_value
            ):
                return {"status": "unknown", "reason": "missing_or_decreasing_counter"}
            totals[key] += new_value - old_value
    totals["input_tokens"] += totals.pop("cache_read_tokens") + totals.pop("cache_write_tokens")
    return {"status": "known", **totals}


def cli_argv(python, provider, query_file, resume_id=None):
    argv = [
        str(python),
        "-B",
        "-m",
        "hermes_cli.main",
        "--cli",
        "chat",
        "-Q",
        "--query-file",
        str(Path(query_file).resolve()),
        "--model",
        MODEL,
        "--provider",
        provider,
    ]
    if resume_id is not None:
        if not isinstance(resume_id, str) or not _ID.fullmatch(resume_id):
            raise HermesCLIError("exact_observed_resume_id_required")
        argv.extend(("--resume", resume_id))
    return argv


def validate_cli_capture(response, root):
    """Re-read actual bytes/exports, not a self-declared real_host/status flag."""
    if (
        response.get("transport") != TRANSPORT
        or response.get("method_id") != METHOD_ID
        or response.get("fixture_mode") is not False
    ):
        raise HermesCLIError("CLI_method_transport_mismatch")
    query = _read_ref(response["query_input"], root).decode("utf-8")
    stdout = _read_ref(response["stdout"], root).decode("utf-8")
    stderr = _read_ref(response["stderr"], root).decode("utf-8")
    ids = re.findall(r"(?m)^session_id:\s*([A-Za-z0-9._-]+)\s*$", stderr)
    actual = response.get("session_id")
    if not ids or set(ids) != {actual}:
        raise HermesCLIError("CLI_stderr_session_mismatch")
    request = json.loads(_read_ref(response["request_artifact"], root))
    argv = request.get("argv", [])
    if (
        not argv
        or request.get("query_file")
        != str((root / response["query_input"]["path"]).resolve())
        or request.get("query_sha256") != _sha(query.encode())
        or request.get("id") != response.get("request_id")
        or argv
        != cli_argv(
            argv[0],
            response["provider"],
            request.get("query_file"),
            response.get("requested_session_id"),
        )
    ):
        raise HermesCLIError("CLI_actual_argv_input_mismatch")
    exports = response.get("exports", {})
    after = {
        sid: parse_export(_read_ref(ref, root), sid)
        for sid, ref in exports.get("after", {}).items()
    }
    before = {
        sid: parse_export(_read_ref(ref, root), sid)
        for sid, ref in exports.get("before", {}).items()
    }
    lineage = response.get("session_lineage")
    if (
        not isinstance(lineage, list)
        or not lineage
        or len(lineage) > 16
        or len(set(lineage)) != len(lineage)
        or lineage[-1] != actual
        or set(after) != set(lineage)
    ):
        raise HermesCLIError("CLI_physical_lineage_invalid")
    for index, sid in enumerate(lineage):
        parent = after[sid].get("parent_session_id")
        if parent != (lineage[index - 1] if index else None):
            raise HermesCLIError("CLI_parent_lineage_mismatch")
        if index and after[lineage[index - 1]].get("end_reason") != "compression":
            raise HermesCLIError("CLI_noncompression_session_switch")
    requested = response.get("requested_session_id")
    if requested is None and before:
        raise HermesCLIError("fresh_CLI_session_has_previous_history")
    if requested is not None and (requested not in before or requested not in lineage):
        raise HermesCLIError("CLI_resume_not_observed_in_lineage")
    old_ids = {msg.get("id") for row in before.values() for msg in row["messages"]}
    messages = [
        msg
        for sid in lineage
        for msg in after[sid]["messages"]
        if msg.get("id") not in old_ids
    ]
    users = [
        m for m in messages if m.get("role") == "user" and m.get("content") == query
    ]
    assistants = [
        m
        for m in messages
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]
    if not users or not assistants:
        raise HermesCLIError("CLI_actual_query_answer_export_missing")
    model_content = users[-1].get("api_content") or query
    if not isinstance(model_content, str) or (
        model_content != query and not model_content.startswith(query + "\n\n")
    ):
        raise HermesCLIError("CLI_export_api_content_not_bound_to_original_query")
    final = assistants[-1]
    if (
        type(final.get("id")) is not int
        or final["id"] < 1
        or response.get("turn_id") != f"{actual}:message:{final['id']}"
    ):
        raise HermesCLIError("CLI_actual_message_id_missing")
    if (
        _visible_cli_answer(stdout.replace("\r\n", "\n").rstrip("\r\n")) != final["content"]
        or response.get("answer_text") != final["content"]
    ):
        raise HermesCLIError("CLI_stdout_export_answer_mismatch")
    computed = usage_delta(before, after)
    if computed != response.get("export_usage"):
        raise HermesCLIError("CLI_export_usage_mismatch")
    return {
        "session_id": actual,
        "turn_id": response["turn_id"],
        "answer_text": final["content"],
        "export_usage": computed,
        "model_user_content": model_content,
    }


class OwnedHermesCLI(OwnedHermesProcess):
    """Reuse the P11 meter/env/owned-job mechanics, with no A2A gateway."""

    def quiesce(self):
        # On Windows the venv launcher can exit before the Job Object's
        # listening descendant has finished termination. Await socket closure
        # only after stopping our retained tree; never adopt or kill a listener.
        owned_bridge = self.bridge is not None
        port = frozen_meter_port(self.binding) if owned_bridge else None
        super().quiesce()
        if port is None:
            return
        deadline = time.monotonic() + 5
        while True:
            with socket.socket() as sock:
                sock.settimeout(0.25)
                occupied = sock.connect_ex(("127.0.0.1", port)) == 0
            if not occupied:
                return
            if time.monotonic() >= deadline:
                raise HermesCLIError("owned_CLI_meter_listener_not_closed")
            time.sleep(0.05)

    def resume(self):
        if self.bridge is not None and self.bridge.poll() is None:
            return
        from probes.hermes.p11_start_a2a_test import _local_ready

        port = frozen_meter_port(self.binding)
        with socket.socket() as sock:
            # A recently quiesced HTTP listener leaves TIME_WAIT sockets on
            # Windows. Probe a live listener, not bind() without HTTPServer's
            # reuse policy; the owned child's ready log proves actual binding.
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise HermesCLIError("owned_CLI_meter_port_occupied")
        self.generation += 1
        repo = Path(__file__).resolve().parents[2]
        env = self._env()
        env["PYTHONPATH"] = os.pathsep.join((str(repo), env.get("PYTHONPATH", "")))
        command = self._meter_bridge_command(port=port)
        self.bridge = self._spawn(command, cwd=self.root, env=env, label="cli-meter")
        deadline = time.monotonic() + 12
        ready_log = self.archive / f"cli-meter-{self.generation}.log"
        while True:
            if self.bridge.poll() is not None or time.monotonic() >= deadline:
                self.quiesce()
                raise HermesCLIError("CLI_meter_not_ready")
            owned_ready = ready_log.is_file() and any(
                line == json.dumps({"ready": True, "route": "main", "port": port})
                for line in ready_log.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            if owned_ready and _local_ready(f"http://127.0.0.1:{port}/health"):
                break
            time.sleep(0.1)

    def run_process(self, argv, output, *, label, timeout_seconds):
        from scope_recall.runtime.worker_watchdog import _OwnedWindowsJob

        stdout, stderr = output / (label + ".stdout"), output / (label + ".stderr")
        with stdout.open("xb") as out, stderr.open("xb") as err:
            child = subprocess.Popen(
                argv,
                cwd=self.binding["launch_contract"]["working_directory"],
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
            job = _OwnedWindowsJob()
            try:
                job.assign(child)
            except Exception:
                child.kill()
                child.wait(timeout=5)
                job.close()
                raise
            self.jobs.append(job)
            deadline = time.monotonic() + timeout_seconds
            reason = None
            while child.poll() is None:
                if (
                    time.monotonic() >= deadline
                    or max(stdout.stat().st_size, stderr.stat().st_size)
                    > MAX_OUTPUT_BYTES
                ):
                    reason = "timeout_or_output_cap"
                    job.close()
                    child.kill()
                    child.wait(timeout=5)
                    break
                time.sleep(0.05)
        return {
            "pid": child.pid,
            "returncode": child.returncode,
            "error": reason,
            "stdout": stdout,
            "stderr": stderr,
        }

    def settle(self):
        """Observe normal owned work only; no manual drain or additional turn."""
        limit = self.binding["launch_contract"]["post_turn_settle_seconds"]
        if self.binding["arm_id"] != "C":
            return {
                "status": "NATIVE_CLI_CLOSED",
                "allowance_seconds": limit,
                "extra_model_turns": 0,
            }
        deadline = time.monotonic() + limit
        database = Path(self.binding["roots"]["database_path"]).resolve()
        while True:
            try:
                with sqlite3.connect(
                    database.as_uri() + "?mode=ro", uri=True, timeout=0.15
                ) as db:
                    counts = dict(
                        db.execute(
                            "SELECT state,count(*) FROM work_items GROUP BY state"
                        ).fetchall()
                    )
            except sqlite3.Error as exc:
                return {
                    "status": "OBSERVATION_GAP",
                    "error_type": type(exc).__name__,
                    "allowance_seconds": limit,
                }
            if not (counts.get("pending", 0) + counts.get("leased", 0)):
                return {
                    "status": "QUIESCENT",
                    "states": counts,
                    "allowance_seconds": limit,
                    "extra_model_turns": 0,
                }
            if self.pause.lock is not None or time.monotonic() >= deadline:
                return {
                    "status": "HELD_BY_JOURNEY"
                    if self.pause.lock is not None
                    else "TIMEOUT",
                    "states": counts,
                    "allowance_seconds": limit,
                    "extra_model_turns": 0,
                }
            time.sleep(0.2)


_FORMAL_READY = {}


def _formal_ready(formal_config_path):
    from p18_formal_evidence import verify_formal_run_config

    path = Path(formal_config_path).resolve()
    key = (str(path), _sha(path.read_bytes()))
    ready = _FORMAL_READY.get(key)
    if ready is None:
        ready = verify_formal_run_config(path)
        _FORMAL_READY[key] = ready
    return ready


class HermesCLITransport:
    def __init__(self, binding, formal_config_path, budget):
        self.binding = binding
        self.formal_config_path = Path(formal_config_path).resolve()
        self.root = self.formal_config_path.parent
        ready = _formal_ready(self.formal_config_path)
        if (
            not ready.formal_execution_allowed
            or "hermes_method_path" not in ready.details
        ):
            raise HermesCLIError("formal_CLI_method_G2_freeze_required")
        if binding.get("host_id") != METHOD_ID:
            raise HermesCLIError("actual_CLI_binding_required")
        if binding.get("launch_contract", {}).get("post_turn_settle_seconds") != 60:
            raise HermesCLIError("same_frozen_CLI_lifecycle_allowance_required")
        verify_binding(binding)
        config = _json(binding["roots"]["config_path"])
        model = config.get("model", {})
        self.provider = model.get("provider")
        self.meter_port = frozen_meter_port(binding)
        providers = [
            p
            for p in config.get("custom_providers", [])
            if p.get("name") == self.provider
        ]
        if (
            model.get("default") != MODEL
            or len(providers) != 1
            or _meter_port(config) != self.meter_port
            or providers[0].get("key_env") != "SCOPE_RECALL_TEST_LOCAL_BRIDGE_TOKEN"
            or config.get("fallback_model") not in ([], None)
            or config.get("agent", {}).get("api_max_retries") != 0
            or config.get("agent", {}).get("max_turns") != 3
            or model.get("max_tokens") != 4096
            or model.get("context_length") != 131072
            or model.get("streaming") is not False
            or config.get("platform_toolsets")
            != {"cli": ["terminal", "file", "memory"]}
            or config.get("compression", {}).get("enabled") is not False
        ):
            raise HermesCLIError("CLI_all_primary_calls_must_use_frozen_meter")
        workspace = Path(binding["launch_contract"]["working_directory"]).resolve()
        if (
            workspace == Path(binding["roots"]["home_path"]).resolve()
            or Path(config["terminal"]["cwd"]).resolve() != workspace
        ):
            raise HermesCLIError("CLI_actual_workspace_mismatch")
        if binding["arm_id"] == "C":
            candidate = binding.get("source", {}).get("candidate_wheel", {})
            if (
                candidate.get("sha256") != ready.details["wheel_sha256"]
                or _sha(Path(candidate["path"]).read_bytes())
                != ready.details["wheel_sha256"]
            ):
                raise HermesCLIError("CLI_candidate_wheel_not_frozen_in_G2")
            site = Path(binding["loader"]["module_search_path"])
            with zipfile.ZipFile(candidate["path"]) as wheel:
                for name in wheel.namelist():
                    if name.startswith("scope_recall/") and not name.endswith("/"):
                        installed = (site / name).resolve()
                        if not installed.is_relative_to(
                            site.resolve()
                        ) or installed.read_bytes() != wheel.read(name):
                            raise HermesCLIError(
                                "CLI_installed_candidate_bytes_mismatch"
                            )
            manifest = binding["installation_manifest"]
            if (
                _sha(Path(manifest["manifest_path"]).read_bytes())
                != manifest["manifest_sha256"]
                or _json(manifest["manifest_path"]) != manifest["payload"]
            ):
                raise HermesCLIError("CLI_installation_manifest_changed")
            audiences = (
                binding.get("installation_manifest", {})
                .get("payload", {})
                .get("audiences", [])
            )
            if not any(
                all(
                    a.get(k) == v
                    for k, v in {
                        "platform": "cli",
                        "chat_type": "cli",
                        "chat_id": "local",
                        "thread_id": "main",
                        "agent_workspace": "hermes",
                    }.items()
                )
                for a in audiences
            ):
                raise HermesCLIError("CLI_local_manifest_audience_required")
        self.budget = budget
        self.owner = OwnedHermesCLI(
            binding, formal_config_path=self.formal_config_path, context_id="local"
        )
        self.python = Path(binding["fixed_host"]["runtime_python_path"])
        self.config = type("CLIConfig", (), {"cwd": workspace})()

    def _exports(self, session_id, output, label):
        refs, rows = {}, {}
        current = session_id
        while current:
            if not _ID.fullmatch(current) or current in rows or len(rows) >= 16:
                raise HermesCLIError("CLI_export_lineage_invalid")
            path = output / f"{label}-{len(rows)}.jsonl"
            argv = [
                str(self.python),
                "-B",
                "-m",
                "hermes_cli.main",
                "sessions",
                "export",
                str(path),
                "--format",
                "jsonl",
                "--session-id",
                current,
            ]
            process = self.owner.run_process(
                argv, output, label=f"{label}-export-{len(rows)}", timeout_seconds=20
            )
            if process["returncode"] != 0 or process["error"]:
                raise HermesCLIError("official_CLI_export_failed")
            row = parse_export(path.read_bytes(), current)
            refs[current] = _ref(path, self.root)
            rows[current] = row
            current = row.get("parent_session_id")
        return refs, rows

    def execute(self, query, *, operation_id, request_id, session_id, output):
        verify_binding(self.binding)
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        query_file = output / "query.txt"
        with query_file.open("xb") as handle:
            handle.write(query.encode("utf-8"))
        before_refs, before = (
            self._exports(session_id, output, "before") if session_id else ({}, {})
        )
        argv = cli_argv(self.python, self.provider, query_file, session_id)
        request = {
            "id": request_id,
            "argv": argv,
            "query_file": str(query_file),
            "query_sha256": _sha(query.encode()),
        }
        request_file = output / "cli-request.json"
        request_file.write_bytes(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self.budget.set_operation(
            operation_id=operation_id,
            request_id=request_id,
            task_id=operation_id,
            context_id="cli-local",
        )
        reservation = self.budget.reserve(MODEL, request_file.read_bytes())
        result = {
            "schema": "scope-recall.p18-hermes-cli.v1",
            "transport": TRANSPORT,
            "method_id": METHOD_ID,
            "provider": self.provider,
            "formal_evaluation": True,
            "fixture_mode": False,
            "status": "FAILED",
            "transport_status": "FAILED",
            "request": request,
            "request_id": request_id,
            "requested_session_id": session_id,
            "request_sha256": _sha(request_file.read_bytes()),
            "request_artifact": _ref(request_file, self.root),
            "query_input": _ref(query_file, self.root),
            "errors": [],
            "retry_count": 0,
        }
        try:
            process = self.owner.run_process(
                argv, output, label="chat", timeout_seconds=_chat_timeout_seconds()
            )
            result["process"] = {k: process[k] for k in ("pid", "returncode", "error")}
            result["stdout"], result["stderr"] = (
                _ref(process["stdout"], self.root),
                _ref(process["stderr"], self.root),
            )
            ids = re.findall(
                rb"(?m)^session_id:\s*([A-Za-z0-9._-]+)\s*$",
                process["stderr"].read_bytes(),
            )
            if (
                process["returncode"] != 0
                or process["error"]
                or not ids
                or len(set(ids)) != 1
            ):
                raise HermesCLIError("actual_CLI_completion_failed")
            actual = ids[-1].decode()
            refs, after = self._exports(actual, output, "after")
            lineage = list(reversed(list(after)))
            # Windows text stdout translates LF to CRLF; public JSON export
            # retains model content LF. Normalize only the stream newline.
            answer = _visible_cli_answer(
                process["stdout"].read_bytes().decode("utf-8").replace("\r\n", "\n").rstrip("\r\n")
            )
            final = [
                m
                for m in after[actual]["messages"]
                if m.get("role") == "assistant" and m.get("content") == answer
            ]
            if not final:
                raise HermesCLIError("actual_CLI_final_message_missing")
            result.update(
                {
                    "session_id": actual,
                    "session_lineage": lineage,
                    "turn_id": f"{actual}:message:{final[-1]['id']}",
                    "task_id": f"{actual}:message:{final[-1]['id']}",
                    "answer_text": answer,
                    "exports": {"before": before_refs, "after": refs},
                    "export_usage": usage_delta(before, after),
                }
            )
            validate_cli_capture(result, self.root)
            result["status"] = result["transport_status"] = "COMPLETED"
        except Exception as exc:
            result["errors"].append(
                {
                    "kind": "CLI",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:160],
                }
            )
        finally:
            try:
                self.budget.finish(reservation, result["status"], None)
                result["formal_usage"] = self.budget.formal_usage()
                result["provider_response_archives"] = [
                    _ref(path, self.root) for path in self.budget._finished_archives
                ]
                if result.get("export_usage", {}).get("api_call_count") != len((result["formal_usage"] or {}).get("entries", [])):
                    result["host_export_accounting_note"] = "Host export omits some provider calls; formal validation must prove exact iteration-summary attribution. Full provider ledger usage is retained."
            except Exception as exc:
                # Retain the actual process/input/export artifacts even when
                # ledger reconciliation fails after a potentially charged call.
                result["status"] = result["transport_status"] = "FAILED"
                result["errors"].append(
                    {"kind": "accounting", "error_type": type(exc).__name__}
                )
                result["formal_usage"] = None
        result["post_turn_worker_state"] = self.owner.settle()
        return result
