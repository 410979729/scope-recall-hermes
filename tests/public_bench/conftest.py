"""Expose standalone benchmark helpers during explicit-file test collection."""

import sys
from pathlib import Path

# Helpers remain outside the installed product and use sibling imports in their CLI entry points.
sys.path.insert(0, str(Path(__file__).resolve().parent))
