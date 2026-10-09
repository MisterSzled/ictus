"""Turning a graph into the YAML Conductor loads.

Every module here is a pure function of a pipeline: nodes in, document shape
out, no environment read and no process started. That is what separates it from
``control`` next door, which only ever acts on a run that already exists, and
from ``preflight``, which answers questions about this machine.

The split is not cosmetic. Emission is what `ictus emit` must be able to do on
a laptop with no credentials, no network and no engine running; keeping the
modules that open sockets out of it is what keeps that true.
"""

from __future__ import annotations

__all__: list[str] = []
