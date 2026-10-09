"""The ``ictus`` command line.

    building    emit, lint, preflight, validate, init — before anything runs
    running     run — compile, check, then launch what was compiled
    tracing     trace — what a finished run did
    watching    watch — what a live run is doing, relayed to its audience
    catalogue   stdlib — what is already built, touching no pipeline

``app.py`` holds the Typer object and the helpers the command modules share. A command
module registers itself by being imported here.
"""

from __future__ import annotations

from ictus.cli.app import app

# Imported for the side effect of registering their commands on `app`.
from ictus.cli import building, catalogue, running, tracing, watching  # noqa: F401  # isort: skip

__all__ = ["app"]
