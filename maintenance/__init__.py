"""Bounded v1.1 install, doctor, and uninstall helpers for host wrappers."""

from .doctor import run_doctor
from .install import (
    PACKAGE_VERSION,
    InstallError,
    apply_install,
    apply_uninstall,
    plan_install,
    plan_uninstall,
)

__all__ = [
    "PACKAGE_VERSION",
    "InstallError",
    "apply_install",
    "apply_uninstall",
    "plan_install",
    "plan_uninstall",
    "run_doctor",
]
