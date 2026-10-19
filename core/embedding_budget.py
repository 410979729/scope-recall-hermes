"""How much of a source may be handed to the embedding model.

Embedding input is truncated rather than refused so oversized sources can still
have a vector representation; the full text remains in SQLite and the lexical
index.  The default bound is 2,000 estimated tokens.

Each character counts as one estimated token to handle digit-heavy text without
a tokenizer dependency, at the cost of truncating English prose more
aggressively.  Emoji and rare characters can still cost more than one token,
so the estimate is not a guaranteed provider-token bound.  Truncated embedded
text includes a truncation marker.

This module bounds one embedding input; ``core/worker.py`` decides whether to
embed a source.  It does not split a long source into several vectors.
"""

from __future__ import annotations

#: Estimated tokens of a single object handed to the embedding model.
EMBEDDING_INPUT_TOKENS = 2000

#: Appended when text was cut, so a reader of the embedded text can tell.  It
#: is inside the embedded body on purpose: the marker travels with the thing it
#: describes rather than living in a side table nobody joins.
TRUNCATION_MARKER = " …[truncated]"


def estimated_tokens(text: str) -> int:
    """The token estimate the bound applies: one a character, whatever the script (see the module)."""
    return len(text)


def bounded_embedding_text(text: str, *, limit: int = EMBEDDING_INPUT_TOKENS) -> tuple[str, bool]:
    """Return the text to embed and whether it had to be cut.

    The cut is the longest prefix whose estimate, with the marker, fits ``limit``.
    Cutting on a character boundary is deliberate: a sentence or word boundary would
    make the kept length depend on content, and the one property that has to hold is
    that the body is never larger than the provider accepts.
    """
    if type(text) is not str:
        raise TypeError("text must be str")
    if type(limit) is not int or type(limit) is bool or limit < 1:
        raise ValueError("limit")
    if estimated_tokens(text) <= limit:
        return text, False
    kept = max(1, limit - estimated_tokens(TRUNCATION_MARKER))
    return text[:kept] + TRUNCATION_MARKER, True


__all__ = ["EMBEDDING_INPUT_TOKENS", "TRUNCATION_MARKER", "bounded_embedding_text", "estimated_tokens"]
