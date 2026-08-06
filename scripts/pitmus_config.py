"""Deprecated: moved to `pitmus.version`.

Kept as a shim so any existing import keeps working. New code should use::

    from pitmus import DATASET_VERSION, dataset_dirname
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pitmus.version import DATASET_VERSION, dataset_dirname   # noqa: F401,E402

__all__ = ["DATASET_VERSION", "dataset_dirname"]
