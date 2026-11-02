"""Bounded HTTP command payload classification, separate from authentication.

The command matcher understands standard clients and their payload parameters;
policy literals extend client names and select sensitive variable/file names.
Only one bounded command line is parsed, without executing or expanding it.
"""

import re
import shlex
from functools import lru_cache

CLIENTS = ("curl", "wget", "http", "https", "Invoke-WebRequest", "Invoke-RestMethod", "iwr", "irm")
BODY = frozenset(
    {
        "--data",
        "--data-raw",
        "--data-ascii",
        "--url-query",
        "--body-data",
        "--raw",
        "--data-binary",
        "--data-urlencode",
        "--json",
        "--post-data",
        "-d",
        "-F",
        "--form",
        "--form-string",
        "-Body",
        "-Form",
    }
)
FILES = frozenset({"--post-file", "--body-file", "--upload-file", "-T", "-InFile"})
_VARIABLE = re.compile(r"\$|%[A-Za-z_][A-Za-z_0-9]*%")
_URL = re.compile(r"https?://[^\s\"'`<>]+", re.I)


@lru_cache(maxsize=256)
def _clients(aliases):
    names = sorted(set((*CLIENTS, *aliases)), key=len, reverse=True)
    pattern = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(name) for name in names) + r")(?:\.exe)?(?=\s)", re.I)
    initials = re.compile("[" + "".join(re.escape(name[0]) for name in names) + "]", re.I)
    return pattern, initials


def _sensitive(value, needles):
    return any(token in value.casefold() for token in needles)


def _payload(words, command, needles):
    """Ignore headers/auth options; recognize body values, files and URL query variables."""
    if any(
        "?" in url[0] and _VARIABLE.search(url[0].split("?", 1)[1]) and _sensitive(url[0].split("?", 1)[1], needles)
        for url in _URL.finditer(command)
    ):
        return True
    client = words[0].casefold().removesuffix(".exe")
    powershell = client in {"invoke-webrequest", "invoke-restmethod", "iwr", "irm"}
    httpie = client in {"http", "https"}
    skip = False
    for index, word in enumerate(words[1:], 1):
        if skip:
            skip = False
            continue
        flag, separator, value = word.partition("=")
        if powershell:
            flag = next((name for name in BODY | FILES if name.casefold() == flag.casefold()), flag)
        if flag in BODY | FILES:
            if not separator:
                value = words[index + 1] if index + 1 < len(words) else ""
                skip = True
            if powershell and flag in {"-Body", "-Form"}:
                # PowerShell expressions can contain spaces, hashtables and file readers.
                # Stop at the next option so authentication headers remain separate.
                tail = " ".join(words[index + 1 :]) if not separator else value
                value = re.split(r"\s+-[A-Za-z]", tail, maxsplit=1)[0]
            if flag == "--form-string" and not _VARIABLE.search(value):
                continue
            if _sensitive(value, needles) and (
                flag in FILES
                or _VARIABLE.search(value)
                or "@" in value
                or powershell
                and bool(re.search(r"\b(?:Get-Content|Get-Item)\b", value, re.I))
            ):
                return True
        elif client == "curl" and word.startswith(("-d", "-F", "-T")) and len(word) > 2:
            value = word[2:]
            if _sensitive(value, needles) and (word.startswith("-T") or _VARIABLE.search(value) or "@" in value):
                return True
        elif httpie and not word.startswith(("-", "http://", "https://")):
            # Header fields have ':' before any payload operator; values in those
            # fields authorize a request and are not uploaded document content.
            operator = re.search(r"==|:=|=|@|:", word)
            if (word.startswith("@") or operator and operator[0] != ":") and _sensitive(word, needles):
                if "@" in word or _VARIABLE.search(word):
                    return True
    return False


def command_candidate(folded, aliases):
    """Reject folded windows without any possible client initial before parsing raw text."""
    return _clients(tuple(aliases))[1].search(folded) is not None


def command_spans(text, slots, span):
    """Yield bounded command ranges with sensitive payloads, retaining quote ambiguity."""
    pattern, initials = _clients(tuple(slots[0]))
    # Both patterns use the same case rules; a full client match always contains
    # an initial. Rejecting here avoids a lookbehind at every prose character.
    if initials.search(text) is None:
        return
    for match in pattern.finditer(text):
        command = text[match.start() : match.start() + span].replace("\\\n", " ").split("\n", 1)[0]
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        if words and _payload(words, command, slots[1]):
            yield match.start(), match.start() + len(command)
