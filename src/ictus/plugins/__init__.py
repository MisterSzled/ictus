"""Adapters this installation has, wherever they came from.

``notify`` and ``sources`` say what an adapter *is*; this says which ones exist
here. The two are separate on purpose: the first is a shape ictus defines, the
second is a fact about the machine, and a fact about the machine cannot be a
list written into the library.
"""

from __future__ import annotations

from ictus.plugins.registry import (
    NOTIFY_GROUP,
    SOURCE_GROUP,
    Adapter,
    AdapterProblem,
    installed,
    problems,
)

__all__ = [
    "NOTIFY_GROUP",
    "SOURCE_GROUP",
    "Adapter",
    "AdapterProblem",
    "installed",
    "problems",
]
