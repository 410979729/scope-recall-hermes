"""Stdlib-only endpoint policy shared by model routes and the isolated HTTP worker.

URL credentials are refused even on HTTPS; provider credentials belong in
headers. Plain HTTP is permitted on loopback or by a literal opt-in, but it
never carries credential headers. This module does not open sockets.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

_CREDENTIAL_KEYS = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "apikey",
        "apitoken",
        "auth",
        "authorization",
        "authtoken",
        "awsaccesskeyid",
        "bearertoken",
        "chatgptaccountid",
        "clientassertion",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "idtoken",
        "key",
        "ocpapimsubscriptionkey",
        "password",
        "privatekey",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "secretkey",
        "securitytoken",
        "sessiontoken",
        "setcookie",
        "sig",
        "signature",
        "subscriptionkey",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xapikey",
        "xauthtoken",
        "xgoogapikey",
        "xgoogcredential",
        "xgoogsignature",
        "xopenaiapikey",
        "xtoken",
    }
)
_CREDENTIAL_SUFFIXES = (
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
    "signature",
    "credential",
    "authorization",
)


def is_credential_key(name: object) -> bool:
    """Classify query/header names; ambiguous percent encodings fail closed.

    Decode once, not until a guessed depth: a remaining percent sign is refused
    rather than letting a downstream server give an encoded key new meaning.
    Values are neither logged nor needed to decide whether a name is sensitive.
    """
    if type(name) is not str or not name or len(name) > 384:
        return True
    try:
        decoded = urllib.parse.unquote_plus(name, errors="strict")
    except UnicodeError:
        return True
    if "%" in decoded or len(decoded) > 128:
        return True
    normalized = "".join(char for char in decoded.casefold() if char.isalnum())
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(_CREDENTIAL_SUFFIXES)


def is_loopback_host(value: object) -> bool:
    """Accept localhost names and numeric addresses defined as loopback."""
    host = str(value or "").rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_url_shape_ok(endpoint: object) -> bool:
    """Require HTTP(S), a valid authority, and no URL credentials or fragments.

    This is independent of host policy: a descriptor can represent a private
    HTTP endpoint without itself granting permission to connect to it.
    """
    if type(endpoint) is not str or not endpoint or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint):
        return False
    if "\\" in endpoint or "#" in endpoint:
        return False
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    fields = parsed.query.replace(";", "&").split("&")
    return len(fields) <= 64 and not any(is_credential_key(field.partition("=")[0]) for field in fields if field)


def endpoint_scheme_allowed(endpoint: str, *, allow_insecure: bool = False) -> bool:
    """Apply safe shape and host policy, without coercing the plaintext opt-in."""
    if type(allow_insecure) is not bool or not endpoint_url_shape_ok(endpoint):
        return False
    parsed = urllib.parse.urlsplit(endpoint)
    return parsed.scheme == "https" or is_loopback_host(parsed.hostname) or allow_insecure
