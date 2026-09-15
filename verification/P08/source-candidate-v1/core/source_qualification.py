"""Shared textual guards for source assertions, without persistence or model I/O."""
from __future__ import annotations

import re


AUTHORITY_QUESTION = re.compile(r'[?？]|是否|能否|可否|要不要|是不是|吗(?:[。.!，,;；\s]|$)|\b(?:whether|is it|(?:can|could|would|should)\s+(?:I|we|you))\b',re.I)
CLAUSE_BREAK = re.compile(r'[，,;；。!?！？\n]|\.(?:\s|$)')
QUALIFIER = re.compile(r'不要|不能|不必|不得|不准|不允许|并非|不是|没有|未曾|尚未|还没|尚无|不|没|未|无|仅|只|唯独|除了?|暂时|暂不|限于|\b(?:not|never|no|without|don[’\']t|cannot|can[’\']t|only|unless|except|until|temporarily)\b',re.I)


def preserves_qualifiers(content, text):
    """Reject a substring which drops its clause's polarity or restriction.

    The full negative clause remains valid. All matching occurrences must be
    unambiguous because the current resume contract has no character offsets.
    """
    if not text:return False
    start=0;found=False
    while (at:=content.find(text,start))>=0:
        found=True;end=at+len(text)
        before=list(CLAUSE_BREAK.finditer(content,0,at))
        left=before[-1].end() if before else 0
        after=CLAUSE_BREAK.search(content,end)
        right=after.start() if after else len(content)
        if any(m.start()<at or m.end()>end for m in QUALIFIER.finditer(content,left,right)):
            return False
        start=end
    return found


def asserted_marker(pattern, content):
    return not AUTHORITY_QUESTION.search(content) and any(
        preserves_qualifiers(content,match.group()) for match in pattern.finditer(content))
