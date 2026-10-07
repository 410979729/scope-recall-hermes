"""How much of a source may be handed to the embedding model.

Measured on one instance: of 1,852 embedding attempts, **six failed with http_400
and every one of them was an oversized source** -- 16,505 / 17,531 / 36,650 /
52,410 / 52,451 / 65,536 characters.  ``http_400`` is not in
``AUTO_RECOVERABLE_ERRORS``, and rightly so -- resending the same oversized body
would fail the same way -- so those sources could **never** be embedded: their
text was in SQLite and in the lexical index the whole time, only the vector was
missing.  So the input is bounded and **truncated rather than refused**.  A
vector built from the first thousands of characters of a 52 kB tool transcript
is worth having; no vector at all is not.  Truncation is recorded, never silent.

The bound counts **tokens**, which is what providers limit, and it counts every
character as one, whatever the script.  That is the most any measured text
costs: against Zhipu ``embedding-3`` (3,072 tokens per input) 3,068 Chinese
characters stopped at exactly 3,072 tokens (#125), and ASCII ran from 4.0
characters a token for letters down to 2.5 for code, 2.0 for symbols, 1.4 for
base64 and **1.0 for digits** (#151).  The estimate this replaced counted three
ASCII characters as one token, which held for prose and symbols and let 6,000
characters of a digit-dense body through as 2,000 tokens -- logs, IDs, hashes
and JSON -- and ``embedding-3`` refused them with ``http_400`` for good.  One
token a character keeps 2,000 of anything under both limits in use: 3,072 and
Gemini's 2,048.  It costs ASCII prose: a long English source is embedded from
its first 2,000 characters where it had 6,000; all of it stays in the lexical
index.  A tokeniser would know better, and is a dependency this does not take.
An emoji or a rare character can cost a provider more than one token.

Not responsible for: deciding *whether* to embed (``core/worker.py``), or for
chunking a long source into several vectors -- that is a feature, and the
corpus does not yet justify it.
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
