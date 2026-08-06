"""Install, upgrade, verify, and rollback helpers for copying Scope Recall into a Hermes home.

Installer operations are designed around dry-run evidence, backups, and explicit rollback commands."""

from __future__ import annotations

import argparse
import fnmatch
import gc
import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .activation_transaction import (
    ActivationSnapshotError,
    capture_activation_state,
    committed_activation_receipt,
    compensate_activation_failure,
    refresh_activation_sqlite_epoch,
)
from .capture_filters import sanitize_report_text
from .config import load_runtime_config, load_runtime_config_errors
from .gating import config_bool
from .installer_yaml import (
    InstallerYamlError,
    atomic_replace_text,
    memory_provider_from_yaml,
    set_memory_provider_yaml_text,
)
from .response_schemas import (
    DOCTOR_REQUIRED_CHECK_NAMES,
    DOCTOR_RESPONSE_SCHEMA_VERSION,
)
from .recovery_commands import quote_argument, restore_file_command
from .vector_bootstrap import vector_companion_presence
from .vector_generation_preflight import validate_generation_for_activation
from .vector_store import normalize_vector_backend
from .windows_filesystem import (
    copy_file,
    copy_tree as filesystem_copy_tree,
    io_path,
    make_dirs,
    move_path,
    path_exists,
    path_is_dir,
    path_is_file,
    path_is_symlink,
    public_path,
    remove_path,
)

PLUGIN_NAME = "scope-recall"
PROVIDER_CONFIG_COMMAND = f"hermes config set memory.provider {PLUGIN_NAME}"
REQUIRED_PLUGIN_FILES = (
    "__init__.py",
    "provider.py",
    "plugin.yaml",
    "config.json",
)
_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hermes-agent-src",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "tests",
    "venv",
    "lancedb",
    "lancepro",
    "scope-recall",
    "backups",
    "htmlcov",
}
_EXCLUDED_FILE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.egg-info",
    ".coverage",
    ".env",
    ".env.*",
    "review-report.*.md",
)
_DOCTOR_STDOUT_LIMIT_BYTES = 1_000_000
_DOCTOR_STDERR_LIMIT_BYTES = 1_000_000


class _BoundedPipeCapture:
    """Continuously drain one child stream while retaining only bounded bytes.

    A process-wide ``RLIMIT_FSIZE`` cannot safely bound redirected doctor
    output: the same limit also applies to SQLite snapshots and every other
    regular file written by the doctor.  Pipe backpressure plus a dedicated
    reader keeps memory and disk bounded without changing the child's file
    semantics.
    """

    def __init__(self, limit: int, *, stream_name: str) -> None:
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        self.writer = os.fdopen(write_fd, "wb")
        self._limit = int(limit)
        self._buffer = bytearray()
        self._total_bytes = 0
        self._reader_error = False
        self._thread = threading.Thread(
            target=self._drain,
            name=f"scope-recall-doctor-{stream_name}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        try:
            with os.fdopen(self._read_fd, "rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    self._total_bytes += len(chunk)
                    remaining = self._limit + 1 - len(self._buffer)
                    if remaining > 0:
                        self._buffer.extend(chunk[:remaining])
        except Exception:
            self._reader_error = True

    def finish(self) -> None:
        if not self.writer.closed:
            self.writer.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive() or self._reader_error:
            raise RuntimeError("doctor output capture failed")

    @property
    def exceeded(self) -> bool:
        return self._total_bytes >= self._limit

    @property
    def payload(self) -> bytes:
        return bytes(self._buffer)


def _register_activation_sqlite_epoch(snapshot: dict[str, Any]) -> str:
    """Refresh the activation epoch owned by the explicitly supplied snapshot."""

    return refresh_activation_sqlite_epoch(snapshot)


class InstallError(RuntimeError):
    """Raised when the scope-recall installer cannot safely proceed."""


def _platform_default_hermes_home() -> Path:
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def resolve_hermes_home(hermes_home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the target Hermes home without importing Hermes runtime code."""
    raw = str(hermes_home or os.environ.get("HERMES_HOME") or _platform_default_hermes_home())
    return Path(raw).expanduser().resolve()


def source_root() -> Path:
    """Return the package/plugin source tree copied into Hermes plugins."""
    return Path(__file__).resolve().parent


def plugin_dir_for(hermes_home: str | os.PathLike[str] | None = None) -> Path:
    return resolve_hermes_home(hermes_home) / "plugins" / PLUGIN_NAME


def _read_manifest_name(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.exists():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def _read_manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.exists():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def _clear_runtime_verify_modules(package_name: str) -> None:
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)


def _load_installed_package(plugin_dir: Path, *, package_name: str = "_scope_recall_runtime_verify") -> Any:
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        raise InstallError(f"installed plugin is missing __init__.py: {plugin_dir}")
    _clear_runtime_verify_modules(package_name)
    spec = importlib.util.spec_from_file_location(package_name, init_file, submodule_search_locations=[str(plugin_dir)])
    if spec is None or spec.loader is None:
        raise InstallError(f"cannot build import spec for installed plugin: {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_verify(home: Path, plugin_dir: Path) -> dict[str, Any]:
    """Verify an installed Scope Recall copy against a Hermes home.

    Runtime verification checks importability and basic commands without performing upgrade or repair mutations."""
    payload: dict[str, Any] = {
        "requested": True,
        "provider_loaded": False,
        "config_schema_keys": [],
        "tool_schema_names": [],
        "sqlite_schema_current": False,
        "vector_companion": {"enabled": None, "configured_backend": "", "status": "unknown"},
        "failures": [],
    }
    failures: list[str] = []
    package_name = "_scope_recall_runtime_verify"
    try:
        try:
            _load_installed_package(plugin_dir, package_name=package_name)
            provider_module = importlib.import_module(f"{package_name}.provider")
            provider = provider_module.ScopeRecallMemoryProvider()
            setattr(provider, "_hermes_home", home)
            payload["provider_loaded"] = True
            payload["config_schema_keys"] = [str(item.get("key") or "") for item in provider.get_config_schema()]
            tool_names = [str(schema.get("name") or "") for schema in provider.get_tool_schemas()]
            payload["tool_schema_names"] = tool_names
            required_tools = {
                "scope_recall_store",
                "scope_recall_search",
                "scope_recall_context",
                "scope_recall_profile",
                "scope_recall_memory",
                "scope_recall_entity",
            }
            missing_tools = sorted(required_tools - set(tool_names))
            if missing_tools:
                failures.append(f"runtime tool schemas missing compact defaults: {', '.join(missing_tools)}")
            try:
                config_module = importlib.import_module(f"{package_name}.config")
                storage_dir = home / "scope-recall"
                runtime_config = config_module.load_runtime_config(plugin_dir, storage_dir)
                config_errors = config_module.load_runtime_config_errors(runtime_config)
                payload["config_load_errors"] = config_errors
                if config_errors:
                    failures.extend(
                        "runtime config load failed: "
                        + str(item.get("message") or item.get("kind") or "invalid config")
                        for item in config_errors
                    )
                vector_config = dict((runtime_config or {}).get("vector") or {})
                vector_enabled = bool(vector_config.get("enabled", False))
                configured_backend = str(vector_config.get("backend") or "").strip().lower()
                vector_path = storage_dir / ("vector.sqlite3" if configured_backend == "sqlite-bruteforce" else "lancedb")
                if not vector_enabled:
                    vector_status = "disabled"
                elif vector_path.exists():
                    vector_status = "ready"
                else:
                    vector_status = "not_initialized"
                payload["vector_companion"] = {
                    "enabled": vector_enabled,
                    "configured_backend": configured_backend,
                    "fallback_backend": str(vector_config.get("fallback_backend") or "").strip().lower(),
                    "status": vector_status,
                    "path": str(vector_path),
                }
            except Exception as exc:
                payload["vector_companion"] = {"enabled": None, "configured_backend": "", "status": "degraded", "error": str(exc)}
        except Exception as exc:
            failures.append(f"provider runtime load failed: {exc}")

        db_path = home / "scope-recall" / "memory.sqlite3"
        payload["sqlite_path"] = str(db_path)
        if not db_path.is_file():
            failures.append(f"SQLite truth DB missing: {db_path}; run `hermes memory setup` first")
        else:
            try:
                sql_store = importlib.import_module(f"{package_name}.sql_store")
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                try:
                    schema_status = sql_store.schema_migration_status(conn)
                    vector_generation = importlib.import_module(
                        f"{package_name}.vector_generation"
                    )
                    manifest = vector_generation.current_generation(conn)
                    if manifest is not None:
                        active_backend = str(
                            manifest.get("backend") or ""
                        ).strip().lower()
                        generation_root = vector_generation.resolve_generation_storage_root(
                            storage_dir,
                            manifest.get("storage_path"),
                        )
                        if active_backend == "sqlite-bruteforce":
                            active_path = generation_root / "vector.sqlite3"
                            physical_ready = active_path.is_file()
                        elif active_backend == "lancedb":
                            active_path = generation_root / "lancedb"
                            physical_ready = active_path.is_dir()
                        else:
                            # Remote backends have no local path to probe here;
                            # the canonical doctor performs connectivity checks.
                            active_path = generation_root
                            physical_ready = active_backend == "pgvector"
                        active_status = str(manifest.get("status") or "")
                        payload["vector_companion"] = {
                            **dict(payload.get("vector_companion") or {}),
                            "active_backend": active_backend,
                            "generation_id": str(
                                manifest.get("generation_id") or ""
                            ),
                            "generation_status": active_status,
                            "status": (
                                "ready"
                                if active_status == "active" and physical_ready
                                else "degraded"
                            ),
                            "path": str(active_path),
                        }
                finally:
                    conn.close()
                payload["schema_migrations"] = schema_status
                payload["sqlite_schema_current"] = bool(schema_status.get("current"))
                if not payload["sqlite_schema_current"]:
                    failures.append("SQLite schema migration ledger is not current")
            except Exception as exc:
                failures.append(f"SQLite runtime schema check failed: {exc}")
        payload["failures"] = failures
        payload["ok"] = not failures
        return payload
    finally:
        _clear_runtime_verify_modules(package_name)


def _has_discovery_marker(plugin_dir: Path) -> bool:
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return False
    source = init_file.read_text(encoding="utf-8", errors="replace")[:8192]
    return "register_memory_provider" in source or "MemoryProvider" in source


def _is_same_tree(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _should_skip_entry(directory: str, name: str) -> bool:
    candidate = Path(directory) / name
    if candidate.is_symlink():
        return True
    if name in _EXCLUDED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _EXCLUDED_FILE_GLOBS)


def _make_copied_directories_owner_writable(destination: Path) -> None:
    """Normalize copied directory modes for staging, cleanup, and runtime use."""

    # Windows ACLs, not POSIX mode bits, govern writability.  Avoid traversing
    # a deep copied tree through ordinary Path strings after the safe copy.
    if os.name == "nt":
        return
    directories = [destination]
    directories.extend(path for path in destination.rglob("*") if path.is_dir())
    for directory in directories:
        mode = stat.S_IMODE(directory.stat().st_mode)
        directory.chmod(mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if _should_skip_entry(directory, name)}

    filesystem_copy_tree(source, destination, ignore=ignore, symlinks=False)
    _make_copied_directories_owner_writable(destination)


def _copy_existing_plugin(source: Path, destination: Path) -> None:
    if path_is_symlink(source) or path_is_file(source):
        copy_file(source, destination, follow_symlinks=False)
    else:
        filesystem_copy_tree(source, destination, symlinks=True)


def _remove_existing_plugin(path: Path) -> None:
    remove_path(path, missing_ok=True)


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


_BACKUP_CATEGORY_DIRS = {
    "scope-recall-installer": "i",
    "scope-recall-rollback-current": "r",
    "scope-recall-installer-config": "c",
}


def _backup_root(home: Path, category: str) -> Path:
    """Return a short collision-resistant backup root without creating it."""

    lane = _BACKUP_CATEGORY_DIRS.get(category, "x")
    return home / "backups" / "sr" / lane / f"{_backup_stamp()}.{uuid.uuid4().hex[:8]}"


def _backup_existing_plugin(
    home: Path,
    plugin_dir: Path,
    *,
    category: str,
    pre_mutation_check: Callable[[], None] | None = None,
) -> Path:
    # Keep the final READY epoch check inside the first backup operation.  A
    # wrapper that observes or delays this boundary cannot mutate the vector
    # epoch and then enter the backup body without being revalidated first.
    if pre_mutation_check is not None:
        pre_mutation_check()
    backup_root = _backup_root(home, category)
    backup_path = backup_root / PLUGIN_NAME
    try:
        _copy_existing_plugin(plugin_dir, backup_path)
    except Exception:
        remove_path(backup_root, missing_ok=True, ignore_errors=True)
        raise
    return backup_path


def _validate_backup_dir(backup_dir: Path) -> str:
    if not path_exists(backup_dir):
        return f"rollback backup missing: {backup_dir}"
    if not path_is_dir(backup_dir):
        return f"rollback backup is not a directory: {backup_dir}"
    if _read_manifest_name(backup_dir) != PLUGIN_NAME:
        return f"rollback backup plugin.yaml is not {PLUGIN_NAME}: {backup_dir}"
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not path_is_file(backup_dir / rel)]
    if missing:
        return f"rollback backup missing required files: {', '.join(missing)}"
    return ""


def _rollback_command(home: Path, backup_path: str) -> str:
    if not backup_path:
        return ""
    return f"hermes-scope-recall rollback --hermes-home {_shell_quote_path(home)} --backup-dir {_shell_quote_path(Path(backup_path))}"


def _config_restore_command(home: Path, backup_path: str, *, previous_config_existed: bool) -> str:
    config_path = home / "config.yaml"
    return restore_file_command(
        config_path,
        backup_path=Path(backup_path) if backup_path else None,
        preexisting=previous_config_existed,
    )


def _shell_quote_path(path: Path) -> str:
    return quote_argument(path)


def _config_backup_path(home: Path) -> Path:
    return _backup_root(home, "scope-recall-installer-config") / "config.yaml"


def _set_memory_provider_yaml_text(text: str) -> tuple[str, bool]:
    """Set memory.provider only when the YAML edit is unambiguous and lossless."""

    try:
        return set_memory_provider_yaml_text(text)
    except InstallerYamlError as exc:
        raise InstallError(str(exc)) from exc


def _write_memory_provider_config(home: Path) -> dict[str, Any]:
    """Activate Scope Recall in Hermes config.yaml and preserve rollback evidence."""
    config_path = home / "config.yaml"
    config_source = (
        config_path.resolve(strict=False) if config_path.is_symlink() else config_path
    )
    if config_path.is_symlink() and not config_source.is_file():
        raise InstallError(f"config symlink target is not a regular file: {config_source}")
    previous_config_existed = config_source.is_file()
    before = (
        config_source.read_text(encoding="utf-8", errors="strict")
        if previous_config_existed
        else ""
    )
    after, changed = _set_memory_provider_yaml_text(before)
    backup_path = ""
    if changed:
        home.mkdir(parents=True, exist_ok=True)
        if previous_config_existed:
            backup = _config_backup_path(home)
            copy_file(config_source, backup, follow_symlinks=False)
            backup_path = str(backup)
        try:
            atomic_replace_text(
                config_path,
                after,
                expected_before=before,
            )
        except InstallerYamlError as exc:
            raise InstallError(str(exc)) from exc
    return {
        "config_path": str(config_path),
        "config_updated": changed,
        "previous_config_existed": previous_config_existed,
        "config_backup_path": backup_path,
        "config_rollback_command": _config_restore_command(home, backup_path, previous_config_existed=previous_config_existed) if changed else "",
    }


def _bootstrap_installed_provider(
    home: Path,
    plugin_dir: Path,
    *,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    package_name = "_scope_recall_install_activate"
    lease = dict(snapshot.get("maintenance_lease") or {})
    lease_token = str(lease.get("token") or "")
    if not lease_token:
        raise InstallError("activation snapshot is missing its connection capability")
    try:
        _load_installed_package(plugin_dir, package_name=package_name)
        provider_module = importlib.import_module(f"{package_name}.provider")
        provider = provider_module.ScopeRecallMemoryProvider()
        # The installer passes the lease token only to the one bootstrap
        # connection it owns. No process-, thread-, or context-global authority
        # is visible to ordinary provider connections.
        provider.save_config(
            {},
            str(home),
            activation_lease_token=lease_token,
        )
        _register_activation_sqlite_epoch(snapshot)
    finally:
        _clear_runtime_verify_modules(package_name)
    runtime_verify = verify(home, runtime=True)
    raw_runtime = runtime_verify.get("runtime")
    runtime_payload: dict[str, Any] = raw_runtime if isinstance(raw_runtime, dict) else {}
    return {
        "runtime_verify": runtime_verify,
        "sqlite_schema_current": bool(runtime_payload.get("sqlite_schema_current")),
        "sqlite_path": str(runtime_payload.get("sqlite_path") or home / "scope-recall" / "memory.sqlite3"),
    }


def _postdeploy_doctor_verify(home: Path, plugin_dir: Path) -> dict[str, Any]:
    """Run the installed candidate's canonical operator doctor before commit.

    The subprocess uses isolated Python import semantics so another installed
    Scope Recall package cannot shadow ``plugin_dir``. Only bounded, sanitized
    health metadata is returned; doctor runtime details may contain private
    memory samples and therefore are intentionally not embedded in installer
    output.
    """

    doctor_script = plugin_dir / "scripts" / "doctor.py"
    if not doctor_script.is_file():
        return {
            "requested": True,
            "ok": False,
            "returncode": None,
            "failed_checks": ["doctor_script"],
            "failures": ["installed candidate doctor script is missing"],
            "checks": {},
            "recommendations": [],
        }
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    stdout_capture = _BoundedPipeCapture(
        _DOCTOR_STDOUT_LIMIT_BYTES,
        stream_name="stdout",
    )
    stderr_capture = _BoundedPipeCapture(
        _DOCTOR_STDERR_LIMIT_BYTES,
        stream_name="stderr",
    )
    completed: subprocess.CompletedProcess[Any] | None = None
    process_error: Exception | None = None
    timed_out = False
    capture_error: Exception | None = None
    stdout_capture.start()
    stderr_capture.start()
    try:
        # Dedicated readers continuously drain both pipes so child output can
        # neither deadlock nor grow an in-memory/disk capture without bound.
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(doctor_script),
                "--source-root",
                str(plugin_dir),
                "--hermes-home",
                str(home),
                "--json",
            ],
            cwd=plugin_dir,
            env=env,
            stdout=stdout_capture.writer,
            stderr=stderr_capture.writer,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except Exception as exc:
        process_error = exc
    finally:
        for capture in (stdout_capture, stderr_capture):
            try:
                capture.finish()
            except Exception as exc:
                if capture_error is None:
                    capture_error = exc

    if timed_out:
        return {
            "requested": True,
            "ok": False,
            "returncode": None,
            "timed_out": True,
            "schema_version": "",
            "failed_checks": ["doctor_process"],
            "failures": ["candidate doctor process timed out"],
            "checks": {},
            "recommendations": [],
        }
    if process_error is not None or capture_error is not None:
        exc = process_error or capture_error
        assert exc is not None
        safe_error = sanitize_report_text(str(exc))[:500] or type(exc).__name__
        return {
            "requested": True,
            "ok": False,
            "returncode": None,
            "schema_version": "",
            "failed_checks": ["doctor_process"],
            "failures": [f"candidate doctor process failed: {safe_error}"],
            "checks": {},
            "recommendations": [],
        }
    assert completed is not None
    if stdout_capture.exceeded or stderr_capture.exceeded:
        return {
            "requested": True,
            "ok": False,
            "returncode": int(completed.returncode),
            "schema_version": "",
            "failed_checks": ["doctor_output"],
            "failures": ["candidate doctor exceeded its output limit"],
            "checks": {},
            "recommendations": [],
        }
    stdout_bytes = stdout_capture.payload

    try:
        raw_payload = json.loads(stdout_bytes.decode("utf-8", errors="strict"))
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        return {
            "requested": True,
            "ok": False,
            "returncode": int(completed.returncode),
            "schema_version": "",
            "failed_checks": ["doctor_payload"],
            "failures": ["candidate doctor did not emit a valid JSON object"],
            "checks": {},
            "recommendations": [],
        }
    if not isinstance(raw_payload, dict):
        return {
            "requested": True,
            "ok": False,
            "returncode": int(completed.returncode),
            "schema_version": "",
            "failed_checks": ["doctor_payload"],
            "failures": ["candidate doctor did not emit a valid JSON object"],
            "checks": {},
            "recommendations": [],
        }

    payload = raw_payload
    raw_checks = payload.get("checks")
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    bounded_checks: dict[str, dict[str, bool]] = {}
    failed_checks: list[str] = []
    missing_checks: list[str] = []
    for name in DOCTOR_REQUIRED_CHECK_NAMES:
        check = checks.get(name)
        check_ok = isinstance(check, dict) and check.get("ok") is True
        bounded_checks[name] = {"ok": check_ok}
        if name not in checks:
            missing_checks.append(name)
        if not check_ok:
            failed_checks.append(name)

    required_check_names = set(DOCTOR_REQUIRED_CHECK_NAMES)
    unexpected_checks = any(name not in required_check_names for name in checks)
    if unexpected_checks:
        failed_checks.append("doctor_check_contract")

    reported_schema = payload.get("schema_version")
    schema_ok = (
        isinstance(reported_schema, str)
        and reported_schema == DOCTOR_RESPONSE_SCHEMA_VERSION
    )
    if not schema_ok:
        failed_checks.append("doctor_schema_version")
    reported_ok = payload.get("ok") is True
    if not reported_ok:
        failed_checks.append("doctor_report")
    process_ok = int(completed.returncode) == 0
    if not process_ok:
        failed_checks.append("doctor_process")
    failures: list[str] = []
    if missing_checks:
        failures.append(
            "candidate doctor is missing required checks: "
            + ", ".join(missing_checks)
        )
    if unexpected_checks:
        failures.append("candidate doctor reported checks outside its response contract")
    unhealthy_checks = [name for name in failed_checks if name not in missing_checks]
    if unhealthy_checks:
        failures.append(
            "candidate doctor failed checks: " + ", ".join(unhealthy_checks)
        )
    if not schema_ok:
        failures.append("candidate doctor response schema version is incompatible")
    if not reported_ok:
        failures.append("candidate doctor did not report ok=true")
    if not process_ok:
        failures.append(f"candidate doctor exited with status {completed.returncode}")
    return {
        "requested": True,
        "ok": reported_ok and process_ok and not failed_checks,
        "returncode": int(completed.returncode),
        "schema_version": (
            reported_schema if isinstance(reported_schema, str) else ""
        ),
        "failed_checks": failed_checks,
        "failures": failures,
        # Never forward doctor details, runtime payloads, unknown checks,
        # recommendations, stdout, or stderr.  Canonical names and booleans are
        # the complete privacy-bounded installer contract.
        "checks": bounded_checks,
        "recommendations": [],
    }


def _activation_payload(
    home: Path,
    plugin_dir: Path,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    config_payload = _write_memory_provider_config(home)
    bootstrap_payload = _bootstrap_installed_provider(
        home,
        plugin_dir,
        snapshot=snapshot,
    )
    runtime_ok = bool(bootstrap_payload["runtime_verify"].get("ok"))
    postdeploy_doctor = (
        _postdeploy_doctor_verify(home, plugin_dir)
        if runtime_ok
        else {
            "requested": False,
            "ok": False,
            "failed_checks": ["runtime_verify"],
            "failures": ["candidate doctor skipped because runtime verification failed"],
            "checks": {},
            "recommendations": [],
        }
    )
    return {
        "activation_requested": True,
        "activated": runtime_ok and bool(postdeploy_doctor.get("ok")),
        **config_payload,
        **bootstrap_payload,
        "postdeploy_doctor": postdeploy_doctor,
    }


def _activation_runtime_failure(activation: dict[str, Any]) -> str:
    runtime = activation.get("runtime_verify")
    runtime_payload = runtime if isinstance(runtime, dict) else {}
    if not bool(runtime_payload.get("ok")):
        failures = [str(item) for item in runtime_payload.get("failures", []) if str(item)]
        detail = "; ".join(failures[:8]) or "runtime verification did not report ok=true"
        return f"activation runtime verification failed: {detail}"
    doctor = activation.get("postdeploy_doctor")
    doctor_payload = doctor if isinstance(doctor, dict) else {}
    if not bool(doctor_payload.get("ok")):
        failures = [str(item) for item in doctor_payload.get("failures", []) if str(item)]
        failed_checks = [str(item) for item in doctor_payload.get("failed_checks", []) if str(item)]
        detail = "; ".join(failures[:8])
        if not detail and failed_checks:
            detail = "failed checks: " + ", ".join(failed_checks[:20])
        return f"activation postdeploy doctor failed: {detail or 'doctor did not report ok=true'}"
    if bool(activation.get("activated")):
        return ""
    return "activation did not report activated=true after runtime and doctor verification"


def _extend_rollback_commands(result: dict[str, Any], commands: list[str]) -> None:
    existing = [str(item) for item in result.get("rollback_commands", []) if str(item)]
    for command in commands:
        _append_unique(existing, str(command))
    result["rollback_commands"] = existing
    if not result.get("rollback_command") and existing:
        result["rollback_command"] = existing[0]


def _detach_exception_tracebacks(exc: BaseException) -> None:
    """Release frames that may retain Windows file handles before compensation."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        cause = current.__cause__
        context = current.__context__
        current.__traceback__ = None
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
    gc.collect()


def _activate_installed_target(
    home: Path,
    target: Path,
    *,
    result: dict[str, Any],
    snapshot: dict[str, Any],
    previous_plugin_existed: bool,
    previous_version: str,
    plugin_backup_path: str,
    plugin_replaced: bool,
) -> dict[str, Any]:
    activation: dict[str, Any] = {}
    transaction: dict[str, Any] = {}
    try:
        activation = _activation_payload(home, target, snapshot)
        result.update(activation)
        runtime_failure = _activation_runtime_failure(activation)
        if runtime_failure:
            raise InstallError(runtime_failure)
        transaction = committed_activation_receipt(
            snapshot,
            plugin_dir=target,
            previous_plugin_existed=previous_plugin_existed,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
        )
        transaction_status = str(transaction.get("status") or "")
        if transaction_status != "committed":
            raw_failures = transaction.get("failures")
            transaction_failures = (
                [
                    sanitize_report_text(str(item))[:300]
                    for item in raw_failures
                    if str(item)
                ][:8]
                if isinstance(raw_failures, list)
                else []
            )
            detail = "; ".join(transaction_failures)
            message = (
                "activation transaction finalization did not commit: "
                f"status={sanitize_report_text(transaction_status)[:100] or '<missing>'}"
            )
            if detail:
                message += f"; {detail}"
            raise InstallError(message)
    except Exception as exc:
        activation_error = {
            "type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
        _detach_exception_tracebacks(exc)
        transaction = compensate_activation_failure(
            snapshot,
            plugin_dir=target,
            previous_plugin_existed=previous_plugin_existed,
            previous_version=previous_version,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
        )
        result.update(
            {
                "ok": False,
                "installed": False,
                "activated": False,
                "config_updated": False,
                "mode": (
                    "activation-failed-rolled-back"
                    if transaction["automatic_rollback"]
                    else "activation-failed-rollback-failed"
                ),
                "activation_error": activation_error,
                "activation_transaction": transaction,
                "verify": verify(home),
            }
        )
        _extend_rollback_commands(
            result,
            [str(item) for item in transaction.get("restore_commands", [])],
        )
        result["next_steps"] = [
            f"hermes-scope-recall verify --hermes-home {_shell_quote_path(home)}",
        ]
        if not transaction["automatic_rollback"]:
            result["next_steps"].extend(
                str(item) for item in transaction.get("restore_commands", [])
            )
        return result

    result["activation_transaction"] = transaction
    config_rollback = str(result.get("config_rollback_command") or "")
    commands = [config_rollback] if config_rollback else []
    commands.extend(str(item) for item in transaction.get("restore_commands", []))
    _extend_rollback_commands(result, commands)
    result["verify"] = activation["runtime_verify"]
    result["ok"] = bool(result["verify"].get("ok"))
    return result


def _manifestless_vector_state_failures(
    storage_dir: Path,
    conn: sqlite3.Connection,
    runtime_config: dict[str, Any],
) -> list[str]:
    """Return read-only blockers for a vector-enabled DB without a current manifest."""

    vector_config = dict(runtime_config.get("vector") or {})
    if not config_bool(vector_config, "enabled", True):
        return []

    generation_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generations'"
    ).fetchone()
    state_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generation_state'"
    ).fetchone()
    current_id = ""
    if state_table is not None:
        row = conn.execute(
            "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
        ).fetchone()
        current_id = str(row[0] or "") if row else ""
    if current_id:
        if generation_table is None:
            return ["current vector generation pointer exists without a generation manifest table"]
        manifest = conn.execute(
            "SELECT status FROM vector_generations WHERE generation_id = ?",
            (current_id,),
        ).fetchone()
        if manifest is None:
            return ["current vector generation pointer references a missing manifest"]
        if str(manifest[0] or "").strip().lower() != "active":
            return ["current vector generation manifest is not active"]
        return []

    manifest_count = 0
    if generation_table is not None:
        manifest_count = int(conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0])
    memories_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
    ).fetchone()
    truth_rows = (
        int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        if memories_table is not None
        else 0
    )

    failures: list[str] = []
    if manifest_count:
        failures.append(
            "active vector generation manifest is missing while generation manifests already exist"
        )
    if truth_rows:
        failures.append(
            "active vector generation manifest is missing while SQLite truth is non-empty"
        )

    configured_backends: list[tuple[str, str]] = []
    for label, raw_backend in (
        ("primary", vector_config.get("backend") or "lancedb"),
        ("fallback", vector_config.get("fallback_backend") or ""),
    ):
        if not str(raw_backend or "").strip():
            continue
        try:
            backend = normalize_vector_backend(str(raw_backend))
        except Exception:
            continue
        if any(existing_backend == backend for _, existing_backend in configured_backends):
            continue
        configured_backends.append((label, backend))

    for label, backend in configured_backends:
        presence = vector_companion_presence(backend, storage_dir)
        if presence is True:
            failures.append(
                f"manifestless vector companion exists for configured {label} backend {backend}"
            )
        elif presence is None:
            failures.append(
                f"manifestless remote vector companion absence cannot be proven for configured {label} backend {backend}"
            )
    return failures


def _upgrade_compatibility_preflight(
    home: Path,
    candidate_source: Path,
) -> dict[str, Any]:
    """Read-only N-1 contract check performed before backup or replacement."""

    storage_dir = home / "scope-recall"
    runtime_config = load_runtime_config(candidate_source, storage_dir)
    config_errors = load_runtime_config_errors(runtime_config)
    failures: list[str] = []
    next_steps: list[str] = []
    if config_errors:
        failures.extend(str(item.get("message") or "runtime config error") for item in config_errors)
        next_steps.append(
            "Migrate or remove every unsupported runtime config key before retrying the upgrade."
        )

    vector_checks: list[dict[str, Any]] = []
    manifestless_failures: list[str] = []
    db_path = storage_dir / "memory.sqlite3"
    if db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            try:
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'vector_generations'"
                ).fetchone()
                ready_rows = (
                    conn.execute(
                        "SELECT * FROM vector_generations "
                        "WHERE lower(status) = 'ready' ORDER BY generation_id"
                    ).fetchall()
                    if table_exists is not None
                    else []
                )
                for row in ready_rows:
                    manifest = dict(row)
                    generation_id = str(manifest.get("generation_id") or "")
                    manifest_sha256 = hashlib.sha256(
                        json.dumps(
                            manifest,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    try:
                        # The installer must enforce the same physical + current
                        # truth-cohort contract as activation and doctor. Keeping
                        # this call inside the query-only connection also closes
                        # the build-to-check race as much as a read preflight can.
                        report = validate_generation_for_activation(
                            storage_dir,
                            conn,
                            manifest,
                        )
                        vector_checks.append(
                            {
                                "generation_id": generation_id,
                                "ok": True,
                                "manifest_sha256": manifest_sha256,
                                "receipt_sha256": str(report.get("receipt_sha256") or ""),
                                "physical_rows": int(report.get("physical_rows") or 0),
                                "physical_records_sha256": str(
                                    report.get("physical_records_sha256") or ""
                                ),
                            }
                        )
                    except Exception as exc:
                        safe_error = sanitize_report_text(str(exc))[:300] or type(exc).__name__
                        failures.append(
                            f"ready vector generation {generation_id} is not upgrade-compatible: {safe_error}"
                        )
                        vector_checks.append(
                            {
                                "generation_id": generation_id,
                                "ok": False,
                                "error": safe_error,
                            }
                        )
                manifestless_failures = _manifestless_vector_state_failures(
                    storage_dir,
                    conn,
                    runtime_config,
                )
                failures.extend(manifestless_failures)
            finally:
                conn.close()
        except Exception as exc:
            safe_error = sanitize_report_text(str(exc))[:300] or type(exc).__name__
            failures.append(f"cannot inspect existing vector generation state: {safe_error}")
    if any(not bool(item.get("ok")) for item in vector_checks):
        next_steps.append(
            "Explicitly rebuild or migrate each invalid READY vector generation so a bound physical preflight receipt is produced, then rerun install."
        )
    if manifestless_failures:
        next_steps.append(
            "Run scripts/migrate.vector_generation.py with --dry-run, then --apply --activate under confirmed maintenance, and rerun the upgrade."
        )

    return {
        "ok": not failures,
        "read_only": True,
        "checked_before_backup": True,
        "config": {
            "ok": not config_errors,
            "error_count": len(config_errors),
            "errors": config_errors,
        },
        "ready_vector_generations": vector_checks,
        "manifestless_vector_state": {
            "ok": not manifestless_failures,
            "failures": manifestless_failures,
        },
        "failures": failures,
        "next_steps": next_steps,
    }


def _ready_vector_epoch(
    compatibility: dict[str, Any],
) -> tuple[tuple[str, str, str, int, str], ...]:
    """Return the privacy-safe manifest/physical epoch sealed by preflight."""

    raw_checks = compatibility.get("ready_vector_generations")
    checks = raw_checks if isinstance(raw_checks, list) else []
    return tuple(
        sorted(
            (
                str(item.get("generation_id") or ""),
                str(item.get("manifest_sha256") or ""),
                str(item.get("receipt_sha256") or ""),
                int(item.get("physical_rows") or 0),
                str(item.get("physical_records_sha256") or ""),
            )
            for item in checks
            if isinstance(item, dict) and item.get("ok") is True
        )
    )


def _revalidate_upgrade_compatibility(
    home: Path,
    candidate_source: Path,
    *,
    initial: dict[str, Any],
) -> dict[str, Any]:
    """Re-run canonical checks at the first plugin mutation boundary."""

    current = _upgrade_compatibility_preflight(home, candidate_source)
    current["revalidated_before_target_mutation"] = True
    if not bool(current.get("ok")):
        failures = [str(item) for item in current.get("failures", [])]
        detail = "; ".join(failures[:5]) or "unknown compatibility failure"
        raise InstallError(
            "upgrade compatibility revalidation failed before backup/replacement: "
            + detail
        )
    if _ready_vector_epoch(current) != _ready_vector_epoch(initial):
        raise InstallError(
            "READY vector generation manifest/physical epoch changed after the "
            "initial preflight and before backup/replacement"
        )
    return current


def _next_steps(home: Path) -> list[str]:
    quoted_home = _shell_quote_path(home)
    return [
        PROVIDER_CONFIG_COMMAND,
        "hermes memory setup",
        f"hermes-scope-recall verify --hermes-home {quoted_home}",
        "restart Hermes gateway/service to load the installed plugin copy",
        f"hermes-scope-recall doctor --hermes-home {quoted_home} --json",
    ]


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _verify_next_steps(
    home: Path,
    *,
    structural_ok: bool,
    runtime: bool,
    runtime_payload: dict[str, Any] | None,
) -> list[str]:
    steps: list[str] = []
    quoted_home = _shell_quote_path(home)
    if not structural_ok:
        _append_unique(steps, f"hermes-scope-recall install --hermes-home {quoted_home}")
        return steps
    if runtime and runtime_payload is not None:
        schema_status = runtime_payload.get("schema_migrations") if isinstance(runtime_payload.get("schema_migrations"), dict) else {}
        db_missing = not bool(runtime_payload.get("sqlite_path")) or not Path(str(runtime_payload.get("sqlite_path") or "")).is_file()
        if schema_status and not bool(schema_status.get("current")):
            _append_unique(steps, f"hermes-scope-recall migrate status --hermes-home {quoted_home}")
        if db_missing or not bool(runtime_payload.get("sqlite_schema_current")):
            _append_unique(steps, "hermes memory setup")
        if steps:
            _append_unique(steps, f"hermes-scope-recall verify --runtime --hermes-home {quoted_home}")
    return steps


def _extract_memory_provider_from_config(text: str) -> str:
    try:
        return memory_provider_from_yaml(text)
    except InstallerYamlError:
        return ""


def _hermes_config_summary(home: Path) -> dict[str, Any]:
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return {"exists": False, "memory_provider": "", "ok": False}
    provider = _extract_memory_provider_from_config(config_path.read_text(encoding="utf-8", errors="replace")[:200_000])
    return {"exists": True, "memory_provider": provider, "ok": provider == PLUGIN_NAME}


def _plugin_files_summary(plugin_dir: Path, *, missing: list[str], manifest_name: str, manifest_version: str) -> dict[str, Any]:
    discovery_marker = _has_discovery_marker(plugin_dir) if not missing else False
    return {
        "ok": not missing and manifest_name == PLUGIN_NAME and discovery_marker,
        "missing": missing,
        "manifest_name": manifest_name,
        "manifest_version": manifest_version,
        "discovery_marker": discovery_marker,
    }


def _provider_load_summary(runtime_payload: dict[str, Any] | None) -> dict[str, Any]:
    if runtime_payload is None:
        return {"requested": False, "ok": None}
    failures = [str(item) for item in runtime_payload.get("failures", []) if str(item).startswith("provider runtime load failed")]
    return {
        "requested": True,
        "ok": bool(runtime_payload.get("provider_loaded")) and not failures,
        "provider_loaded": bool(runtime_payload.get("provider_loaded")),
        "failures": failures,
    }


def _sqlite_truth_summary(home: Path, runtime_payload: dict[str, Any] | None) -> dict[str, Any]:
    db_path = Path(str((runtime_payload or {}).get("sqlite_path") or home / "scope-recall" / "memory.sqlite3"))
    return {
        "path": str(db_path),
        "exists": db_path.is_file(),
        "schema_current": bool((runtime_payload or {}).get("sqlite_schema_current")),
        "schema_migrations": (runtime_payload or {}).get("schema_migrations") or {},
    }


def _tool_schema_summary(runtime_payload: dict[str, Any] | None) -> dict[str, Any]:
    names = [str(item) for item in (runtime_payload or {}).get("tool_schema_names", [])]
    required = {
        "scope_recall_store",
        "scope_recall_search",
        "scope_recall_context",
        "scope_recall_profile",
        "scope_recall_memory",
        "scope_recall_entity",
    }
    missing = sorted(required - set(names))
    return {
        "names": names,
        "required": sorted(required),
        "missing_required": missing,
        "compact_required_present": not missing,
    }


def _vector_companion_summary(home: Path, runtime_payload: dict[str, Any] | None) -> dict[str, Any]:
    runtime_vector = (runtime_payload or {}).get("vector_companion")
    if isinstance(runtime_vector, dict):
        return runtime_vector
    return {"enabled": None, "configured_backend": "", "status": "unknown", "path": str(home / "scope-recall")}


def _layered_verify_payload(
    home: Path,
    plugin_dir: Path,
    *,
    missing: list[str],
    manifest_name: str,
    manifest_version: str,
    runtime_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "plugin_files": _plugin_files_summary(plugin_dir, missing=missing, manifest_name=manifest_name, manifest_version=manifest_version),
        "provider_load": _provider_load_summary(runtime_payload),
        "hermes_config": _hermes_config_summary(home),
        "sqlite_truth": _sqlite_truth_summary(home, runtime_payload),
        "tool_schemas": _tool_schema_summary(runtime_payload),
        "vector_companion": _vector_companion_summary(home, runtime_payload),
    }


def verify(hermes_home: str | os.PathLike[str] | None = None, *, runtime: bool = False) -> dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    plugin_dir = plugin_dir_for(home)
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (plugin_dir / rel).is_file()]
    manifest_name = _read_manifest_name(plugin_dir)
    manifest_version = _read_manifest_version(plugin_dir)
    failures: list[str] = []
    if manifest_name and manifest_name != PLUGIN_NAME:
        failures.append(f"plugin.yaml name is {manifest_name!r}, expected {PLUGIN_NAME!r}")
    if not missing and not _has_discovery_marker(plugin_dir):
        failures.append("__init__.py discovery marker")
    runtime_payload: dict[str, Any] | None = None
    if runtime and not missing and manifest_name == PLUGIN_NAME and not failures:
        runtime_payload = _runtime_verify(home, plugin_dir)
        failures.extend(str(item) for item in runtime_payload.get("failures", []))
    ok = not missing and manifest_name == PLUGIN_NAME and not failures
    structural_ok = not missing and manifest_name == PLUGIN_NAME and not (failures and runtime_payload is None)
    next_steps = _verify_next_steps(home, structural_ok=structural_ok, runtime=runtime, runtime_payload=runtime_payload) if not ok else []
    payload = {
        "ok": ok,
        "hermes_home": str(home),
        "plugin_dir": str(plugin_dir),
        "missing": missing,
        "failures": failures,
        "manifest_name": manifest_name,
        "manifest_version": manifest_version,
        "runtime": runtime_payload or {"requested": bool(runtime)},
        "next_steps": next_steps,
    }
    payload.update(
        _layered_verify_payload(
            home,
            plugin_dir,
            missing=missing,
            manifest_name=manifest_name,
            manifest_version=manifest_version,
            runtime_payload=runtime_payload,
        )
    )
    return payload


def install(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    activate: bool = False,
    maintenance_mode: bool = False,
) -> dict[str, Any]:
    """Install or upgrade the plugin copy in a Hermes home with backup and rollback metadata.

    Writes are explicit, source paths are validated, and operator output includes enough evidence to reverse a bad copy."""
    home = resolve_hermes_home(hermes_home)
    source = source_root()
    target = plugin_dir_for(home)
    if not all((source / rel).is_file() for rel in REQUIRED_PLUGIN_FILES):
        missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (source / rel).is_file()]
        raise InstallError(f"source tree is missing required plugin files: {', '.join(missing)}")

    previous_plugin_existed = path_exists(target) or path_is_symlink(target)
    previous_version = _read_manifest_version(target) if previous_plugin_existed else ""
    new_version = _read_manifest_version(source)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "installed": False,
        "mode": "dry-run" if dry_run else "copy",
        "source_dir": str(source),
        "hermes_home": str(home),
        "plugin_dir": str(target),
        "manifest_version": new_version,
        "new_version": new_version,
        "previous_plugin_existed": previous_plugin_existed,
        "previous_version": previous_version,
        "backup_path": "",
        "rollback_command": "",
        "rollback_commands": [],
        "activation_requested": activate,
        "maintenance_mode_confirmed": bool(maintenance_mode),
        "activated": False,
        "config_updated": False,
        "config_path": str(home / "config.yaml"),
        "config_backup_path": "",
        "config_rollback_command": "",
        "sqlite_schema_current": False,
        "runtime_verify": {"requested": False},
        "postdeploy_doctor": {"requested": False},
        "upgrade_compatibility": {"requested": True},
        "next_steps": _next_steps(home),
    }
    compatibility = _upgrade_compatibility_preflight(home, source)
    result["upgrade_compatibility"] = compatibility
    if not bool(compatibility.get("ok")):
        result["ok"] = False
        result["next_steps"] = list(compatibility.get("next_steps") or [])
        if dry_run:
            return result
        failures = [str(item) for item in compatibility.get("failures", [])]
        detail = "; ".join(failures[:5]) or "unknown compatibility failure"
        raise InstallError(
            "upgrade compatibility preflight failed before backup/replacement: "
            + detail
        )
    if dry_run:
        if activate:
            maintenance_arg = (
                " --maintenance-mode"
                if (home / "scope-recall" / "memory.sqlite3").is_file()
                else ""
            )
            result["next_steps"] = [
                f"hermes-scope-recall install --activate{maintenance_arg} --hermes-home {_shell_quote_path(home)}",
                f"hermes-scope-recall verify --runtime --hermes-home {_shell_quote_path(home)}",
            ]
        return result

    make_dirs(target.parent, exist_ok=True)
    target_exists = path_exists(target) or path_is_symlink(target)
    same_tree = target_exists and _is_same_tree(source, target)
    if target_exists and not same_tree:
        existing_name = _read_manifest_name(target)
        if not force and existing_name != PLUGIN_NAME:
            detail = f"manifest name: {existing_name!r}" if existing_name else "missing or unreadable scope-recall manifest"
            raise InstallError(
                f"refusing to overwrite existing target at {target} ({detail}); "
                "pass --force to replace it"
            )

    activation_snapshot: dict[str, Any] | None = None
    if activate:
        truth_db = home / "scope-recall" / "memory.sqlite3"
        if path_is_file(truth_db) and not maintenance_mode:
            raise InstallError(
                "activation against an existing truth DB requires --maintenance-mode; "
                "stop the gateway and all Scope Recall writers before retrying"
            )
        try:
            activation_snapshot = capture_activation_state(
                home,
                writer_quiesced=maintenance_mode,
            )
        except ActivationSnapshotError as exc:
            raise InstallError(
                f"cannot safely snapshot activation pre-state: {exc}"
            ) from exc

    if same_tree:
        result["mode"] = "already-installed"
        if activate:
            assert activation_snapshot is not None
            return _activate_installed_target(
                home,
                target,
                result=result,
                snapshot=activation_snapshot,
                previous_plugin_existed=previous_plugin_existed,
                previous_version=previous_version,
                plugin_backup_path="",
                plugin_replaced=False,
            )
        result["verify"] = verify(home)
        result["ok"] = bool(result["verify"]["ok"])
        return result

    def revalidate_before_target_mutation() -> None:
        result["upgrade_compatibility"] = _revalidate_upgrade_compatibility(
            home,
            source,
            initial=compatibility,
        )

    staging_root = public_path(
        tempfile.mkdtemp(prefix="sr.stg.", dir=io_path(target.parent))
    )
    staging = staging_root / PLUGIN_NAME
    backup_path = ""
    try:
        _copy_tree(source, staging)
        if path_exists(target) or path_is_symlink(target):
            backup = _backup_existing_plugin(
                home,
                target,
                category="scope-recall-installer",
                pre_mutation_check=revalidate_before_target_mutation,
            )
            backup_path = str(backup)
            result["backup_path"] = backup_path
            result["rollback_command"] = _rollback_command(home, backup_path)
            result["rollback_commands"] = [result["rollback_command"]]
            revalidate_before_target_mutation()
            _remove_existing_plugin(target)
        try:
            revalidate_before_target_mutation()
            move_path(staging, target)
        except Exception:
            if backup_path and not path_exists(target):
                _copy_existing_plugin(Path(backup_path), target)
            raise
    except Exception as exc:
        if activate:
            assert activation_snapshot is not None
            transaction = compensate_activation_failure(
                activation_snapshot,
                plugin_dir=target,
                previous_plugin_existed=previous_plugin_existed,
                previous_version=previous_version,
                plugin_backup_path=backup_path,
                plugin_replaced=bool(backup_path),
            )
            if not bool(transaction.get("automatic_rollback")):
                failures = "; ".join(
                    str(item)
                    for item in transaction.get("failures", [])
                    if str(item)
                )
                raise InstallError(
                    "pre-activation failure could not be compensated"
                    + (f": {failures}" if failures else "")
                ) from exc
        raise
    finally:
        remove_path(staging_root, missing_ok=True, ignore_errors=True)

    result["installed"] = True
    if activate:
        assert activation_snapshot is not None
        return _activate_installed_target(
            home,
            target,
            result=result,
            snapshot=activation_snapshot,
            previous_plugin_existed=previous_plugin_existed,
            previous_version=previous_version,
            plugin_backup_path=backup_path,
            plugin_replaced=True,
        )
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"]["ok"])
    return result


def rollback(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    backup_dir: str | os.PathLike[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    target = plugin_dir_for(home)
    backup = public_path(Path(backup_dir).expanduser())
    error = _validate_backup_dir(backup)
    if error:
        raise InstallError(error)
    replaced_version = _read_manifest_version(target) if path_exists(target) or path_is_symlink(target) else ""
    restored_version = _read_manifest_version(backup)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "restored": False,
        "hermes_home": str(home),
        "plugin_dir": str(target),
        "backup_dir": str(backup),
        "replaced_version": replaced_version,
        "restored_version": restored_version,
        "current_backup_path": "",
        "next_steps": _next_steps(home),
    }
    if dry_run:
        return result
    make_dirs(target.parent, exist_ok=True)
    if path_exists(target) or path_is_symlink(target):
        current_backup = _backup_existing_plugin(home, target, category="scope-recall-rollback-current")
        result["current_backup_path"] = str(current_backup)
        _remove_existing_plugin(target)
    try:
        _copy_existing_plugin(backup, target)
    except Exception:
        current_backup_path = str(result.get("current_backup_path") or "")
        if current_backup_path and not path_exists(target):
            _copy_existing_plugin(Path(current_backup_path), target)
        raise
    result["restored"] = True
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"].get("ok"))
    return result


def _print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    status = "ok" if payload.get("ok") else "error"
    print(f"scope-recall {status}")
    for key in ("hermes_home", "plugin_dir", "manifest_version", "mode"):
        if key in payload and payload[key]:
            print(f"{key}: {payload[key]}")
    missing = payload.get("missing") or []
    if missing:
        print("missing:")
        for item in missing:
            print(f"- {item}")
    next_steps = payload.get("next_steps") or []
    if next_steps:
        print("next steps:")
        for item in next_steps:
            print(f"- {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-scope-recall",
        description="Install or verify the scope-recall Hermes memory provider plugin.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install", help="copy scope-recall into a Hermes home plugins directory")
    install_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    install_parser.add_argument("--dry-run", action="store_true", help="show what would be installed without mutating files")
    install_parser.add_argument("--force", action="store_true", help="replace an existing non-scope-recall directory")
    install_parser.add_argument("--activate", action="store_true", help="also set memory.provider=scope-recall and bootstrap the SQLite schema")
    install_parser.add_argument(
        "--maintenance-mode",
        action="store_true",
        help="confirm the gateway and all Scope Recall writers are stopped before activating an existing truth DB",
    )
    install_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    upgrade_parser = sub.add_parser("upgrade", help="upgrade scope-recall and back up the existing plugin copy first")
    upgrade_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    upgrade_parser.add_argument("--dry-run", action="store_true", help="show what would be upgraded without mutating files")
    upgrade_parser.add_argument("--force", action="store_true", help="replace an existing non-scope-recall directory")
    upgrade_parser.add_argument("--activate", action="store_true", help="also set memory.provider=scope-recall and bootstrap the SQLite schema")
    upgrade_parser.add_argument(
        "--maintenance-mode",
        action="store_true",
        help="confirm the gateway and all Scope Recall writers are stopped before activating an existing truth DB",
    )
    upgrade_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    rollback_parser = sub.add_parser("rollback", help="restore a previous scope-recall plugin backup")
    rollback_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    rollback_parser.add_argument("--backup-dir", required=True, help="Backup plugin directory returned by install/upgrade")
    rollback_parser.add_argument("--dry-run", action="store_true", help="validate rollback without mutating files")
    rollback_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    verify_parser = sub.add_parser("verify", help="verify scope-recall is installed in a Hermes home")
    verify_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    verify_parser.add_argument("--runtime", action="store_true", help="also load the installed provider and read the SQLite schema ledger")
    verify_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"install", "upgrade"}:
            payload = install(
                args.hermes_home,
                dry_run=args.dry_run,
                force=args.force,
                activate=args.activate,
                maintenance_mode=args.maintenance_mode,
            )
            if args.command == "upgrade":
                payload["mode"] = "upgrade-dry-run" if args.dry_run else ("already-installed" if payload.get("mode") == "already-installed" else "upgrade")
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        if args.command == "rollback":
            payload = rollback(args.hermes_home, backup_dir=args.backup_dir, dry_run=args.dry_run)
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        if args.command == "verify":
            payload = verify(args.hermes_home, runtime=args.runtime)
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
    except InstallError as exc:
        payload = {"ok": False, "error": str(exc)}
        _print_payload(payload, as_json=getattr(args, "json", False))
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
