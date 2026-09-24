"""How much of a source may be handed to the embedding model.

Measured on one instance: of 1,852 embedding attempts, **six failed with http_400
and every one of them was an oversized source** -- 16,505 / 17,531 / 36,650 /
52,410 / 52,451 / 65,536 characters.  ``http_400`` is not in
``AUTO_RECOVERABLE_ERRORS``, and rightly so -- resending the same oversized body
would fail the same way -- so those sources could **never** be embedded: their
text was in SQLite and in the lexical index the whole time, only the vector was
missing.  So the input is bounded and **truncated rather than refused**.  A
vector built from the first several thousand characters of a 52 kB tool
transcript is worth having; no vector at all is not.  Truncation is recorded,
never silent.

The bound is an estimate of **tokens**, not a count of characters.  Providers
limit tokens, and characters per token depend on the script: against Zhipu
``embedding-3`` (3,072 tokens per input) 12,271 ASCII letters, 3,068 Chinese
characters and 4,382 characters of 60% Chinese text all stopped at exactly
3,072 tokens (#125).  The 8,000-character bound this replaced let through more
than that for any text denser than about a quarter Chinese, and 39 ordinary
sources of one instance failed with ``http_400`` for good -- including 5,937
characters of 15% Chinese, while 12,271 ASCII characters passed.  No character
bound separates those two; an estimate that knows the script does.

The estimate errs high on purpose: one token for every character outside ASCII,
one for every three ASCII characters.  Symbol-dense ASCII measured 2.3
characters a token there, CJK about one, so 2,000 estimated tokens stays under
3,072 real ones for every input reported, and keeps 6,000 ASCII characters --
still above the 5,536 that 99% of one instance's successful bodies were under.

Not responsible for: deciding *whether* to embed (``core/worker.py``), or for
chunking a long source into several vectors -- that is a feature, and the
corpus does not yet justify it.
"""
from __future__ import annotations

#: Estimated tokens of a single object handed to the embedding model.
EMBEDDING_INPUT_TOKENS = 2000

#: ASCII characters counted as one token.  Everything outside ASCII counts one each.
ASCII_CHARS_PER_TOKEN = 3

#: Appended when text was cut, so a reader of the embedded text can tell.  It
#: is inside the embedded body on purpose: the marker travels with the thing it
#: describes rather than living in a side table nobody joins.
TRUNCATION_MARKER = " …[truncated]"


def _cost(character: str) -> int:
    """A character's share of a token, in thirds: an ASCII character is one, any other three."""
    return 1 if ord(character) < 128 else ASCII_CHARS_PER_TOKEN


def estimated_tokens(text: str) -> int:
    """The token estimate the bound applies, rounded up."""
    return -(-sum(_cost(character) for character in text) // ASCII_CHARS_PER_TOKEN)


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
    allowance = max(1, limit - estimated_tokens(TRUNCATION_MARKER)) * ASCII_CHARS_PER_TOKEN
    spent = kept = 0
    for character in text:
        spent += _cost(character)
        if spent > allowance:
            break
        kept += 1
    return text[: max(1, kept)] + TRUNCATION_MARKER, True


__all__ = ["ASCII_CHARS_PER_TOKEN", "EMBEDDING_INPUT_TOKENS", "TRUNCATION_MARKER", "bounded_embedding_text",
           "estimated_tokens"]
