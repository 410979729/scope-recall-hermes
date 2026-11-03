"""Text projections of already authorized injection items; never a visibility boundary."""

import json

from .resume_compaction import resume_evidence_refs

FORMATS = frozenset({"json", "compact", "outline"})


def _plain(value):
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_plain(item)}" for key, item in value.items() if item not in (None, [], {}))
    if isinstance(value, list):
        return "; ".join(_plain(item) for item in value)
    return str(value)


def _line(text):
    # Preserve literal separators and hostile delimiters as data on one line.
    return json.dumps(str(text), ensure_ascii=False)[1:-1].replace("<", "\\u003c").replace(">", "\\u003e")


def _summary(text):
    text = " ".join(text.split())
    return text[:160] + ("... [summary]" if len(text) > 160 else "")


def _content(item):
    text = item["content"]
    if item["kind"] in {"claim", "procedure"}:
        try:
            payload = json.loads(text)
        except ValueError:
            return text
        if isinstance(payload, dict):
            ordered = {
                key: payload[key]
                for key in ("subject", "predicate", "value_text", "conditions", "valid_from", "valid_to")
                if key in payload
            }
            ordered.update((key, value) for key, value in payload.items() if key not in ordered)
            return _plain(ordered)
    return text


def question(payload, style):
    """Project canonical items without changing their identities, times or source labels."""
    lines = ["Recalled memory (" + style + "; inspect references for full content):"]
    for item in payload["items"]:
        content = _content(item)
        if style == "outline":
            content = _summary(content)
        identity = f"{item['ref']}@{item['revision']}"
        refs = ", ".join(dict.fromkeys([identity, *item["evidence_refs"]]))
        labels = "; ".join(
            f"{key}: {_plain(item[key])}"
            for key in ("basis", "applicability", "entries", "source_contexts", "qualification_reason")
            if item.get(key)
        )
        lines.append(
            " | ".join(
                _line(part)
                for part in (
                    f"{item['kind']} ({item['temporal_status']}; {item['origin']})",
                    content,
                    item.get("occurred_at") or "time unknown",
                    refs,
                    labels,
                )
                if part
            )
        )
    if payload.get("gaps"):
        lines.append("Gaps: " + _line(", ".join(payload["gaps"])))
    return "\n".join(lines)


def background(lines, style):
    """Format whole briefing lines, retaining the external-report authority warning."""
    if style == "json":
        return list(lines)
    rendered = []
    for line in lines:
        label, separator, body = line.partition(" | ")
        if label.startswith("Pending handoff"):
            view = json.loads(body)
            refs = ", ".join(
                dict.fromkeys(
                    [
                        f"{view['ref']}@{view['revision']}",
                        f"{view['task_ref']}@{view['task_revision']}",
                        *resume_evidence_refs(view["resume"]),
                    ]
                )
            )
            content = _plain(view["resume"])
            if style == "outline":
                content = _summary(content)
            body = f"sender: {view['sender_entry']}; session label unverified: {view['session_label']}; {content}"
            rendered.append(_line(f"{label} | {body} | {view['created_at']} | {refs}"))
        elif separator:
            parts = body.rsplit(" | ", 2)
            content = _summary(parts[0]) if style == "outline" else parts[0]
            rendered.append(_line(" | ".join([label, content, *parts[1:]])))
        else:
            rendered.append(_line(_summary(line) if style == "outline" else line))
    return rendered


def notice_prefix(value, style):
    """Keep both delivered and current references outside any shortened value."""
    old = f"{value['ref']}@{value['revision']}"
    if style == "json":
        return old + ": "
    current = value.get("current")
    target = f"inspect {current['ref']}@{current['revision']} | " if style == "outline" and current else ""
    return "update | " + _line(old) + " | " + target


def notice(value, style):
    """Keep withdrawals content-free; summaries link to the readable current value."""
    if style == "json":
        return value["text"]
    _ref, _, message = value["text"].partition(": ")
    if style == "outline" and value["corrected"]:
        message = _summary(message)
    return notice_prefix(value, style) + _line(message)
