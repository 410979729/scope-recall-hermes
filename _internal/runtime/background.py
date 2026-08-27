"""Background journal digest and auto experience follow-up.

Owns schedule / start / single-flight / shutdown-blocked starts / joinable
digest state. Provider keeps same-name thin wrappers so existing tests can
still patch ``scope_recall.provider.run_journal_digest``.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, cast

from ...gating import config_bool

logger = logging.getLogger(__name__)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def active_run_journal_digest() -> Callable[..., dict[str, Any]]:
    """Resolve the production digest function through the provider module.

    Tests monkeypatch ``scope_recall.provider.run_journal_digest``. A static
    import inside this module would ignore that patch.
    """

    provider_mod = sys.modules.get("scope_recall.provider")
    if provider_mod is not None:
        fn = getattr(provider_mod, "run_journal_digest", None)
        if callable(fn):
            return cast(Callable[..., dict[str, Any]], fn)
    from ...journal import run_journal_digest

    return run_journal_digest


class BackgroundWork:
    """Single owner of background digest scheduling and joinable state."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.thread: threading.Thread | None = None
        self.lock = threading.RLock()
        self.last_started = 0.0
        self.last_finished = 0.0
        self.last_status = "never_run"
        self.last_error = ""
        self.consecutive_failures = 0
        self.needs_resume = False

    def maybe_start_journal_digest(self) -> None:
        provider = self._provider
        shutdown = getattr(provider, "_shutdown_requested", None)
        is_set = getattr(shutdown, "is_set", None)
        if callable(is_set) and is_set():
            return
        blocked = getattr(provider, "_truth_writes_blocked", None)
        if callable(blocked) and blocked():
            return
        isolated = getattr(provider, "_memory_isolated_for_scope", None)
        if callable(isolated) and isolated():
            return
        if getattr(provider, "_hermes_home", None) is None:
            return
        scope = getattr(provider, "_scope", None)
        if getattr(scope, "agent_context", None) != "primary":
            return
        journal_config_fn = getattr(provider, "_journal_config", None)
        journal_config = journal_config_fn() if callable(journal_config_fn) else {}
        if not isinstance(journal_config, dict):
            journal_config = {}
        if not config_bool(journal_config, "enabled", True):
            return
        if not config_bool(journal_config, "background_digest_enabled", True):
            return
        coerce = getattr(provider, "_coerce_journal_float", None)
        if not callable(coerce):
            return
        interval_hours = _as_float(coerce(journal_config, "digest_interval_hours", 2.0), 2.0)
        drain_while_idle = config_bool(journal_config, "background_digest_drain_while_idle", True)
        min_restart = _as_float(
            coerce(journal_config, "background_digest_min_restart_seconds", 2.0), 2.0
        )
        if interval_hours <= 0 and not drain_while_idle:
            return
        now = time.time()
        with self.lock:
            if callable(is_set) and is_set():
                return
            if self.thread is not None and self.thread.is_alive():
                return
            if drain_while_idle:
                last = float(self.last_started or 0.0)
                if last and now - last < max(0.0, min_restart):
                    return
            elif not self.needs_resume:
                if interval_hours <= 0:
                    return
                if self.last_started and now - self.last_started < interval_hours * 3600:
                    return
            self.last_started = now
            self.last_status = "running"
            self.last_error = ""
            self.needs_resume = False
            if config_bool(journal_config, "background_digest_synchronous", False):
                current_thread = threading.current_thread()
                self.thread = current_thread
                try:
                    self.run_digest(journal_config)
                finally:
                    if self.thread is current_thread:
                        self.thread = None
                return
            thread = threading.Thread(
                target=self.run_digest,
                args=(dict(journal_config),),
                name="scope-recall-journal-digest",
                daemon=True,
            )
            self.thread = thread
            thread.start()

    def run_digest(
        self,
        journal_config: dict[str, Any],
        *,
        digest_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        from ..journal.runtime import run_provider_background_journal_digest

        run_provider_background_journal_digest(
            self._provider,
            journal_config,
            digest_fn=digest_fn or active_run_journal_digest(),
        )

    def run_session_end_digest(
        self,
        *,
        digest_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        from ..journal.runtime import run_provider_session_end_journal_digest

        run_provider_session_end_journal_digest(
            self._provider,
            digest_fn=digest_fn or active_run_journal_digest(),
        )

    def maybe_promote(self, *, trigger: str) -> None:
        provider = self._provider
        shutdown = getattr(provider, "_shutdown_requested", None)
        is_set = getattr(shutdown, "is_set", None)
        if callable(is_set) and is_set():
            return
        isolated = getattr(provider, "_memory_isolated_for_scope", None)
        if callable(isolated) and isolated():
            return
        scope = getattr(provider, "_scope", None)
        if getattr(scope, "agent_context", None) != "primary":
            return
        raw_experience_config = getattr(provider, "_config", {}).get("experience")
        experience_config = raw_experience_config if isinstance(raw_experience_config, dict) else {}
        if not config_bool(experience_config, "enabled", True):
            return
        if not config_bool(experience_config, "auto_promotion_enabled", False):
            return
        from ..experience.runtime import run_experience_promotion

        try:
            limit_sessions = int(experience_config.get("auto_promotion_limit_sessions") or 20)
        except (TypeError, ValueError):
            limit_sessions = 20
        try:
            result = run_experience_promotion(
                provider,
                limit_sessions=max(1, limit_sessions),
            )
            logger.info("Scope Recall auto experience promotion after %s: %s", trigger, result)
        except Exception:
            rollback = getattr(provider, "_rollback_conn_after_error", None)
            if callable(rollback):
                rollback(f"auto experience promotion after {trigger}")
            logger.exception("Scope Recall auto experience promotion failed after %s", trigger)

    def maybe_adjudicate(self, *, trigger: str) -> None:
        from ...auto_adjudication import run_provider_auto_adjudication

        run_provider_auto_adjudication(self._provider, trigger=trigger)

    def join_digest(self, timeout: float) -> None:
        thread = self.thread
        if thread is None:
            return
        if thread is threading.current_thread():
            raise RuntimeError("Scope Recall journal digest cannot shut down its own provider")
        wait_timeout = max(0.0, float(timeout))
        if thread.is_alive():
            thread.join(timeout=wait_timeout)
        if thread.is_alive():
            raise RuntimeError(
                "Scope Recall journal digest did not acknowledge shutdown before timeout"
            )
        with self.lock:
            if self.thread is thread:
                self.thread = None
