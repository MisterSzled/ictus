"""``python -m ictus.cli``.

A module of its own so running and importing take the same path through
command registration.
"""

from __future__ import annotations

from ictus.cli import app

app()
