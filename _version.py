"""Single source for the Scope Recall distribution version.

Every other place the version appears -- both plugin.yaml manifests, the Codex
plugin.json, and the packaging allowlist -- is stamped from here by
``scripts/build.package_manifest.py``, and ``tests/packaging`` fails if any of
them drifts.  Change it here and nowhere else.
"""

__version__ = "3.3.0rc4"
