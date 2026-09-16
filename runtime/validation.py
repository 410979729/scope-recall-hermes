"""Strict field coercion shared by the runtime configurations and entry points.

Every check is exact by design: ``True`` is not ``1``, ``"45"`` is not ``45.0``
and a relative path is not a path.  A value that happens to parse is a caller
bug, and coercing it would hide one.  Each helper raises ``ValueError(name)``
so the field name is the whole message; host diagnostics and the tests match
on that name.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import math
import os
from pathlib import Path
from typing import Any, Collection, Mapping, TypeVar

T = TypeVar("T")


def strict_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(name)
    return value


def strict_int(name: str, value: object, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """An ``int`` inside the closed bounds.  ``bool`` is rejected, not counted."""
    if type(value) is not int:
        raise ValueError(name)
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(name)
    return value


def nonneg_int(name: str, value: object) -> int:
    return strict_int(name, value, minimum=0)


def positive_int(name: str, value: object) -> int:
    return strict_int(name, value, minimum=1)


def strict_float(name: str, value: object, *, minimum: float, maximum: float) -> float:
    """A finite ``int`` or ``float`` inside the closed bounds, returned as ``float``."""
    if type(value) not in (int, float):
        raise ValueError(name)
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(name)
    return parsed


def nonneg_decimal(name: str, value: object) -> Decimal:
    """A finite, non-negative amount given as ``Decimal``, ``int`` or an exact string."""
    if type(value) is int:
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    elif type(value) is str and value and value.strip() == value:
        try:
            amount = Decimal(value)
        except ArithmeticError as exc:
            raise ValueError(name) from exc
    else:
        raise ValueError(name)
    if not amount.is_finite() or amount < 0:
        raise ValueError(name)
    return amount


def text(name: str, value: object) -> str:
    """A non-empty ``str``."""
    if type(value) is not str or not value:
        raise ValueError(name)
    return value


def identifier(name: str, value: object, *, required: bool = True) -> str | None:
    """A non-blank ``str`` of at most 240 characters; ``None`` only when optional."""
    if value is None and not required:
        return None
    if type(value) is not str or not value.strip() or len(value) > 240:
        raise ValueError(name)
    return value


def absolute_path(name: str, value: object) -> Path:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if type(value) is not str or not value:
        raise ValueError(name)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name}_must_be_absolute")
    return path


def mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(name)
    return value


def member(name: str, value: T, allowed: Collection[T]) -> T:
    if value not in allowed:
        raise ValueError(name)
    return value


def only_keys(name: str, raw: Mapping[str, Any], allowed: Collection[str]) -> Mapping[str, Any]:
    """Reject a mapping that names a field the reader would silently ignore."""
    if set(raw) - set(allowed):
        raise ValueError(name)
    return raw


def utc_now() -> str:
    """The ``Z``-suffixed UTC stamp every worker metadata file and receipt carries."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "absolute_path",
    "identifier",
    "mapping",
    "member",
    "nonneg_decimal",
    "nonneg_int",
    "only_keys",
    "positive_int",
    "strict_bool",
    "strict_float",
    "strict_int",
    "text",
    "utc_now",
]
