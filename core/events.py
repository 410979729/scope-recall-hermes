"""Pure source admission. No database, host, environment, or model calls."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata

from .capture_filters import sanitize_source_capture_text
from ..contracts import ContractError, SourceEvent, TrustedContext, decode_payload, validate_capture
from .secret_patterns import contains_secret_like_text


MAX_SEGMENT_CHARS = 65536


@dataclass(frozen=True)
class PreparedCapture:
    events: tuple[SourceEvent, ...]
    gaps: tuple[str, ...] = ()
    rejection: str | None = None


def _secret(value: object) -> bool:
    if isinstance(value, str):
        return contains_secret_like_text(value)
    if isinstance(value, dict):
        return any(_secret(k) or _secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_secret(v) for v in value)
    return False


def prepare_capture(value: SourceEvent | dict | str | bytes, context: TrustedContext) -> PreparedCapture:
    raw = decode_payload(dict(value) if isinstance(value, dict) else value)
    content = raw.get("content")
    if type(content) is not str:
        raise ContractError("INPUT_INVALID", "content")
    # Validate the envelope before splitting a bounded long host message. The
    # original content is never repaired or supplied to a model to pass schema.
    validate_capture({**raw, "content": ""}, context)
    if not raw["source_event_key"].strip() or "\x00" in raw["source_event_key"]:
        raise ContractError("INPUT_INVALID", "source_event_key")
    if _secret(raw):
        return PreparedCapture((), rejection="plaintext_secret_rejected")
    filtered = sanitize_source_capture_text(content)
    gaps = ("transport_payload_omitted",) if filtered != content else ()
    if not filtered.strip() and not raw.get("artifact_refs") and raw["capture_state"] == "complete":
        return PreparedCapture((), gaps, "empty_source")
    state = "partial" if gaps and raw["capture_state"] == "complete" else raw["capture_state"]
    if len(filtered) <= MAX_SEGMENT_CHARS:
        event = validate_capture({**raw, "content": filtered, "capture_state": state}, context)
        return PreparedCapture((event,), gaps)
    if "segment" in raw:
        raise ContractError("INPUT_INVALID", "nested_oversize_segment")
    # Stable ordinal chunks preserve every character. Hashing the occurrence
    # key keeps generated keys bounded; content never supplies identity.
    prefix = "segmented-" + hashlib.sha256(raw["source_event_key"].encode("utf-8")).hexdigest()
    total = (len(filtered) + MAX_SEGMENT_CHARS - 1) // MAX_SEGMENT_CHARS
    events = []
    for index in range(total):
        event = {**raw, "source_event_key": f"{prefix}/{index}",
                 "content": filtered[index*MAX_SEGMENT_CHARS:(index+1)*MAX_SEGMENT_CHARS],
                 "capture_state": state,
                 "segment": {"group_key": raw["source_event_key"], "index": index,
                             "total": total, "truncated": bool(gaps)}}
        events.append(validate_capture(event, context))
    return PreparedCapture(tuple(events), gaps)


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")
_FILENAME = re.compile(r"(?<![\w-])[\w-]+(?:\.[\w-]+)+", re.UNICODE)
#: A dotted release version with a pre-release or build suffix, after
#: normalization: 3.1.0rc28, v3.1.0-rc28, 3.1.0.dev2, 2.0.1-beta3, 1.10.2a1.
_VERSION_SUFFIX = re.compile(r"(?<![a-z0-9])v?[0-9]+(?:\.[0-9]+)+[._-]?([a-z]+[0-9]+)(?![a-z0-9])")


def version_suffixes(text: str) -> frozenset[str]:
    """The suffixes ("rc28") of the release versions ``text`` names.

    People name a release by its suffix alone, which the intact version token
    never matches.  Callers add the suffix beside the full token, so rc28 and
    rc29 stay as distinct as the versions they come from.
    """
    return frozenset(_VERSION_SUFFIX.findall(unicodedata.normalize("NFKC", text).casefold()))


def lexical_terms(text: str) -> tuple[str, ...]:
    """Chinese bigrams and intact identifiers; normalization affects index only."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms = set(_IDENTIFIER.findall(normalized)) | set(_FILENAME.findall(normalized))
    terms.update(part for token in tuple(terms) for part in token.split("/"))
    terms.update(_VERSION_SUFFIX.findall(normalized))
    for run in _CJK.findall(normalized):
        if len(run) == 1:
            terms.add(run)
        terms.update(run[i:i+2] for i in range(len(run)-1))
    # Pathological uninterrupted identifiers are available by source expansion,
    # but are not unbounded index keys. Normal identifiers remain exact.
    return tuple(sorted(t for t in terms if 0 < len(t) <= 240))


def query_terms(query: str) -> tuple[str, ...]:
    if type(query) is not str or len(query) > 8192:
        raise ContractError("INPUT_INVALID", "query")
    terms = lexical_terms(query)
    if len(terms) > 128:
        raise ContractError("INPUT_INVALID", "query_terms")
    return terms
