"""Authorized host attachment metadata; reject untrusted paths and traversal."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_MIME = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/svg+xml",
        "text/plain",
        "application/json",
    }
)
_MAX_META_CHARS = 240
_MAX_BYTES = 16 * 1024 * 1024
_TRAVERSAL = re.compile(r"(^|[\\/])\.\.([\\/]|$)|^[a-zA-Z]+://")


@dataclass(frozen=True)
class AttachmentAuthorization:
    authorized: bool
    artifact_ref: str | None = None
    media_type: str | None = None
    sha256: str | None = None
    byte_length: int | None = None
    gap: str | None = None


def attachment_gap(reason: str) -> str:
    return f"attachment_gap:{reason}"


def _bounded_text(value: object, field: str) -> str | None:
    if type(value) is not str:
        return None
    text = value.strip()
    if not text or len(text) > _MAX_META_CHARS:
        return None
    if _TRAVERSAL.search(text):
        return None
    return text


def _bounded_digest(value: object) -> str | None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        return None
    return value


def _bounded_bytes(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= _MAX_BYTES:
        return None
    return value


def authorize_attachment_metadata(payload: dict[str, Any]) -> AttachmentAuthorization:
    """Accept only documented authorized host event shapes with bounded metadata."""

    if not isinstance(payload, dict):
        return AttachmentAuthorization(False, gap=attachment_gap("unsupported_shape"))
    filename = _bounded_text(payload.get("filename"), "filename")
    media_type = _bounded_text(payload.get("mediaType") or payload.get("media_type"), "media_type")
    sha256 = _bounded_digest(payload.get("sha256"))
    byte_length = _bounded_bytes(payload.get("byte_length") or payload.get("byteLength"))
    reference = _bounded_text(payload.get("reference") or payload.get("artifact_ref"), "reference")

    if payload.get("path") or payload.get("url"):
        return AttachmentAuthorization(False, gap=attachment_gap("untrusted_path"))
    raw_path = payload.get("raw_path")
    if raw_path is not None:
        return AttachmentAuthorization(False, gap=attachment_gap("untrusted_path"))

    svg = payload.get("svg")
    if svg is not None:
        if type(svg) is not str or not svg.startswith("<svg ") or len(svg.encode("utf-8")) > _MAX_BYTES:
            return AttachmentAuthorization(False, gap=attachment_gap("svg_bounds"))
        digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()
        return AttachmentAuthorization(
            True,
            artifact_ref=reference or f"artifact:svg:{digest[:16]}",
            media_type="image/svg+xml",
            sha256=digest,
            byte_length=len(svg.encode("utf-8")),
        )

    if filename and media_type in _ALLOWED_MIME and sha256 and byte_length:
        return AttachmentAuthorization(
            True,
            artifact_ref=reference or f"artifact:{media_type}:{sha256[:16]}",
            media_type=media_type,
            sha256=sha256,
            byte_length=byte_length,
        )

    if filename and media_type and not sha256:
        return AttachmentAuthorization(False, gap=attachment_gap("missing_hash"))
    if filename and sha256 and not media_type:
        return AttachmentAuthorization(False, gap=attachment_gap("missing_mime"))
    return AttachmentAuthorization(False, gap=attachment_gap("unsupported_shape"))
