"""Artifact extraction helpers for turning issue, PR, release, commit, and URL mentions into stable recall anchors.

The goal is to enrich memory text with machine-comparable artifact metadata without treating URLs themselves as durable truth."""

from __future__ import annotations

import re
from typing import Any

from .gating import compact_text
from .graph import normalize_entity

ANCHOR_MARKER = "Artifact anchors:"
_URL_RE = re.compile(r"https?://[^\s<>'\"\])}，。；、]+", re.IGNORECASE)
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:/(?P<section>issues|pull|pulls|commit|releases/tag)/(?P<item>[^\s<>'\"\])}，。；、]+))?",
    re.IGNORECASE,
)
_GITHUB_SHORTHAND_RE = re.compile(r"\b(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)\b")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?。！？、，；：)]}）"


def _strip_url(value: str) -> str:
    return str(value or "").rstrip(_TRAILING_PUNCT)


def _artifact_key(artifact: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(artifact.get("kind") or ""),
        str(artifact.get("repo") or ""),
        str(artifact.get("number") or artifact.get("tag") or artifact.get("commit") or ""),
        str(artifact.get("url") or ""),
    )


def _github_artifact_from_match(match: re.Match[str], *, url: str = "") -> dict[str, Any]:
    owner = match.group("owner")
    repo_name = match.group("repo")
    repo = f"{owner}/{repo_name}"
    section = (match.groupdict().get("section") or "").lower()
    item = _strip_url(match.groupdict().get("item") or "")
    if section == "issues" and item.isdigit():
        return {"kind": "github_issue", "repo": repo, "number": int(item), "url": url or f"https://github.com/{repo}/issues/{item}"}
    if section in {"pull", "pulls"} and item.isdigit():
        return {"kind": "github_pull", "repo": repo, "number": int(item), "url": url or f"https://github.com/{repo}/pull/{item}"}
    if section == "commit" and _SHA_RE.fullmatch(item):
        return {"kind": "github_commit", "repo": repo, "commit": item, "url": url or f"https://github.com/{repo}/commit/{item}"}
    if section == "releases/tag" and item:
        return {"kind": "github_release", "repo": repo, "tag": item, "url": url or f"https://github.com/{repo}/releases/tag/{item}"}
    return {"kind": "github_repo", "repo": repo, "url": url or f"https://github.com/{repo}"}


def extract_artifacts(text: str) -> list[dict[str, Any]]:
    """Extract stable external artifact anchors from memory text.

    These anchors are deliberately deterministic and source-only: they record
    identifiers/URLs that appeared in the text, not live status claims that need
    a fresh GitHub/API check.
    """

    source = str(text or "")
    artifacts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    github_url_spans: list[tuple[int, int]] = []

    for match in _GITHUB_URL_RE.finditer(source):
        url = _strip_url(match.group(0))
        artifact = _github_artifact_from_match(match, url=url)
        key = _artifact_key(artifact)
        if key not in seen:
            seen.add(key)
            artifacts.append(artifact)
        github_url_spans.append(match.span())

    for match in _GITHUB_SHORTHAND_RE.finditer(source):
        artifact = {
            "kind": "github_issue_or_pull",
            "repo": f"{match.group('owner')}/{match.group('repo')}",
            "number": int(match.group("number")),
            "url": "",
        }
        key = _artifact_key(artifact)
        if key not in seen:
            seen.add(key)
            artifacts.append(artifact)

    for match in _URL_RE.finditer(source):
        if any(start <= match.start() < end for start, end in github_url_spans):
            continue
        url = _strip_url(match.group(0))
        artifact = {"kind": "url", "url": url}
        key = _artifact_key(artifact)
        if key not in seen:
            seen.add(key)
            artifacts.append(artifact)

    return artifacts[:24]


def artifact_label(artifact: dict[str, Any]) -> str:
    kind = str(artifact.get("kind") or "")
    repo = str(artifact.get("repo") or "")
    url = str(artifact.get("url") or "")
    if kind in {"github_issue", "github_pull", "github_issue_or_pull"}:
        number = artifact.get("number")
        label_kind = "PR" if kind == "github_pull" else "issue/PR" if kind == "github_issue_or_pull" else "issue"
        label = f"GitHub {label_kind} {repo}#{number}"
    elif kind == "github_commit":
        commit = str(artifact.get("commit") or "")
        label = f"GitHub commit {repo}@{commit[:12]}"
    elif kind == "github_release":
        label = f"GitHub release {repo} {artifact.get('tag')}"
    elif kind == "github_repo":
        label = f"GitHub repo {repo}"
    else:
        label = f"URL {url}"
    if url and url not in label:
        label = f"{label} ({url})"
    return compact_text(label, 240)


def artifact_anchor_block(artifacts: list[dict[str, Any]]) -> str:
    if not artifacts:
        return ""
    labels = [artifact_label(artifact) for artifact in artifacts[:10]]
    return f"{ANCHOR_MARKER} " + "; ".join(labels)


def enrich_content_with_artifact_anchors(content: str) -> str:
    cleaned = str(content or "").strip()
    if not cleaned or ANCHOR_MARKER in cleaned:
        return cleaned
    block = artifact_anchor_block(extract_artifacts(cleaned))
    if not block:
        return cleaned
    return f"{cleaned}\n\n{block}"


def merge_artifact_metadata(metadata: dict[str, Any], content: str) -> dict[str, Any]:
    artifacts = extract_artifacts(content)
    if not artifacts:
        return metadata

    raw_existing = metadata.get("artifacts")
    existing: list[Any] = raw_existing if isinstance(raw_existing, list) else []
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for artifact in [*existing, *artifacts]:
        if not isinstance(artifact, dict):
            continue
        key = _artifact_key(artifact)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(artifact))
    metadata["artifacts"] = merged[:24]

    raw_entity_values = metadata.get("entities")
    entity_values: list[Any] = raw_entity_values if isinstance(raw_entity_values, list) else []
    artifact_entities: list[str] = []
    for artifact in artifacts:
        repo = str(artifact.get("repo") or "")
        if repo:
            artifact_entities.append(repo)
        if artifact.get("number") and repo:
            artifact_entities.append(f"{repo}#{artifact['number']}")
        if artifact.get("tag") and repo:
            artifact_entities.append(f"{repo}@{artifact['tag']}")
        if artifact.get("commit") and repo:
            artifact_entities.append(f"{repo}@{str(artifact['commit'])[:12]}")
    metadata["entities"] = sorted({entity for entity in (normalize_entity(item) for item in [*entity_values, *artifact_entities]) if entity})

    raw_tag_values = metadata.get("tags")
    tag_values: list[Any] = raw_tag_values if isinstance(raw_tag_values, list) else []
    artifact_tags = ["artifact"]
    if any(str(artifact.get("kind") or "").startswith("github_") for artifact in artifacts):
        artifact_tags.append("github")
    artifact_tags.extend(f"artifact:{artifact.get('kind')}" for artifact in artifacts if artifact.get("kind"))
    metadata["tags"] = sorted({str(item).strip().lower() for item in [*tag_values, *artifact_tags] if str(item).strip()})
    return metadata
