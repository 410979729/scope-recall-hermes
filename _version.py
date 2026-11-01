"""Single source for the Scope Recall distribution version.

Every other place the version appears -- both plugin.yaml manifests
and the packaging allowlist -- is stamped from here by
``scripts/build.package_manifest.py``, and ``tests/packaging`` fails if any of
them drifts.  Change it here and nowhere else.
"""

import re

__version__ = "4.0.0.dev0"


def manifest_version(version: str) -> str:
    """Return the host SemVer spelling of a PEP 440 distribution version."""
    if ".dev" in version:
        return version.replace(".dev", "-dev.", 1)
    # Preserve trailing segments while converting the release-candidate prefix.
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", version)
