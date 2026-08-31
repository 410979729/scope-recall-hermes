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
    abort_interrupted_activation_capture,
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
    DOCTOR_ACTIVATION_ADVISORY_CHECK_NAMES,
    DOCTOR_ACTIVATION_SAFETY_CHECK_NAMES,
    DOCTOR_REQUIRED_CHECK_NAMES,
    DOCTOR_RESPONSE_SCHEMA_VERSION,
)
from .recovery_commands import quote_argument, restore_file_command
from .sqlite_backup import logical_fingerprint as sqlite_logical_fingerprint
from .vector_bootstrap import vector_companion_presence
from .vector_generation_preflight import validate_generation_for_activation
from .vector_store import normalize_vector_backend
from .windows_filesystem import (
    copy_file,
    copy_tree as filesystem_copy_tree,
    atomic_write_text,
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
    ".execution",
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
_MANAGED_TRANSACTION_SCHEMA = "scope-recall.managed-activation-transaction.v1"
_MANAGED_TRANSACTION_FILENAME = "activation-transaction.json"


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _managed_json_value(value: Any) -> Any:
    """Convert an activation snapshot to a strict, lossless JSON surface."""

    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise InstallError("managed activation state contains a non-string key")
        return {key: _managed_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_managed_json_value(item) for item in value]
    raise InstallError(
        "managed activation state contains an unsupported value: "
        f"{type(value).__name__}"
    )


def _managed_unsigned(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "sha256"}


def _managed_canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _managed_seal(document: dict[str, Any]) -> dict[str, Any]:
    unsigned = _managed_unsigned(_managed_json_value(document))
    return {
        **unsigned,
        "sha256": hashlib.sha256(_managed_canonical_bytes(unsigned)).hexdigest(),
    }


def _managed_path_is_linklike(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _validate_managed_state_dir(
    home: Path,
    managed_state_dir: str | os.PathLike[str] | None,
    *,
    create: bool,
) -> Path:
    if managed_state_dir is None or not str(managed_state_dir).strip():
        raise InstallError("managed upgrade requires an explicit private state directory")
    home = home.expanduser().resolve()
    operations_root = (home / "scope-recall" / "upgrades" / "operations").resolve(
        strict=False
    )
    raw = Path(managed_state_dir).expanduser()
    state_dir = raw.resolve(strict=False)
    if (
        state_dir.name != "private"
        or state_dir.parent.parent != operations_root
        or not state_dir.parent.name
        or state_dir.parent.name in {".", ".."}
        or ".." in state_dir.parent.name
    ):
        raise InstallError(
            "managed state must be the private directory of one exact operation"
        )
    for candidate in (state_dir.parent, state_dir):
        if _managed_path_is_linklike(candidate):
            raise InstallError("managed state refuses symlink or reparse-point paths")
    if create:
        make_dirs(state_dir, exist_ok=True)
        try:
            state_dir.chmod(0o700)
        except OSError:
            pass
    if not state_dir.is_dir():
        raise InstallError("managed private state directory does not exist")
    return state_dir


def _managed_transaction_path(state_dir: Path) -> Path:
    path = state_dir / _MANAGED_TRANSACTION_FILENAME
    if _managed_path_is_linklike(path):
        raise InstallError("managed activation transaction cannot be a link")
    return path


def _managed_durable_replace(source: Path, destination: Path) -> None:
    """Publish the activation handle with write-through semantics on Windows."""

    if os.name != "nt":
        os.replace(io_path(source), io_path(destination))
        return
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    if not move_file_ex(
        io_path(source),
        io_path(destination),
        0x1 | 0x8,  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _write_managed_transaction(
    state_dir: Path,
    document: dict[str, Any],
) -> dict[str, Any]:
    path = _managed_transaction_path(state_dir)
    sealed = _managed_seal(document)
    payload = _managed_canonical_bytes(sealed) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=io_path(state_dir),
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _managed_durable_replace(temporary, path)
        with open(io_path(path), "r+b") as published:
            if published.read() != payload:
                raise InstallError(
                    "managed activation transaction durable publish mismatch"
                )
            published.flush()
            os.fsync(published.fileno())
        try:
            path.chmod(0o600)
        except OSError:
            pass
        try:
            directory_fd = os.open(io_path(state_dir), os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        remove_path(temporary, missing_ok=True, ignore_errors=True)
    return sealed


def _read_managed_transaction(state_dir: Path) -> dict[str, Any]:
    path = _managed_transaction_path(state_dir)
    if not path.is_file():
        raise InstallError("managed activation transaction is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("managed activation transaction is unreadable") from exc
    if not isinstance(payload, dict):
        raise InstallError("managed activation transaction is not an object")
    supplied = str(payload.get("sha256") or "")
    expected = hashlib.sha256(
        _managed_canonical_bytes(_managed_unsigned(payload))
    ).hexdigest()
    if not supplied or supplied != expected:
        raise InstallError("managed activation transaction integrity check failed")
    if payload.get("schema_version") != _MANAGED_TRANSACTION_SCHEMA:
        raise InstallError("managed activation transaction schema is incompatible")
    return payload


def _begin_managed_transaction_intent(
    state_dir: Path,
    *,
    home: Path,
    target: Path,
    previous_plugin_existed: bool,
    previous_version: str,
    target_version: str,
    requires_vector_degrade: bool,
    capture_lease_token: str,
) -> dict[str, Any]:
    path = _managed_transaction_path(state_dir)
    if path.exists():
        raise InstallError(
            "managed activation transaction already exists; resume it instead"
        )
    now = _utc_now_iso()
    return _write_managed_transaction(
        state_dir,
        {
            "schema_version": _MANAGED_TRANSACTION_SCHEMA,
            "phase": "snapshot_pending",
            "created_at": now,
            "updated_at": now,
            "hermes_home": str(home),
            "plugin_dir": str(target),
            "previous_plugin_existed": bool(previous_plugin_existed),
            "previous_version": str(previous_version),
            "target_version": str(target_version),
            "requires_vector_degrade": bool(requires_vector_degrade),
            "capture_lease_token": str(capture_lease_token),
            "plugin_backup_path": "",
            "plugin_replaced": False,
            "snapshot": {},
            "last_transaction": {},
        },
    )


def _begin_managed_transaction(
    state_dir: Path,
    *,
    home: Path,
    target: Path,
    snapshot: dict[str, Any],
    previous_plugin_existed: bool,
    previous_version: str,
    target_version: str,
    requires_vector_degrade: bool,
) -> dict[str, Any]:
    """Compatibility helper for already-captured test/operator snapshots."""

    lease = snapshot.get("maintenance_lease")
    raw_lease = lease if isinstance(lease, dict) else {}
    capture_token = str(raw_lease.get("token") or uuid.uuid4().hex)
    _begin_managed_transaction_intent(
        state_dir,
        home=home,
        target=target,
        previous_plugin_existed=previous_plugin_existed,
        previous_version=previous_version,
        target_version=target_version,
        requires_vector_degrade=requires_vector_degrade,
        capture_lease_token=capture_token,
    )
    return _advance_managed_transaction(
        state_dir,
        "snapshot_captured",
        snapshot=snapshot,
    )


def _advance_managed_transaction(
    state_dir: Path,
    phase: str,
    **updates: Any,
) -> dict[str, Any]:
    current = _read_managed_transaction(state_dir)
    document = {
        **_managed_unsigned(current),
        **updates,
        "phase": str(phase),
        "updated_at": _utc_now_iso(),
    }
    return _write_managed_transaction(state_dir, document)


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


def _read_current_vector_generation(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Read the active vector manifest without invoking schema-ensuring helpers."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('vector_generations', 'vector_generation_state')"
        ).fetchall()
    }
    if tables != {"vector_generations", "vector_generation_state"}:
        return None
    cursor = conn.execute(
        "SELECT generation.* FROM vector_generation_state AS state "
        "JOIN vector_generations AS generation ON generation.generation_id = state.value "
        "WHERE state.key = 'current_generation' LIMIT 1"
    )
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description or ()]
    return {column: row[index] for index, column in enumerate(columns)}


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
                    manifest = _read_current_vector_generation(conn)
                    if manifest is not None:
                        active_backend = str(
                            manifest.get("backend") or ""
                        ).strip().lower()
                        vector_generation = importlib.import_module(
                            f"{package_name}.vector_generation"
                        )
                        generation_root = vector_generation.resolve_generation_storage_root(
                            storage_dir, manifest.get("storage_path")
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


def _postdeploy_doctor_verify(
    home: Path,
    plugin_dir: Path,
    *,
    managed_upgrade: bool = False,
) -> dict[str, Any]:
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
                "-X",
                "utf8",
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
    process_ok = int(completed.returncode) == 0
    safety_failed_checks = [
        name
        for name in DOCTOR_ACTIVATION_SAFETY_CHECK_NAMES
        if not bool(bounded_checks.get(name, {}).get("ok"))
    ]
    advisory_failed_checks = [
        name
        for name in DOCTOR_ACTIVATION_ADVISORY_CHECK_NAMES
        if not bool(bounded_checks.get(name, {}).get("ok"))
    ]
    contract_failed = bool(missing_checks or unexpected_checks or not schema_ok)
    managed_process_ok = int(completed.returncode) in {0, 1}
    if not managed_upgrade:
        if not reported_ok:
            failed_checks.append("doctor_report")
        if not process_ok:
            failed_checks.append("doctor_process")
    elif not managed_process_ok:
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
    if not reported_ok and not managed_upgrade:
        failures.append("candidate doctor did not report ok=true")
    if not process_ok and not managed_upgrade:
        failures.append(f"candidate doctor exited with status {completed.returncode}")
    if managed_upgrade and safety_failed_checks:
        failures.append(
            "candidate doctor failed activation safety checks: "
            + ", ".join(safety_failed_checks)
        )
    if managed_upgrade and not managed_process_ok:
        failures.append(f"candidate doctor exited with status {completed.returncode}")
    safety_gate_ok = (
        not contract_failed
        and not safety_failed_checks
        and (managed_process_ok if managed_upgrade else process_ok)
    )
    return {
        "requested": True,
        "ok": (
            safety_gate_ok
            if managed_upgrade
            else reported_ok and process_ok and not failed_checks
        ),
        "returncode": int(completed.returncode),
        "schema_version": (
            reported_schema if isinstance(reported_schema, str) else ""
        ),
        "failed_checks": failed_checks,
        "safety_gate_ok": safety_gate_ok,
        "safety_failed_checks": safety_failed_checks,
        "advisory_failed_checks": advisory_failed_checks,
        "managed_upgrade_policy": bool(managed_upgrade),
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
    *,
    managed_upgrade: bool = False,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    config_payload = _write_memory_provider_config(home)
    if progress is not None:
        progress("config_updated", snapshot)
    bootstrap_payload = _bootstrap_installed_provider(
        home,
        plugin_dir,
        snapshot=snapshot,
    )
    if progress is not None:
        # Bootstrap may perform additive SQLite migrations and refreshes the
        # activation-owned fingerprint. Persist that refreshed epoch before
        # the next failure point.
        progress("bootstrap_complete", snapshot)
    runtime_ok = bool(bootstrap_payload["runtime_verify"].get("ok"))
    postdeploy_doctor = (
        _postdeploy_doctor_verify(
            home,
            plugin_dir,
            managed_upgrade=managed_upgrade,
        )
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
    if progress is not None:
        progress("doctor_complete", snapshot)
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


def _disable_vector_for_managed_upgrade(home: Path) -> dict[str, Any]:
    """Persist a local-only lexical fallback after vector preflight debt.

    The activation snapshot is captured before this function runs, so every
    byte is restored by the ordinary compensation path when activation fails.
    The updater never deletes vector companions and never attempts an
    embedding rebuild; an operator can explicitly re-enable and repair the
    derived index after the version upgrade has completed.
    """

    config_path = home / "scope-recall" / "config.json"
    if config_path.is_symlink():
        raise InstallError(
            "managed upgrade refuses to replace a symlinked Scope Recall config"
        )
    raw = "{}"
    if config_path.is_file():
        raw = config_path.read_text(encoding="utf-8", errors="strict")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError(
            "managed upgrade cannot disable an unreadable Scope Recall config"
        ) from exc
    if not isinstance(payload, dict):
        raise InstallError("managed upgrade requires a JSON object provider config")
    raw_vector = payload.get("vector")
    if raw_vector is not None and not isinstance(raw_vector, dict):
        raise InstallError("managed upgrade requires vector config to be an object")
    vector = dict(raw_vector or {})
    previously_enabled = config_bool(vector, "enabled", True)
    if vector.get("enabled") is False:
        return {
            "applied": False,
            "previously_enabled": previously_enabled,
            "mode": "sqlite-lexical",
            "content_egress": False,
        }
    vector["enabled"] = False
    payload["vector"] = vector
    atomic_write_text(
        config_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "applied": True,
        "previously_enabled": previously_enabled,
        "mode": "sqlite-lexical",
        "content_egress": False,
    }


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
    managed_upgrade: bool = False,
    degrade_vector: bool = False,
    managed_state_dir: Path | None = None,
) -> dict[str, Any]:
    activation: dict[str, Any] = {}
    transaction: dict[str, Any] = {}
    commit_decided = False
    try:
        if managed_upgrade:
            if managed_state_dir is None:
                raise InstallError("managed activation state directory is missing")
            _advance_managed_transaction(
                managed_state_dir,
                "activating",
                activation_step="starting",
                snapshot=snapshot,
                plugin_backup_path=plugin_backup_path,
                plugin_replaced=bool(plugin_replaced),
            )

        def persist_progress(step: str, current_snapshot: dict[str, Any]) -> None:
            if managed_upgrade:
                assert managed_state_dir is not None
                _advance_managed_transaction(
                    managed_state_dir,
                    "activating",
                    activation_step=step,
                    snapshot=current_snapshot,
                    plugin_backup_path=plugin_backup_path,
                    plugin_replaced=bool(plugin_replaced),
                )

        if degrade_vector:
            result["managed_vector_degrade"] = _disable_vector_for_managed_upgrade(
                home
            )
            persist_progress("vector_degraded", snapshot)
        activation = (
            _activation_payload(
                home,
                target,
                snapshot,
                managed_upgrade=True,
                progress=persist_progress,
            )
            if managed_upgrade
            else _activation_payload(home, target, snapshot)
        )
        result.update(activation)
        runtime_failure = _activation_runtime_failure(activation)
        if runtime_failure:
            raise InstallError(runtime_failure)
        if managed_upgrade:
            assert managed_state_dir is not None
            commit_decided = True
            _advance_managed_transaction(
                managed_state_dir,
                "commit_started",
                activation_step="validated",
                snapshot=snapshot,
                plugin_backup_path=plugin_backup_path,
                plugin_replaced=bool(plugin_replaced),
            )
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
        if managed_upgrade and commit_decided and managed_state_dir is not None:
            pending_transaction = transaction or {
                "status": "commit_cleanup_pending",
                "automatic_rollback": False,
                "failures": ["activation commit cleanup must be retried"],
                "restore_commands": [],
            }
            durable = True
            try:
                _advance_managed_transaction(
                    managed_state_dir,
                    "commit_cleanup_pending",
                    activation_step="commit_cleanup_pending",
                    snapshot=snapshot,
                    last_transaction=pending_transaction,
                    plugin_backup_path=plugin_backup_path,
                    plugin_replaced=bool(plugin_replaced),
                )
            except Exception:
                # ``commit_started`` is already durable.  Never reverse that
                # decision; the frozen outer worker will retry this phase.
                durable = False
            result.update(
                {
                    "ok": False,
                    "installed": True,
                    "activated": False,
                    "mode": "managed-commit-cleanup-pending",
                    "safe_to_restart_previous": False,
                    "managed_retryable": True,
                    "activation_error": activation_error,
                    "activation_transaction": pending_transaction,
                    "managed_commit_state_durable": durable,
                }
            )
            return result
        if managed_upgrade and managed_state_dir is not None:
            try:
                _advance_managed_transaction(
                    managed_state_dir,
                    "rollback_started",
                    activation_step="failed",
                    snapshot=snapshot,
                    plugin_backup_path=plugin_backup_path,
                    plugin_replaced=bool(plugin_replaced),
                )
            except Exception:
                # Compensation still owns the in-memory capability. Never skip
                # rollback merely because the external journal became damaged.
                pass
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
                "safe_to_restart_previous": bool(
                    transaction["automatic_rollback"]
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
        if managed_upgrade and managed_state_dir is not None:
            try:
                _advance_managed_transaction(
                    managed_state_dir,
                    (
                        "rolled_back"
                        if bool(transaction.get("automatic_rollback"))
                        else "rollback_failed"
                    ),
                    snapshot=snapshot,
                    last_transaction=transaction,
                    plugin_backup_path=plugin_backup_path,
                    plugin_replaced=bool(plugin_replaced),
                )
            except Exception as journal_exc:
                result["ok"] = False
                result["safe_to_restart_previous"] = False
                result["mode"] = "managed-journal-finalization-failed"
                result["journal_error"] = {
                    "type": type(journal_exc).__name__,
                    "message": sanitize_report_text(str(journal_exc))[:500],
                }
        return result

    result["activation_transaction"] = transaction
    config_rollback = str(result.get("config_rollback_command") or "")
    commands = [config_rollback] if config_rollback else []
    commands.extend(str(item) for item in transaction.get("restore_commands", []))
    _extend_rollback_commands(result, commands)
    result["verify"] = activation["runtime_verify"]
    result["ok"] = bool(result["verify"].get("ok"))
    result["safe_to_restart_previous"] = False
    if managed_upgrade:
        assert managed_state_dir is not None
        _advance_managed_transaction(
            managed_state_dir,
            "committed",
            activation_step="complete",
            snapshot=snapshot,
            last_transaction=transaction,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=bool(plugin_replaced),
        )
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
    *,
    managed_upgrade: bool = False,
) -> dict[str, Any]:
    """Read-only N-1 contract check performed before backup or replacement."""

    storage_dir = home / "scope-recall"
    runtime_config = load_runtime_config(candidate_source, storage_dir)
    config_errors = load_runtime_config_errors(runtime_config)
    failures: list[str] = []
    recoverable_vector_debt: list[str] = []
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
                        recoverable_vector_debt.append(
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
                recoverable_vector_debt.extend(manifestless_failures)
            finally:
                conn.close()
        except Exception as exc:
            safe_error = sanitize_report_text(str(exc))[:300] or type(exc).__name__
            failures.append(f"cannot inspect existing vector generation state: {safe_error}")
    if any(not bool(item.get("ok")) for item in vector_checks) and not managed_upgrade:
        next_steps.append(
            "Explicitly rebuild or migrate each invalid READY vector generation so a bound physical preflight receipt is produced, then rerun install."
        )
    if manifestless_failures and not managed_upgrade:
        next_steps.append(
            "Run scripts/migrate.vector_generation.py with --dry-run, then --apply --activate under confirmed maintenance, and rerun the upgrade."
        )

    if not managed_upgrade:
        failures.extend(recoverable_vector_debt)

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
        "managed_upgrade": bool(managed_upgrade),
        "recoverable_vector_debt": recoverable_vector_debt,
        "requires_vector_degrade": bool(
            managed_upgrade and recoverable_vector_debt
        ),
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
    managed_upgrade: bool = False,
) -> dict[str, Any]:
    """Re-run canonical checks at the first plugin mutation boundary."""

    current = _upgrade_compatibility_preflight(
        home,
        candidate_source,
        managed_upgrade=managed_upgrade,
    )
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


def managed_upgrade_preflight(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    candidate_tree: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return the complete read-only installer gate for a managed candidate.

    This public seam lets an external frozen runner decide whether it may stop
    a target gateway. It performs no backup, lease acquisition, config update,
    plugin copy, schema bootstrap, repair, or network request.
    """

    home = resolve_hermes_home(hermes_home)
    candidate = (
        Path(candidate_tree).expanduser().resolve()
        if candidate_tree is not None
        else source_root()
    )
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (candidate / rel).is_file()]
    if missing:
        return {
            "ok": False,
            "read_only": True,
            "content_egress": False,
            "candidate_dir": str(candidate),
            "target_version": "",
            "previous_version": _read_manifest_version(plugin_dir_for(home)),
            "failures": [
                "candidate is missing required plugin files: " + ", ".join(missing)
            ],
            "compatibility": {"requested": False},
        }
    if _read_manifest_name(candidate) != PLUGIN_NAME:
        return {
            "ok": False,
            "read_only": True,
            "content_egress": False,
            "candidate_dir": str(candidate),
            "target_version": _read_manifest_version(candidate),
            "previous_version": _read_manifest_version(plugin_dir_for(home)),
            "failures": ["candidate plugin manifest name is not scope-recall"],
            "compatibility": {"requested": False},
        }
    compatibility = _upgrade_compatibility_preflight(
        home,
        candidate,
        managed_upgrade=True,
    )
    return {
        "ok": bool(compatibility.get("ok")),
        "read_only": True,
        "content_egress": False,
        "candidate_dir": str(candidate),
        "target_version": _read_manifest_version(candidate),
        "previous_version": _read_manifest_version(plugin_dir_for(home)),
        "requires_vector_degrade": bool(
            compatibility.get("requires_vector_degrade")
        ),
        "failures": [str(item) for item in compatibility.get("failures", [])],
        "compatibility": compatibility,
    }


def install(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    activate: bool = False,
    maintenance_mode: bool = False,
    managed_upgrade: bool = False,
    managed_state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Install or upgrade the plugin copy in a Hermes home with backup and rollback metadata.

    Writes are explicit, source paths are validated, and operator output includes enough evidence to reverse a bad copy."""
    home = resolve_hermes_home(hermes_home)
    private_state: Path | None = None
    if managed_upgrade and not (activate and maintenance_mode):
        raise InstallError(
            "managed upgrade requires activation under confirmed maintenance mode"
        )
    if managed_upgrade:
        private_state = _validate_managed_state_dir(
            home,
            managed_state_dir,
            create=True,
        )
        if _managed_transaction_path(private_state).exists():
            raise InstallError(
                "managed activation transaction already exists; resume it instead"
            )
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
        "managed_upgrade": bool(managed_upgrade),
        "activated": False,
        "config_updated": False,
        "config_path": str(home / "config.yaml"),
        "config_backup_path": "",
        "config_rollback_command": "",
        "sqlite_schema_current": False,
        "runtime_verify": {"requested": False},
        "postdeploy_doctor": {"requested": False},
        "upgrade_compatibility": {"requested": True},
        "mutation_started": False,
        "safe_to_restart_previous": False,
        "next_steps": _next_steps(home),
    }
    compatibility = _upgrade_compatibility_preflight(
        home,
        source,
        managed_upgrade=managed_upgrade,
    )
    result["upgrade_compatibility"] = compatibility
    if not bool(compatibility.get("ok")):
        result["ok"] = False
        result["next_steps"] = list(compatibility.get("next_steps") or [])
        if dry_run or managed_upgrade:
            if managed_upgrade:
                result["mode"] = "managed-preflight-failed-safe"
                result["safe_to_restart_previous"] = True
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

    managed_flags = {
        "degrade_vector": bool(compatibility.get("requires_vector_degrade"))
    }
    activation_snapshot: dict[str, Any] | None = None
    if activate:
        truth_db = home / "scope-recall" / "memory.sqlite3"
        if path_is_file(truth_db) and not maintenance_mode:
            raise InstallError(
                "activation against an existing truth DB requires --maintenance-mode; "
                "stop the gateway and all Scope Recall writers before retrying"
            )
        capture_lease_token = ""
        if managed_upgrade:
            assert private_state is not None
            capture_lease_token = uuid.uuid4().hex
            try:
                _begin_managed_transaction_intent(
                    private_state,
                    home=home,
                    target=target,
                    previous_plugin_existed=previous_plugin_existed,
                    previous_version=previous_version,
                    target_version=new_version,
                    requires_vector_degrade=managed_flags["degrade_vector"],
                    capture_lease_token=capture_lease_token,
                )
            except Exception as exc:
                result.update(
                    {
                        "ok": False,
                        "mode": "managed-journal-start-failed-safe",
                        "safe_to_restart_previous": True,
                        "activation_error": {
                            "type": type(exc).__name__,
                            "message": sanitize_report_text(str(exc))[:500],
                        },
                    }
                )
                return result
        try:
            activation_snapshot = capture_activation_state(
                home,
                writer_quiesced=maintenance_mode,
                activation_lease_token=capture_lease_token,
            )
        except ActivationSnapshotError as exc:
            if managed_upgrade:
                assert private_state is not None
                rollback_receipt = {
                    "status": "rolled_back",
                    "automatic_rollback": True,
                    "failures": [],
                    "restore_commands": [],
                }
                durable = True
                try:
                    _advance_managed_transaction(
                        private_state,
                        "rolled_back",
                        snapshot={},
                        last_transaction=rollback_receipt,
                    )
                except Exception:
                    durable = False
                result.update(
                    {
                        "ok": False,
                        "mode": (
                            "managed-snapshot-failed-safe"
                            if durable
                            else "managed-snapshot-failed-journal-uncertain"
                        ),
                        "safe_to_restart_previous": durable,
                        "activation_error": {
                            "type": type(exc).__name__,
                            "message": sanitize_report_text(str(exc))[:500],
                        },
                        "activation_transaction": rollback_receipt,
                    }
                )
                return result
            raise InstallError(
                f"cannot safely snapshot activation pre-state: {exc}"
            ) from exc
        if managed_upgrade:
            assert private_state is not None
            try:
                _advance_managed_transaction(
                    private_state,
                    "snapshot_captured",
                    snapshot=activation_snapshot,
                )
            except Exception as exc:
                transaction = compensate_activation_failure(
                    activation_snapshot,
                    plugin_dir=target,
                    previous_plugin_existed=previous_plugin_existed,
                    previous_version=previous_version,
                    plugin_backup_path="",
                    plugin_replaced=False,
                )
                automatic_rollback = bool(transaction.get("automatic_rollback"))
                durable = True
                try:
                    _advance_managed_transaction(
                        private_state,
                        "rolled_back" if automatic_rollback else "rollback_failed",
                        snapshot=activation_snapshot,
                        last_transaction=transaction,
                    )
                except Exception:
                    durable = False
                result.update(
                    {
                        "ok": False,
                        "mode": (
                            "managed-journal-start-failed-safe"
                            if automatic_rollback and durable
                            else "managed-journal-start-failed-rollback-failed"
                        ),
                        "safe_to_restart_previous": automatic_rollback and durable,
                        "activation_error": {
                            "type": type(exc).__name__,
                            "message": sanitize_report_text(str(exc))[:500],
                        },
                        "activation_transaction": transaction,
                    }
                )
                return result
        result["mutation_started"] = True

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
                managed_upgrade=managed_upgrade,
                degrade_vector=managed_flags["degrade_vector"],
                managed_state_dir=private_state,
            )
        result["verify"] = verify(home)
        result["ok"] = bool(result["verify"]["ok"])
        return result

    def revalidate_before_target_mutation() -> None:
        current = _revalidate_upgrade_compatibility(
            home,
            source,
            initial=compatibility,
            managed_upgrade=managed_upgrade,
        )
        result["upgrade_compatibility"] = current
        if current.get("requires_vector_degrade"):
            managed_flags["degrade_vector"] = True

    staging_root = public_path(
        tempfile.mkdtemp(prefix="sr.stg.", dir=io_path(target.parent))
    )
    staging = staging_root / PLUGIN_NAME
    backup_path = ""
    plugin_mutation_started = False
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
            if managed_upgrade:
                assert private_state is not None
                _advance_managed_transaction(
                    private_state,
                    "plugin_backed_up",
                    snapshot=activation_snapshot,
                    plugin_backup_path=backup_path,
                    plugin_replaced=False,
                )
            revalidate_before_target_mutation()
            if managed_upgrade:
                assert private_state is not None
                _advance_managed_transaction(
                    private_state,
                    "plugin_mutation_started",
                    snapshot=activation_snapshot,
                    plugin_backup_path=backup_path,
                    plugin_replaced=True,
                )
            plugin_mutation_started = True
            _remove_existing_plugin(target)
        try:
            revalidate_before_target_mutation()
            if not plugin_mutation_started:
                if managed_upgrade:
                    assert private_state is not None
                    _advance_managed_transaction(
                        private_state,
                        "plugin_mutation_started",
                        snapshot=activation_snapshot,
                        plugin_backup_path=backup_path,
                        plugin_replaced=True,
                    )
                plugin_mutation_started = True
            move_path(staging, target)
            if managed_upgrade:
                assert private_state is not None
                _advance_managed_transaction(
                    private_state,
                    "candidate_installed",
                    snapshot=activation_snapshot,
                    plugin_backup_path=backup_path,
                    plugin_replaced=True,
                )
        except Exception:
            if backup_path and not path_exists(target):
                _copy_existing_plugin(Path(backup_path), target)
            raise
    except Exception as exc:
        if activate:
            assert activation_snapshot is not None
            if managed_upgrade and private_state is not None:
                try:
                    _advance_managed_transaction(
                        private_state,
                        "rollback_started",
                        snapshot=activation_snapshot,
                        plugin_backup_path=backup_path,
                        plugin_replaced=bool(plugin_mutation_started),
                    )
                except Exception:
                    pass
            transaction = compensate_activation_failure(
                activation_snapshot,
                plugin_dir=target,
                previous_plugin_existed=previous_plugin_existed,
                previous_version=previous_version,
                plugin_backup_path=backup_path,
                plugin_replaced=bool(plugin_mutation_started),
            )
            if managed_upgrade:
                automatic_rollback = bool(transaction.get("automatic_rollback"))
                result.update(
                    {
                        "ok": False,
                        "installed": False,
                        "activated": False,
                        "mode": (
                            "managed-copy-failed-rolled-back"
                            if automatic_rollback
                            else "managed-copy-failed-rollback-failed"
                        ),
                        "safe_to_restart_previous": automatic_rollback,
                        "activation_error": {
                            "type": type(exc).__name__,
                            "message": sanitize_report_text(str(exc))[:500],
                        },
                        "activation_transaction": transaction,
                        "verify": verify(home),
                    }
                )
                _extend_rollback_commands(
                    result,
                    [str(item) for item in transaction.get("restore_commands", [])],
                )
                if private_state is not None:
                    try:
                        _advance_managed_transaction(
                            private_state,
                            (
                                "rolled_back"
                                if automatic_rollback
                                else "rollback_failed"
                            ),
                            snapshot=activation_snapshot,
                            plugin_backup_path=backup_path,
                            plugin_replaced=bool(plugin_mutation_started),
                            last_transaction=transaction,
                        )
                    except Exception as journal_exc:
                        result["safe_to_restart_previous"] = False
                        result["mode"] = "managed-journal-finalization-failed"
                        result["journal_error"] = {
                            "type": type(journal_exc).__name__,
                            "message": sanitize_report_text(str(journal_exc))[:500],
                        }
                return result
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
            managed_upgrade=managed_upgrade,
            degrade_vector=managed_flags["degrade_vector"],
            managed_state_dir=private_state,
        )
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"]["ok"])
    return result


def _managed_retry_rollback_epoch(
    phase: str,
    snapshot: dict[str, Any],
) -> None:
    """Make an interrupted, already-restored SQLite compensation repeatable.

    We only accept the current database as the rollback pre-state when it is
    logically identical to the verified activation backup. Arbitrary drift is
    never adopted as installer-owned state.
    """

    if phase not in {"rollback_started", "rollback_failed"}:
        return
    sqlite_snapshot = snapshot.get("sqlite")
    if not isinstance(sqlite_snapshot, dict):
        raise InstallError("managed activation snapshot is missing SQLite state")
    current = Path(str(sqlite_snapshot.get("path") or ""))
    backup_raw = str(sqlite_snapshot.get("backup_path") or "")
    if not bool(sqlite_snapshot.get("preexisting")):
        if not current.exists():
            refresh_activation_sqlite_epoch(snapshot)
        return
    backup = Path(backup_raw) if backup_raw else Path()
    if not current.is_file() or not backup.is_file():
        return
    try:
        current_logical = sqlite_logical_fingerprint(current)
        backup_logical = sqlite_logical_fingerprint(backup)
    except Exception:
        return
    if current_logical == backup_logical:
        refresh_activation_sqlite_epoch(snapshot)


def resume_managed_upgrade(
    *,
    managed_state_dir: str | os.PathLike[str],
    hermes_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resume or safely compensate one sealed managed activation transaction."""

    raw_state = Path(managed_state_dir).expanduser().resolve(strict=False)
    initial = _read_managed_transaction(raw_state)
    recorded_home_raw = initial.get("hermes_home")
    if not isinstance(recorded_home_raw, str) or not recorded_home_raw.strip():
        raise InstallError("managed transaction Hermes home is missing")
    recorded_home = resolve_hermes_home(recorded_home_raw)
    if hermes_home is not None and resolve_hermes_home(hermes_home) != recorded_home:
        raise InstallError("managed transaction is bound to a different Hermes home")
    state_dir = _validate_managed_state_dir(
        recorded_home,
        raw_state,
        create=False,
    )
    current = _read_managed_transaction(state_dir)
    phase = str(current.get("phase") or "")
    target = Path(str(current.get("plugin_dir") or "")).expanduser().resolve(
        strict=False
    )
    expected_target = plugin_dir_for(recorded_home).resolve(strict=False)
    if target != expected_target:
        raise InstallError("managed transaction plugin path is outside the target home")
    raw_snapshot = current.get("snapshot")
    if not isinstance(raw_snapshot, dict):
        raise InstallError("managed transaction activation snapshot is missing")
    snapshot = dict(raw_snapshot)
    previous_plugin_existed = bool(current.get("previous_plugin_existed"))
    previous_version = str(current.get("previous_version") or "")
    target_version = str(current.get("target_version") or "")
    plugin_backup_path = str(current.get("plugin_backup_path") or "")
    plugin_replaced = bool(current.get("plugin_replaced"))

    if phase == "committed":
        return {
            "ok": True,
            "mode": "managed-already-committed",
            "resumed": False,
            "safe_to_restart_previous": False,
            "manifest_version": target_version,
            "activation_transaction": dict(current.get("last_transaction") or {}),
        }
    if phase == "rolled_back":
        return {
            "ok": False,
            "mode": "managed-already-rolled-back",
            "resumed": False,
            "safe_to_restart_previous": True,
            "manifest_version": previous_version,
            "activation_transaction": dict(current.get("last_transaction") or {}),
        }

    if phase == "snapshot_pending":
        if previous_plugin_existed:
            previous_identity_ok = (
                _read_manifest_name(target) == PLUGIN_NAME
                and _read_manifest_version(target) == previous_version
            )
        else:
            previous_identity_ok = not (path_exists(target) or path_is_symlink(target))
        if not previous_identity_ok:
            raise InstallError(
                "managed snapshot intent found unexpected plugin state"
            )
        cleanup = abort_interrupted_activation_capture(
            recorded_home,
            expected_lease_token=str(current.get("capture_lease_token") or ""),
        )
        if cleanup.get("ok") is not True:
            raise InstallError(
                "managed snapshot capture barrier could not be safely recovered"
            )
        transaction = {
            "status": "rolled_back",
            "automatic_rollback": True,
            "failures": [],
            "restore_commands": [],
        }
        _advance_managed_transaction(
            state_dir,
            "rolled_back",
            snapshot={},
            last_transaction=transaction,
        )
        return {
            "ok": False,
            "mode": "managed-snapshot-intent-recovered",
            "resumed": True,
            "mutation_started": False,
            "safe_to_restart_previous": True,
            "manifest_version": previous_version,
            "activation_transaction": transaction,
        }

    if phase in {"commit_started", "commit_cleanup_pending"}:
        if (
            _read_manifest_name(target) != PLUGIN_NAME
            or _read_manifest_version(target) != target_version
        ):
            raise InstallError(
                "managed commit cleanup found unexpected candidate identity"
            )
        transaction = committed_activation_receipt(
            snapshot,
            plugin_dir=target,
            previous_plugin_existed=previous_plugin_existed,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
        )
        committed = str(transaction.get("status") or "") == "committed"
        _advance_managed_transaction(
            state_dir,
            "committed" if committed else "commit_cleanup_pending",
            activation_step="complete" if committed else "commit_cleanup_pending",
            snapshot=snapshot,
            last_transaction=transaction,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
        )
        return {
            "ok": committed,
            "mode": (
                "managed-resume-commit-complete"
                if committed
                else "managed-commit-cleanup-pending"
            ),
            "resumed": True,
            "mutation_started": True,
            "safe_to_restart_previous": False,
            "manifest_version": target_version,
            "managed_retryable": not committed,
            "activation_transaction": transaction,
        }

    if phase in {"candidate_installed", "activating"}:
        if _read_manifest_name(target) != PLUGIN_NAME or (
            target_version and _read_manifest_version(target) != target_version
        ):
            phase = "candidate_identity_mismatch"
        else:
            result: dict[str, Any] = {
                "ok": True,
                "dry_run": False,
                "installed": True,
                "mode": "managed-resume-activation",
                "source_dir": str(target),
                "hermes_home": str(recorded_home),
                "plugin_dir": str(target),
                "manifest_version": target_version,
                "new_version": target_version,
                "previous_plugin_existed": previous_plugin_existed,
                "previous_version": previous_version,
                "backup_path": plugin_backup_path,
                "rollback_command": "",
                "rollback_commands": [],
                "activation_requested": True,
                "maintenance_mode_confirmed": True,
                "managed_upgrade": True,
                "activated": False,
                "mutation_started": True,
                "safe_to_restart_previous": False,
                "next_steps": [],
                "resumed": True,
            }
            return _activate_installed_target(
                recorded_home,
                target,
                result=result,
                snapshot=snapshot,
                previous_plugin_existed=previous_plugin_existed,
                previous_version=previous_version,
                plugin_backup_path=plugin_backup_path,
                plugin_replaced=plugin_replaced,
                managed_upgrade=True,
                degrade_vector=bool(current.get("requires_vector_degrade")),
                managed_state_dir=state_dir,
            )

    _managed_retry_rollback_epoch(phase, snapshot)
    try:
        _advance_managed_transaction(
            state_dir,
            "rollback_started",
            snapshot=snapshot,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
        )
    except Exception:
        # A valid handle was already loaded. Continue compensation with its
        # in-memory capability, but never authorize a restart without a final
        # durable receipt.
        pass
    transaction = compensate_activation_failure(
        snapshot,
        plugin_dir=target,
        previous_plugin_existed=previous_plugin_existed,
        previous_version=previous_version,
        plugin_backup_path=plugin_backup_path,
        plugin_replaced=plugin_replaced,
    )
    automatic_rollback = bool(transaction.get("automatic_rollback"))
    durable = True
    try:
        _advance_managed_transaction(
            state_dir,
            "rolled_back" if automatic_rollback else "rollback_failed",
            snapshot=snapshot,
            plugin_backup_path=plugin_backup_path,
            plugin_replaced=plugin_replaced,
            last_transaction=transaction,
        )
    except Exception:
        durable = False
    return {
        "ok": False,
        "mode": (
            "managed-resume-rolled-back"
            if automatic_rollback and durable
            else "managed-resume-manual-recovery-required"
        ),
        "resumed": True,
        "safe_to_restart_previous": automatic_rollback and durable,
        "manifest_version": previous_version if automatic_rollback else "",
        "activation_transaction": transaction,
        "verify": verify(recorded_home) if automatic_rollback else {"requested": False},
    }


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
