"""The chat bridge — a service's users driving runs they did not launch.

A process rather than a compiler, and kept separate for that reason. The edge
is one-way and enforced: ``ictus.bridge`` may import anything in ``ictus``;
nothing in ``ictus`` may import ``ictus.bridge``.

What it alone may know: that a press arrives as a Slack envelope, that a form
has a short-lived ``trigger_id``, that an answered question loses its buttons.
Everything downstream is service-neutral and lives in ``ictus.runs``.
"""

from __future__ import annotations

__all__: list[str] = []
