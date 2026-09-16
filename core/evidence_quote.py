r"""Resolve a model's evidence quote back to a literal slice of stored content.

The model never sees stored bytes.  ``consolidation_messages`` hands it
``json.dumps(body, ensure_ascii=False)``, so a source holding a double quote
or a newline reaches the model as ``\"`` and ``\n`` -- two characters, not
one.  A quote copied faithfully from what the model saw is therefore not
always a substring of what is stored, and the byte-strict ``in`` checks in
``core/claim_storage.py``, ``core/consolidate.py`` and the alias path in
``core/mutate.py`` reject it as ``evidence_span`` / ``fragment_evidence``.

On a live corpus almost every such rejection came from a backslash, a double
quote or a newline; none differed under NFC normalisation and none involved
tabs or non-breaking spaces.  So this ladder has no normalisation rung and no
whitespace-tolerant rung: a rung is added when a corpus shows it is needed,
not in anticipation.  Whitespace tolerance in particular would trade away the
verbatim guarantee the whole quoting contract rests on.

Resolution is monotone.  Rung 1 is exactly the plain substring rule, so
nothing that passes now can begin to fail.  Every later rung returns a slice
of the stored content itself, which is why the callers stay byte-strict: by
the time they run, the quote already *is* a literal substring.
"""
from __future__ import annotations

import json


#: What ``json.dumps(..., ensure_ascii=False)`` emits for each character it
#: rewrites.  Anything else below 0x20 becomes a ``\u00xx`` escape; everything
#: at or above it, non-ASCII included, is passed through unchanged.
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _encoded_piece(char: str) -> str:
    piece = _ESCAPES.get(char)
    if piece is not None:
        return piece
    if char < " ":
        return json.dumps(char)[1:-1]
    return char


def encoded_with_origin(content: str) -> tuple[str, list[int]]:
    """Return the JSON-encoded body and, per encoded character, its raw index.

    The encoded text is what the model was shown.  ``origin`` is what lets a
    hit in that text be mapped back to a real slice of ``content`` rather than
    be trusted on its own.
    """
    pieces: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(content):
        piece = _encoded_piece(char)
        pieces.append(piece)
        origin.extend([index] * len(piece))
    return "".join(pieces), origin


def _json_decoded(quote: str) -> str | None:
    """Read the quote as a JSON string body, or ``None`` if it is not one.

    A quote sliced out of the encoded text can end mid-escape; this then fails
    rather than guessing at the missing half.
    """
    try:
        value = json.loads('"' + quote + '"')
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def resolve_quote(quote: object, content: object) -> str | None:
    """Return the stored slice this quote names, or ``None`` if there is none.

    A returned value is always a literal substring of ``content``.
    """
    if type(quote) is not str or not quote or type(content) is not str or not content:
        return None
    if quote in content:
        return quote
    decoded = _json_decoded(quote)
    if decoded and decoded in content:
        return decoded
    encoded, origin = encoded_with_origin(content)
    position = encoded.find(quote)
    if position < 0:
        return None
    # A match that begins or ends inside an escape sequence maps back to the
    # whole character it belongs to, so the slice can be one character wider at
    # each end than what the model quoted.  It is still a literal slice of the
    # stored source, and a wider quote only ever makes the downstream
    # "value_text must appear inside the quote" check more permissive -- it
    # cannot make an unsupported value look supported by text that is not
    # there.
    start = origin[position]
    end = origin[position + len(quote) - 1] + 1
    return content[start:end]


def resolve_evidence_quotes(value: dict, contents: dict) -> int:
    """Rewrite every resolvable span quote in place; return how many moved.

    ``contents`` maps ``(source_ref, source_revision)`` to stored content and
    is built by the caller from sources it has *already* authorized.  Taking a
    map rather than a transaction is deliberate: a span naming a source outside
    that map is left exactly as it is, so this can never become a way to read a
    source the caller had not already cleared.  Rejecting such a span stays
    with the strict check downstream, which is the place that states it.
    """
    resolved = 0
    for proposal in value.get("claim_proposals") or ():
        if type(proposal) is not dict:
            continue
        for span in proposal.get("evidence_spans") or ():
            if type(span) is not dict:
                continue
            content = contents.get((span.get("source_ref"), span.get("source_revision")))
            if content is None:
                continue
            stored = resolve_quote(span.get("quote"), content)
            if stored is not None and stored != span.get("quote"):
                span["quote"] = stored
                resolved += 1
    return resolved


__all__ = ["encoded_with_origin", "resolve_quote", "resolve_evidence_quotes"]
