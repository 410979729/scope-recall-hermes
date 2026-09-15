"""Lazy Experience tool boundary.

Core imports this module, but Experience implementation modules are loaded only
after their feature gate admits an actual extension call.
"""

from __future__ import annotations

from typing import Any


def experience_preflight(*args: Any, **kwargs: Any) -> Any:
    from ...experience_preflight import experience_preflight as implementation

    return implementation(*args, **kwargs)


def promote_experiences(*args: Any, **kwargs: Any) -> Any:
    from ...experience_promotion import promote_experiences as implementation

    return implementation(*args, **kwargs)


def create_playbook(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import create_playbook as implementation

    return implementation(*args, **kwargs)


def experience_stats(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import experience_stats as implementation

    return implementation(*args, **kwargs)


def find_duplicate_playbooks(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import find_duplicate_playbooks as implementation

    return implementation(*args, **kwargs)


def inspect_playbook(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import inspect_playbook as implementation

    return implementation(*args, **kwargs)


def merge_playbooks(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import merge_playbooks as implementation

    return implementation(*args, **kwargs)


def record_playbook_feedback(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import record_playbook_feedback as implementation

    return implementation(*args, **kwargs)


def review_playbook(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import review_playbook as implementation

    return implementation(*args, **kwargs)


def search_playbooks(*args: Any, **kwargs: Any) -> Any:
    from ...experience_store import search_playbooks as implementation

    return implementation(*args, **kwargs)


__all__ = [
    "create_playbook",
    "experience_preflight",
    "experience_stats",
    "find_duplicate_playbooks",
    "inspect_playbook",
    "merge_playbooks",
    "promote_experiences",
    "record_playbook_feedback",
    "review_playbook",
    "search_playbooks",
]
