"""Setup-time storage, vector companion, and save_config bootstrap.

RuntimeComposition owns this module. Provider keeps one-line delegates so
legacy callers and instance monkeypatches still resolve. Connect, schema,
authorizer, vector, and save-config hooks are injected by the outer Provider
compatibility boundary, so this module never inspects Provider modules.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ...config import load_runtime_config, save_runtime_config
from ...journal_store import ensure_journal_schema
from ...maintenance_lease import (
    MaintenanceLeaseError,
    activation_lease_status,
    ensure_activation_guard_triggers,
    install_activation_lease_authorizer,
)
from ...sql_store import ensure_schema
from ...truth_connection import connect_truth_database
from ...vector_bootstrap import bootstrap_fresh_vector_companion
from ...write_kernel import holding_truth_writer_lease
from .hook_contract import RuntimeHooks
from .storage import (
    configure_published_writer_connection,
    open_configured_truth_connection,
)

DEFAULT_BUSY_TIMEOUT_SECONDS = 10.0


def _busy_timeout(hooks: RuntimeHooks) -> float:
    return float(hooks.resolve("SQLITE_BUSY_TIMEOUT_SECONDS", DEFAULT_BUSY_TIMEOUT_SECONDS))


def bootstrap_storage(
    adapter: Any,
    hermes_home: str | os.PathLike[str],
    *,
    hooks: RuntimeHooks,
    activation_lease_token: str = "",
) -> None:
    """Create the empty SQLite truth/journal schema during `hermes memory setup`.

    Gateway sessions can lazily construct agents only after the first user
    message. Bootstrapping here gives operators an immediate, visible setup
    artifact instead of a false-negative "no database yet" verification gap.
    """

    storage_dir = Path(hermes_home).expanduser() / "scope-recall"
    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "memory.sqlite3"
    opener = hooks.resolve(
        "open_configured_truth_connection", open_configured_truth_connection
    )
    conn = opener(
        db_path,
        timeout=_busy_timeout(hooks),
        connect_fn=hooks.resolve("connect_truth_database", connect_truth_database),
        schema_fn=hooks.resolve("ensure_schema", ensure_schema),
        journal_fn=hooks.resolve("ensure_journal_schema", ensure_journal_schema),
        install_authorizer_fn=hooks.resolve(
            "install_activation_lease_authorizer",
            install_activation_lease_authorizer,
        ),
        ensure_triggers_fn=hooks.resolve(
            "ensure_activation_guard_triggers",
            ensure_activation_guard_triggers,
        ),
        lease_token=activation_lease_token,
        row_factory=sqlite3.Row,
    )
    try:
        load_config = hooks.resolve("load_runtime_config", load_runtime_config)
        plugin_dir = getattr(adapter, "_plugin_dir", None) or Path(__file__).resolve().parents[2]
        runtime_config = load_config(plugin_dir, storage_dir)
        bootstrap_vector_companion(
            adapter,
            storage_dir,
            runtime_config,
            hooks=hooks,
            truth_conn=conn,
        )
        conn.commit()
    finally:
        conn.close()


def bootstrap_vector_companion(
    adapter: Any,
    storage_dir: Path,
    runtime_config: dict[str, Any],
    *,
    hooks: RuntimeHooks,
    truth_conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Delegate fresh primary/fallback selection to the bootstrap policy."""

    bootstrap_fn = hooks.resolve(
        "bootstrap_fresh_vector_companion", bootstrap_fresh_vector_companion
    )
    result = bootstrap_fn(
        storage_dir,
        runtime_config,
        truth_conn=truth_conn,
    )
    adapter._last_vector_bootstrap = result
    return result


def open_runtime_connection(
    adapter: Any, *, hooks: RuntimeHooks
) -> sqlite3.Connection:
    """Open and fully configure one live provider-owned SQLite connection."""

    if getattr(adapter, "_db_path", None) is None:
        raise RuntimeError("Scope Recall database path is not initialized")
    lease_status = hooks.resolve("activation_lease_status", activation_lease_status)(
        adapter._db_path
    )
    lease_error = hooks.resolve("MaintenanceLeaseError", MaintenanceLeaseError)
    if lease_status["status"] == "stale":
        raise lease_error(
            "stale activation maintenance lease blocks startup; inspect with "
            "python scripts/recover.activation_lease.py --dry-run"
        )
    if lease_status["status"] == "active":
        raise lease_error(
            "Scope Recall startup is blocked by an active activation maintenance lease"
        )
    configure = hooks.resolve(
        "configure_published_writer_connection",
        configure_published_writer_connection,
    )
    return configure(
        adapter,
        timeout=_busy_timeout(hooks),
        connect_fn=hooks.resolve("connect_truth_database", connect_truth_database),
        authorizer_fn=hooks.resolve(
            "install_activation_lease_authorizer",
            install_activation_lease_authorizer,
        ),
        schema_fn=hooks.resolve("ensure_schema", ensure_schema),
        journal_fn=hooks.resolve("ensure_journal_schema", ensure_journal_schema),
        ensure_triggers_fn=hooks.resolve(
            "ensure_activation_guard_triggers",
            ensure_activation_guard_triggers,
        ),
    )


def save_config(
    adapter: Any,
    values: dict[str, Any],
    hermes_home: str,
    *,
    hooks: RuntimeHooks,
    activation_lease_token: str = "",
) -> None:
    """Persist operator overlay, then bootstrap empty truth/vector artifacts."""

    storage_dir = Path(hermes_home).expanduser() / "scope-recall"
    hold = hooks.resolve("holding_truth_writer_lease", holding_truth_writer_lease)
    persist = hooks.resolve("save_runtime_config", save_runtime_config)
    with hold(storage_dir, role="save_config"):
        persist(values or {}, hermes_home)
        bootstrap_storage(
            adapter,
            hermes_home,
            hooks=hooks,
            activation_lease_token=activation_lease_token,
        )


class RuntimeBootstrap:
    """Composition-held setup owner. Provider must not keep this logic."""

    def __init__(self, adapter: Any, hooks: RuntimeHooks) -> None:
        self.adapter = adapter
        self.hooks = hooks

    def save_config(
        self,
        values: dict[str, Any],
        hermes_home: str,
        *,
        activation_lease_token: str = "",
    ) -> None:
        return save_config(
            self.adapter,
            values,
            hermes_home,
            hooks=self.hooks,
            activation_lease_token=activation_lease_token,
        )

    def bootstrap_storage(
        self,
        hermes_home: str | os.PathLike[str],
        *,
        activation_lease_token: str = "",
    ) -> None:
        return bootstrap_storage(
            self.adapter,
            hermes_home,
            hooks=self.hooks,
            activation_lease_token=activation_lease_token,
        )

    def bootstrap_vector_companion(
        self,
        storage_dir: Path,
        runtime_config: dict[str, Any],
        *,
        truth_conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        return bootstrap_vector_companion(
            self.adapter,
            storage_dir,
            runtime_config,
            hooks=self.hooks,
            truth_conn=truth_conn,
        )

    def open_runtime_connection(self) -> sqlite3.Connection:
        return open_runtime_connection(self.adapter, hooks=self.hooks)
