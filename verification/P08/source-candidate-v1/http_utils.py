"""HTTP endpoint and transport safety for hosted providers.

Every outbound request that can carry memory text or provider credentials must
pass through this module.  SQLite/local-only code should not depend on it.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

try:  # Support package imports and direct plugin scripts.
    from .capture_filters import redact_secret_like_text
except ImportError:  # pragma: no cover - direct script import style
    from capture_filters import redact_secret_like_text

logger = logging.getLogger(__name__)
DEFAULT_USER_AGENT = (
    "ScopeRecall/2.0 (+https://github.com/410979729/scope-recall-hermes)"
)

_PUBLIC_ENDPOINT_PATH_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/responses",
    "/responses",
    "/v1/messages",
    "/v1/embeddings",
    "/embeddings",
)

SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        "x-goog-api-key",
        "x-openai-api-key",
        "ocp-apim-subscription-key",
        "x-auth-token",
        "subscription-key",
        "auth-token",
        "bearer-token",
        "client-assertion",
        "sig",
        "x-goog-credential",
        "x-goog-signature",
        "x-amz-security-token",
        "awsaccesskeyid",
        "cookie",
        "set-cookie",
        "chatgpt-account-id",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "apitoken",
        "xtoken",
        "xapikey",
        "xgoogapikey",
        "xopenaiapikey",
        "ocpapimsubscriptionkey",
        "xauthtoken",
        "subscriptionkey",
        "authtoken",
        "bearertoken",
        "clientassertion",
        "sig",
        "key",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "accesskey",
        "accesskeyid",
        "sessiontoken",
        "securitytoken",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "xamzcredential",
        "xamzsignature",
        "xgoogcredential",
        "xgoogsignature",
        "xamzsecuritytoken",
        "awsaccesskeyid",
        "signature",
        "secret",
        "clientsecret",
        "privatekey",
        "secretkey",
        "password",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "chatgptaccountid",
    }
)
_CREDENTIAL_KEY_SUFFIXES = (
    "apikey",
    "apitoken",
    "authtoken",
    "bearertoken",
    "subscriptionkey",
    "clientassertion",
    "accesskeyid",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "sessiontoken",
    "securitytoken",
    "clientsecret",
    "privatekey",
    "secretkey",
    "password",
)
_MAX_QUERY_FIELDS = 64
_MAX_ENCODED_KEY_DECODE_PASSES = 8
_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?:[0-9A-Fa-f]{2})?")
_WARNED_INSECURE_ORIGINS: set[tuple[str, str, int]] = set()
_WARNED_INSECURE_ORIGINS_LOCK = threading.Lock()


class UnsafeEndpointError(ValueError):
    """Fail-closed endpoint or redirect policy violation."""


@dataclass(frozen=True)
class EndpointPolicy:
    """Validated endpoint properties used by urllib and SDK transports."""

    url: str
    scheme: str
    host: str
    port: int
    insecure: bool
    allow_credentials: bool

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)


def redact_sensitive(text: Any) -> str:
    """Redact HTTP/provider diagnostics with the canonical capture taxonomy."""

    return redact_secret_like_text(text).replace(
        "[REDACTED_SECRET]",
        "[REDACTED]",
    )


def safe_endpoint_display(url: str, *, public_path_only: bool = True) -> str:
    """Render an endpoint without query/userinfo or arbitrary configured paths.

    The default exposes only a recognized provider API suffix. Callers must set
    ``public_path_only=False`` explicitly for internal debugging and independently
    sanitize any configured path before exposing that result outside the process.
    """

    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        scheme = parsed.scheme.casefold() or "unknown"
        host = parsed.hostname or "missing-host"
        try:
            port = parsed.port
        except ValueError:
            port = None
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        netloc = f"{display_host}:{port}" if port is not None else display_host
        path = parsed.path or ""
        if public_path_only:
            path = next(
                (
                    suffix
                    for suffix in _PUBLIC_ENDPOINT_PATH_SUFFIXES
                    if path.casefold().endswith(suffix)
                ),
                "",
            )
        return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))
    except Exception:
        return "invalid-endpoint"


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _require_boolean_opt_in(value: Any) -> bool:
    """Reject non-boolean values at the final plaintext transport boundary."""

    if not isinstance(value, bool):
        raise UnsafeEndpointError("allow_insecure_endpoint must be a boolean")
    return value


def explicit_insecure_endpoint_opt_in(value: object) -> bool:
    """Return true only for the literal boolean ``True`` security opt-in.

    Config and public-option resolvers use this before reaching transports so
    strings, numerics, arrays, and objects cannot be normalized into permission.
    Final transports still reject non-boolean direct inputs independently.
    """

    return value is True


def _normalize_credential_key(raw_key: str) -> str:
    """Normalize spelling and nested percent encoding for credential policy.

    Query/header names are policy-bearing bytes, so a fixed one- or two-pass
    decoder lets attackers choose the depth that bypasses the contract. Decode
    until stable (bounded for obfuscation bombs), reject malformed or residual
    escapes, and reject decoded names that are still unreasonably long.
    """

    decoded_key = str(raw_key or "")
    for _ in range(_MAX_ENCODED_KEY_DECODE_PASSES):
        try:
            next_key = urllib.parse.unquote_plus(
                decoded_key,
                encoding="utf-8",
                errors="strict",
            )
        except UnicodeDecodeError as exc:
            raise UnsafeEndpointError(
                "encoded query/header key is malformed"
            ) from exc
        if next_key == decoded_key:
            break
        if not next_key:
            raise UnsafeEndpointError("encoded query/header key is malformed")
        if len(next_key) > max(len(decoded_key), 256):
            raise UnsafeEndpointError("encoded query/header key is too large")
        decoded_key = next_key
    else:
        raise UnsafeEndpointError("encoded query/header key is too deeply encoded")

    if "%" in decoded_key:
        raise UnsafeEndpointError("encoded query/header key is malformed")
    if len(decoded_key) > 128:
        raise UnsafeEndpointError("query/header key is too large")
    return "".join(
        character for character in decoded_key.casefold() if character.isalnum()
    )


def _iter_query_pairs(query: str) -> list[tuple[str, str]]:
    """Parse a bounded query and validate raw keys before percent decoding."""

    fields = str(query or "").replace(";", "&").split("&")
    if len(fields) > _MAX_QUERY_FIELDS:
        raise UnsafeEndpointError("endpoint URL query is malformed or too large")
    pairs: list[tuple[str, str]] = []
    for field in fields:
        if not field:
            continue
        raw_key, separator, raw_value = field.partition("=")
        for match in _PERCENT_ESCAPE_PATTERN.finditer(raw_key):
            if len(match.group(0)) != 3:
                raise UnsafeEndpointError("endpoint URL query key is malformed")
        pairs.append(
            (
                raw_key,
                urllib.parse.unquote_plus(raw_value if separator else ""),
            )
        )
    return pairs


def is_credential_key(raw_key: str) -> bool:
    """Apply one canonical credential-key contract to URL and header names.

    SDK transports must call this helper rather than iterating the legacy
    exact-spelling header set, because header names can use the same case,
    punctuation, and bounded encoding variants as endpoint query keys.
    Unparseable or excessively encoded keys fail closed as credentials.
    """

    try:
        normalized = _normalize_credential_key(raw_key)
    except UnsafeEndpointError:
        return True
    return normalized in _CREDENTIAL_KEYS or any(
        normalized.endswith(suffix) for suffix in _CREDENTIAL_KEY_SUFFIXES
    )


def _query_contains_credentials(query: str) -> bool:
    """Return whether a URL query contains a credential-bearing key."""

    pairs = _iter_query_pairs(query)
    for key, _value in pairs:
        normalized = _normalize_credential_key(key)
        if normalized in _CREDENTIAL_KEYS or any(
            normalized.endswith(suffix) for suffix in _CREDENTIAL_KEY_SUFFIXES
        ):
            return True
    return False


def _warn_insecure_once(policy: EndpointPolicy) -> None:
    with _WARNED_INSECURE_ORIGINS_LOCK:
        if policy.origin in _WARNED_INSECURE_ORIGINS:
            return
        _WARNED_INSECURE_ORIGINS.add(policy.origin)
    logger.warning(
        "Scope Recall is using insecure HTTP endpoint %s; credential-bearing headers will be stripped",
        safe_endpoint_display(policy.url, public_path_only=True),
    )


def require_safe_endpoint(
    url: str,
    *,
    allow_insecure: bool = False,
) -> EndpointPolicy:
    """Validate one outbound endpoint and return its transport policy.

    HTTPS may carry credentials.  Loopback HTTP remains compatible for local
    Ollama/LM Studio endpoints, while non-loopback HTTP requires an explicit
    opt-in.  No HTTP request may carry credential-bearing headers.
    """

    allow_insecure = _require_boolean_opt_in(allow_insecure)
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeEndpointError("endpoint URL is required")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise UnsafeEndpointError("endpoint URL contains control characters")
    if "\\" in raw:
        raise UnsafeEndpointError("endpoint URL contains an ambiguous backslash")

    try:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").rstrip(".").casefold()
        port = parsed.port
    except ValueError as exc:
        raise UnsafeEndpointError("endpoint URL is malformed") from exc

    if scheme not in {"http", "https"}:
        raise UnsafeEndpointError(
            f"unsafe endpoint scheme {scheme or '(missing)'}; only HTTP(S) is supported"
        )
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeEndpointError("endpoint URL must not contain embedded credentials")
    if parsed.fragment:
        raise UnsafeEndpointError("endpoint URL must not contain a fragment")
    if _query_contains_credentials(parsed.query):
        raise UnsafeEndpointError("endpoint URL must not contain credential-like query parameters")
    if not host:
        raise UnsafeEndpointError("endpoint URL must contain a host")

    effective_port = int(port or (443 if scheme == "https" else 80))
    insecure = scheme == "http"
    if insecure and not (_is_loopback_host(host) or allow_insecure):
        raise UnsafeEndpointError(
            "non-loopback HTTP endpoint is disabled; set allow_insecure_endpoint=true "
            "only for an explicitly trusted endpoint"
        )

    policy = EndpointPolicy(
        url=raw,
        scheme=scheme,
        host=host,
        port=effective_port,
        insecure=insecure,
        allow_credentials=not insecure,
    )
    if insecure:
        _warn_insecure_once(policy)
    return policy


def sanitize_headers_for_endpoint(
    url: str,
    headers: Mapping[str, str],
    *,
    allow_insecure: bool = False,
) -> dict[str, str]:
    """Return request headers allowed by the endpoint transport policy."""

    policy = require_safe_endpoint(url, allow_insecure=allow_insecure)
    output = {str(name): str(value) for name, value in headers.items()}
    if policy.allow_credentials:
        return output
    return {
        name: value
        for name, value in output.items()
        if not is_credential_key(name)
    }


def prepare_safe_request(
    request: urllib.request.Request,
    *,
    allow_insecure: bool = False,
) -> urllib.request.Request:
    """Clone a request after validating its URL and filtering unsafe headers."""

    policy = require_safe_endpoint(request.full_url, allow_insecure=allow_insecure)
    headers = sanitize_headers_for_endpoint(
        policy.url,
        dict(request.header_items()),
        allow_insecure=allow_insecure,
    )
    # Set this at the shared transport boundary so nightly, capture, and
    # embedding calls all identify the client. Preserve provider-specific UAs.
    if not any(name.casefold() == "user-agent" for name in headers):
        headers["User-Agent"] = DEFAULT_USER_AGENT
    return urllib.request.Request(
        policy.url,
        data=request.data,
        headers=headers,
        origin_req_host=request.origin_req_host,
        unverifiable=request.unverifiable,
        method=request.get_method(),
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only within the validated original origin."""

    def __init__(self, *, allow_insecure: bool) -> None:
        super().__init__()
        self._allow_insecure = _require_boolean_opt_in(allow_insecure)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        source = require_safe_endpoint(
            req.full_url,
            allow_insecure=self._allow_insecure,
        )
        target_scheme = urllib.parse.urlsplit(str(newurl or "").strip()).scheme.casefold()
        if source.scheme == "https" and target_scheme == "http":
            raise UnsafeEndpointError("HTTPS-to-HTTP redirect downgrade is forbidden")
        target = require_safe_endpoint(
            newurl,
            allow_insecure=self._allow_insecure,
        )
        if source.origin != target.origin:
            raise UnsafeEndpointError(
                "cross-origin redirect is forbidden for credentialed memory requests"
            )
        if code in {307, 308}:
            # Python 3.11's default handler rejects POST 307/308 even though
            # those status codes explicitly preserve method and body.  Build
            # the same-origin request ourselves, then re-apply header policy.
            redirected = urllib.request.Request(
                newurl,
                data=req.data,
                headers=dict(req.header_items()),
                origin_req_host=req.origin_req_host,
                unverifiable=True,
                method=req.get_method(),
            )
        else:
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is None:
                return None
        return prepare_safe_request(
            redirected,
            allow_insecure=self._allow_insecure,
        )


def safe_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
    allow_insecure: bool = False,
) -> Any:
    """Open a validated request with same-origin-only redirect handling."""

    prepared = prepare_safe_request(request, allow_insecure=allow_insecure)
    opener = urllib.request.build_opener(
        _SafeRedirectHandler(allow_insecure=allow_insecure)
    )
    return opener.open(prepared, timeout=timeout)


def chat_completions_endpoint(
    base_url: str,
    *,
    endpoint: str = "",
    append_v1: bool = True,
    allow_insecure_endpoint: bool = False,
) -> str:
    """Build and validate an OpenAI-compatible chat-completions endpoint."""

    explicit = str(endpoint or "").strip().rstrip("/")
    if explicit:
        candidate = explicit
    else:
        root = str(base_url or "").strip().rstrip("/") or "https://api.openai.com"
        if root.endswith("/chat/completions"):
            candidate = root
        elif root.endswith("/v1"):
            candidate = root + "/chat/completions"
        else:
            suffix = "/v1/chat/completions" if append_v1 else "/chat/completions"
            candidate = root + suffix
    return require_safe_endpoint(
        candidate,
        allow_insecure=allow_insecure_endpoint,
    ).url


__all__ = [
    "EndpointPolicy",
    "SENSITIVE_REQUEST_HEADERS",
    "UnsafeEndpointError",
    "chat_completions_endpoint",
    "is_credential_key",
    "prepare_safe_request",
    "redact_sensitive",
    "require_safe_endpoint",
    "safe_endpoint_display",
    "safe_urlopen",
    "sanitize_headers_for_endpoint",
]
