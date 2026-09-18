"""How much of a source may be handed to the embedding model.

Measured on one instance: of 1,852 embedding attempts, **six failed with http_400
and every one of them was an oversized source** -- 16,505 / 17,531 / 36,650 /
52,410 / 52,451 / 65,536 characters.  The largest that ever succeeded was
16,770, and the 99th percentile of successes is 5,536, so the provider's
boundary sits in a narrow band just above 16 k and the rest of the corpus is
nowhere near it.

``http_400`` is not in ``AUTO_RECOVERABLE_ERRORS``, and rightly so -- resending
the same oversized body would fail the same way.  The consequence was that
those six sources could **never** be embedded, which means never reachable
through the vector channel, for the life of the instance.  The full text was in
SQLite the whole time; only the index was missing.

This is the same shape as the candidate evidence bound repaired earlier in the
release: a path that bounded a *count* (or nothing at all) where it needed to
bound *bytes*.  ``encode_embedding_text`` had no length check of any kind.

So the input is bounded and **truncated rather than refused**.  A vector built
from the first several thousand characters of a 52 kB tool transcript is worth
having; no vector at all is not, and the stored memory is untouched either way
-- the lexical channel still indexes the whole source, and SQLite still holds
it verbatim.  Truncation is recorded, never silent.

The bound is characters, not tokens, because the failures are only observable
in characters and a token estimate would be a second guess layered on the
first.  8,000 sits above the 99th percentile of what already works and at half
the lowest observed failure, so it truncates 21 of 31,416 stored sources.

Not responsible for: deciding *whether* to embed (``core/worker.py``), or for
chunking a long source into several vectors -- that is a feature, and the
corpus does not yet justify it at 0.07% of sources.
"""
from __future__ import annotations

#: Characters of a single object handed to the embedding model.
EMBEDDING_INPUT_CHARS = 8000

#: Appended when text was cut, so a reader of the embedded text can tell.  It
#: is inside the embedded body on purpose: the marker travels with the thing it
#: describes rather than living in a side table nobody joins.
TRUNCATION_MARKER = " …[truncated]"


def bounded_embedding_text(text: str, *, limit: int = EMBEDDING_INPUT_CHARS) -> tuple[str, bool]:
    """Return the text to embed and whether it had to be cut.

    Cutting on a plain character boundary is deliberate: any cleverer boundary
    (sentence, token, code point class) would make the bound depend on content,
    and the one property that has to hold is that the body is never larger than
    the provider accepts.
    """
    if type(text) is not str:
        raise TypeError("text must be str")
    if type(limit) is not int or type(limit) is bool or limit < 1:
        raise ValueError("limit")
    if len(text) <= limit:
        return text, False
    return text[: max(1, limit - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER, True


__all__ = ["EMBEDDING_INPUT_CHARS", "TRUNCATION_MARKER", "bounded_embedding_text"]
