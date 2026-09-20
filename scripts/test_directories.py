"""Owned TEST temporary directories with bounded Windows long-path cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import weakref


def _cleanup(name: str,parent: str,prefix: str) -> None:
    target=Path(name)
    boundary=Path(parent).resolve()
    # Check the final absolute root before every recursive removal. A moved or
    # substituted root must never redirect cleanup outside its original parent.
    if not target.exists():
        return
    if (not target.is_absolute() or target.is_symlink() or getattr(target,'is_junction',lambda:False)()
        or target.resolve().parent!=boundary or not target.name.startswith(prefix)):
        raise RuntimeError('TEST cleanup target identity changed')
    native=str(target)
    if os.name=='nt' and not native.startswith('\\\\?\\'):
        native='\\\\?\\UNC\\'+native[2:] if native.startswith('\\\\') else '\\\\?\\'+native
    shutil.rmtree(native)


class TestDirectory:
    """Same small surface used from TemporaryDirectory, with explicit ownership."""
    def __init__(self,*,prefix: str,dir):
        parent=Path(dir).resolve()
        if not prefix or not parent.is_absolute():
            raise ValueError('TEST directory ownership required')
        self.name=tempfile.mkdtemp(prefix=prefix,dir=parent)
        self._finalizer=weakref.finalize(self,_cleanup,self.name,str(parent),prefix)

    def cleanup(self):
        if self._finalizer.alive:
            self._finalizer()

    def __enter__(self):return self.name

    def __exit__(self,*exc):self.cleanup()
