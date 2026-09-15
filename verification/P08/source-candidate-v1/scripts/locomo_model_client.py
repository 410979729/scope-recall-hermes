"""Fast, resumable Codex Responses client for the LoCoMo runner.

Credentials are read at call time from an existing Hermes auth store. Their
values are never copied into benchmark artifacts or exception messages.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any


class ModelRouteDriftError(RuntimeError):
    """Raised when a model call no longer matches its secret-free route receipt."""


class CodexModelClient:
    """Call one Codex-compatible model with bounded transport retries."""

    def __init__(
        self,
        auth_path: Path,
        *,
        timeout: float = 90.0,
        max_attempts: int = 6,
        expected_route: dict[str, Any] | None = None,
    ) -> None:
        self.auth_path = Path(auth_path)
        self.timeout = max(1.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.expected_route = (
            json.loads(json.dumps(expected_route, sort_keys=True))
            if isinstance(expected_route, dict)
            else None
        )

    def _credential_entry(self) -> dict[str, Any]:
        data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        pool = (data.get("credential_pool") or {}).get("openai-codex") or []
        entry: dict[str, Any] = dict(pool[0]) if pool else {}
        if not entry:
            provider = (data.get("providers") or {}).get("openai-codex") or {}
            tokens = provider.get("tokens") or {}
            entry = {
                "access_token": tokens.get("access_token"),
                "base_url": provider.get("base_url"),
                "account_id": provider.get("account_id"),
            }
        return entry

    @staticmethod
    def _route_fingerprint_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Return a secret-free digest for one already-selected credential entry."""

        base_url = str(
            entry.get("base_url") or "https://chatgpt.com/backend-api/codex"
        ).rstrip("/")
        identity_keys = (
            "account_id",
            "account",
            "email",
            "id",
            "label",
            "profile",
            "provider",
        )
        identity = {
            key: str(entry.get(key))
            for key in identity_keys
            if entry.get(key) not in (None, "")
        }
        canonical_identity = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "provider": "openai-codex",
            "protocol": "codex-responses",
            "base_url_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
            "credential_identity_fields": sorted(identity),
            "credential_identity_sha256": (
                hashlib.sha256(canonical_identity).hexdigest() if identity else None
            ),
        }

    def _credential(self) -> tuple[str, str]:
        entry = self._credential_entry()
        observed_route = self._route_fingerprint_for_entry(entry)
        if self.expected_route is not None and observed_route != self.expected_route:
            raise ModelRouteDriftError("model route identity changed after preflight")
        token = str(entry.get("access_token") or "")
        base_url = str(
            entry.get("base_url") or "https://chatgpt.com/backend-api/codex"
        ).rstrip("/")
        if not token:
            raise RuntimeError("openai-codex credential is unavailable")
        return token, base_url

    def route_fingerprint(self) -> dict[str, Any]:
        """Return a secret-free digest of the selected model transport route."""

        return self._route_fingerprint_for_entry(self._credential_entry())

    def complete(self, *, model: str, system: str, user: str) -> str:
        """Return model text or raise after retrying transient failures."""

        from scope_recall.nightly_llm import call_codex_responses_llm

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                token, base_url = self._credential()
                return call_codex_responses_llm(
                    user,
                    model=model,
                    base_url=base_url,
                    api_key=token,
                    timeout=self.timeout,
                    system_prompt=system,
                )
            except ModelRouteDriftError:
                raise
            except Exception as exc:  # transport/provider errors are retriable here
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                delay = min(30.0, 1.5 * (2 ** (attempt - 1)))
                time.sleep(delay + random.uniform(0.0, 0.5))
        error_type = type(last_error).__name__ if last_error is not None else "unknown"
        raise RuntimeError(
            f"model {model} failed after {self.max_attempts} attempts ({error_type})"
        ) from last_error
