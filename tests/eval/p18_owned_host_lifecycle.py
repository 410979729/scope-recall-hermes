"""Owned TEST gateway and worker quiescence for P18; no import-time processes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time


class OwnedHostError(RuntimeError):
    pass


FROZEN_HERMES_ATTEMPT_AUTHORIZATION = (
    Path(__file__).resolve().parents[2].parents[1]
    / ".execution"
    / "budget-authorization-20260908-hermes-turn-v1.json"
)
FROZEN_HERMES_ATTEMPT_AUTHORIZATION_SHA256 = (
    "4d9e223c8176ad127294f2e0289594efb099d0fc8f7208f4337413edd8e94570"
)
_HERMES_AUTH_PATH = "SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION"
_HERMES_AUTH_SHA = "SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION_SHA256"


def ensure_frozen_hermes_attempt_authorization(env=None):
    """Bind the frozen Hermes 4340 cover when the parent shell omitted it."""
    target = os.environ if env is None else env
    path_value = target.get(_HERMES_AUTH_PATH)
    hash_value = target.get(_HERMES_AUTH_SHA)
    if path_value or hash_value:
        return dict(target)
    path = FROZEN_HERMES_ATTEMPT_AUTHORIZATION
    if not path.is_file():
        return dict(target)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != FROZEN_HERMES_ATTEMPT_AUTHORIZATION_SHA256:
        raise OwnedHostError("frozen_hermes_attempt_authorization_hash_mismatch")
    target[_HERMES_AUTH_PATH] = str(path)
    target[_HERMES_AUTH_SHA] = digest
    return dict(target)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _inside(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root) or "test" not in str(root).lower():
        raise OwnedHostError("owned_TEST_path_required")
    return path


class WorkerPause:
    """The existing worker lock pauses drains, including later hook launches."""

    def __init__(self, data_directory=None):
        self.data_directory = Path(data_directory) if data_directory else None
        self.lock = None

    def acquire(self):
        if self.data_directory is None or self.lock is not None:
            return
        from scope_recall.file_lock import advisory_file_lock
        lock = advisory_file_lock(self.data_directory / "runtime-worker.lock", timeout_seconds=3)
        lock.__enter__()
        self.lock = lock

    def close(self):
        if self.lock is not None:
            self.lock.__exit__(None, None, None)
            self.lock = None


class OwnedHermesProcess:
    """Start the same official gateway and P11 metered bridge as the probe.

    Only retained Popen children are stopped. Windows Job Objects also cover
    their worker/native descendants. No external PID is adopted or killed.
    Credentials use the already authorized probe loader and stay in memory.
    """

    def __init__(self, binding, *, formal_config_path, context_id, zero_model=False):
        self.binding = binding
        roots = binding["roots"]
        self.root = Path(roots["binding_root"]).resolve()
        self.home = _inside(self.root, roots["home_path"])
        self.archive = self.root / "archive"
        self.archive.mkdir(exist_ok=True)
        self.context_id = context_id
        self.config_path = Path(formal_config_path).resolve()
        self.zero_model = zero_model
        self.gateway = self.bridge = None
        self.jobs = []
        self.logs = []
        self.pause = WorkerPause(Path(roots["database_path"]).parent if binding["arm_id"] == "C" else None)
        self.runtime = _read(roots["runtime_config_path"]) if binding["arm_id"] == "C" else None
        self._environment = None
        self._legacy_embedding_meter = None
        # A zero-model health phase may already have owned bridge logs in
        # this binding. Preserve them and allocate the next launch identity.
        existing_generations = []
        for log in self.archive.glob("*.log"):
            suffix = log.stem.rsplit("-", 1)[-1]
            if suffix.isdigit():
                existing_generations.append(int(suffix))
        self.generation = max(existing_generations, default=0)

    def _meter_bridge_command(self, *, port=None):
        """Same P11 meter argv for A2A and CLI; ledger comes from formal config."""
        repo = Path(__file__).resolve().parents[2]
        freeze = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        command = [
            sys.executable, "-B", str(repo / "probes/hermes/p11_a2a_bridge.py"),
            "--state", str(self.root), "--formal-active-operation",
            "--formal-freeze-sha256", freeze,
            "--formal-config", str(self.config_path),
        ]
        if port is not None:
            command.extend(["--port", str(port)])
        runtime_raw = (self.binding.get("roots") or {}).get("runtime_config_path")
        if runtime_raw:
            runtime = _inside(self.root, runtime_raw) if Path(runtime_raw).resolve().is_relative_to(self.root) else Path(runtime_raw).resolve()
            if "test" not in str(runtime).lower():
                raise OwnedHostError("owned_TEST_path_required")
            command.extend([
                "--runtime-config", str(runtime),
                "--runtime-config-sha256", hashlib.sha256(runtime.read_bytes()).hexdigest(),
            ])
        if self.zero_model:
            command.append("--zero-model-diagnostic")
        return command

    def _env(self):
        if self._environment is not None:
            return dict(self._environment)
        from probes.hermes.p11_start_a2a_test import _resolve_upstream_key
        from probes.hermes.p11_a2a_testkit import LOCAL_BRIDGE_TOKEN_ENV, UPSTREAM_KEY_ENV
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        # Hash-bound formal TEST budget only.  The owned child whitelist
        # otherwise drops these, so the bridge falls back to the conservative
        # 8M output cap and rejects reservations against the original ledger.
        ensure_frozen_hermes_attempt_authorization()
        for key in (
            "SCOPE_RECALL_P18_BUDGET_CONFIG",
            "SCOPE_RECALL_P18_BUDGET_SHA256",
            "SCOPE_RECALL_P11_MAX_MODEL_POSTS",
            _HERMES_AUTH_PATH,
            _HERMES_AUTH_SHA,
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
        # The formal TEST launcher resolves this credential in memory. Keep it
        # available to the owned embedding worker without persisting it in a
        # binding file or copying unrelated account environment variables.
        embedding_key = os.environ.get("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY")
        if embedding_key:
            env["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"] = embedding_key
        env.update(_read(self.binding["roots"]["environment_path"]))
        legacy = self.binding.get("legacy_embedding_meter")
        if legacy is not None and self._legacy_embedding_meter is None:
            from p18_legacy_embedding_meter import LegacyEmbeddingMeter
            policy_path = Path(legacy["budget_path"])
            if hashlib.sha256(policy_path.read_bytes()).hexdigest() != legacy["budget_sha256"]:
                raise OwnedHostError("legacy_embedding_budget_hash_mismatch")
            baseline_config = Path(legacy["config_path"])
            if hashlib.sha256(baseline_config.read_bytes()).hexdigest() != legacy["config_sha256"]:
                raise OwnedHostError("legacy_embedding_config_hash_mismatch")
            token = secrets.token_urlsafe(32)
            meter = LegacyEmbeddingMeter(ledger_path=legacy["ledger_path"], policy=_read(policy_path),
                output=self.archive/"legacy-embedding", key="" if self.zero_model else (embedding_key or ""), token=token)
            meter_port = legacy["port"]
            if type(meter_port) is not int or not 29990 <= meter_port <= 30200:
                raise OwnedHostError("legacy_embedding_meter_port_invalid")
            meter.start(meter_port)
            self._legacy_embedding_meter = meter
            env["SCOPE_RECALL_TEST_B_EMBEDDING_TOKEN"] = token
            env["SCOPE_RECALL_TEST_B_EMBEDDING_BASE_URL"] = "http://127.0.0.1:" + str(meter_port) + "/" + token + "/v1"
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            path = self.root / "environment" / name.lower()
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        env.update({"HERMES_HOME": str(self.home), "SCOPE_RECALL_P11_STATE": str(self.root),
                    "SCOPE_RECALL_P11_TEST_CONTEXT": self.context_id,
                    "SCOPE_RECALL_P11_HERMES_ROOT": self.binding["fixed_host"]["source"]["path"],
                    "SCOPE_RECALL_P11_HERMES_PYTHON": self.binding["fixed_host"]["runtime_python_path"],
                    "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "localhost,127.0.0.1",
                    LOCAL_BRIDGE_TOKEN_ENV: secrets.token_urlsafe(32),
                    UPSTREAM_KEY_ENV: _resolve_upstream_key(zero_model_diagnostic=self.zero_model)})
        self._environment = dict(env)
        return env

    def _spawn(self, command, *, cwd, env, label):
        from scope_recall.runtime.worker_watchdog import _OwnedWindowsJob
        path = self.archive / f"{label}-{self.generation}.log"
        log = path.open("xb")
        self.logs.append(log)
        job = _OwnedWindowsJob()
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                 start_new_session=os.name != "nt")
        try:
            job.assign(child)
        except Exception:
            child.kill()
            child.wait(timeout=5)
            job.close()
            raise
        self.jobs.append(job)
        return child

    def resume(self):
        if self.gateway is not None and self.gateway.poll() is None:
            return
        from probes.hermes.p11_start_a2a_test import _gateway_log_offset, _gateway_processing_ready, _local_ready
        # Do not use another TEST run's bridge/card just because its port answers.
        for port in (19921, 29991):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise OwnedHostError("owned_TEST_port_occupied") from exc
        self.generation += 1
        env = self._env()
        repo = Path(__file__).resolve().parents[2]
        bridge_env = dict(env)
        bridge_env["PYTHONPATH"] = os.pathsep.join((str(repo), env.get("PYTHONPATH", "")))
        command = self._meter_bridge_command()
        log_path = self.home / "logs/gateway.log"
        offset = _gateway_log_offset(log_path)
        try:
            self.bridge = self._spawn(command, cwd=self.root, env=bridge_env, label="bridge")
            deadline = time.monotonic() + 12
            while not _local_ready("http://127.0.0.1:29991/health"):
                if self.bridge.poll() is not None or time.monotonic() >= deadline:
                    raise OwnedHostError("owned_bridge_start_failed")
                time.sleep(.2)
            self.gateway = self._spawn(self.binding["launch_contract"]["argv"],
                                       cwd=self.binding["launch_contract"]["working_directory"], env=env, label="gateway")
            deadline = time.monotonic() + 60
            while True:
                if self.gateway.poll() is not None or time.monotonic() >= deadline:
                    raise OwnedHostError("owned_gateway_processing_not_ready")
                if _local_ready("http://127.0.0.1:19921/.well-known/agent-card.json") and _gateway_processing_ready(log_path, offset):
                    break
                time.sleep(.25)
            receipt = {"gateway_pid": self.gateway.pid, "bridge_pid": self.bridge.pid,
                       "generation": self.generation, "gateway_readiness_log_offset": offset,
                       "processing_ready": True, "model_calls_by_controller": 0}
            (self.archive / f"owned-start-{self.generation}.json").write_bytes(json.dumps(receipt).encode())
        except Exception:
            self.quiesce()
            raise

    def quiesce(self):
        from probes.hermes.p11_start_a2a_test import _terminate_owned
        # Stop complete owned trees before closing foreground SQLite/Lance.
        for job in self.jobs:
            job.close()
        self.jobs.clear()
        for child in (self.gateway, self.bridge):
            _terminate_owned(child)
        self.gateway = self.bridge = None
        for log in self.logs:
            log.close()
        self.logs.clear()

    def quiesce_workers(self):
        self.quiesce()
        self.pause.acquire()
        self.resume()

    def close(self):
        self.quiesce()
        self.pause.close()
        if self._legacy_embedding_meter is not None:
            self._legacy_embedding_meter.close()
            self._legacy_embedding_meter = None
        self._environment = None
