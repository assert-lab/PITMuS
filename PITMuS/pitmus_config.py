"""Deprecated: moved to `shared.version`.

Kept as a shim so any existing import keeps working. New code should use::

    from shared import dataset_dirname
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.version import dataset_dirname   # noqa: F401,E402

__all__ = ["dataset_dirname"]
