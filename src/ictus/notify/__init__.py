"""Telling somebody what a run is doing.

A second boundary, and deliberately not under ``interfaces/``. That one answers
"who executes this graph"; this one answers "who hears about it". Different axes
— a run on any engine can report to any audience — and giving the second its own
package is what stops a service's spelling drifting into a backend, or an
engine's into a message.

One folder per destination, imported by its own name::

    from ictus.notify.slack import slack_channel
    from ictus.notify.jira import jira_cloud

No *destination* is re-exported here, and that is the rule rather than an
omission. This file and ``ictus/sources/__init__.py`` are the two boundary
roots, and a flat re-export would put every service's spelling into them — the
one thing they exist to keep out. What is re-exported is the service-neutral
half: ``deliver``, ``send`` and ``summarise`` name no destination and never
will. Their implementation moved to ``deliver`` for the matching reason — a
boundary with a subprocess call in it is a file doing two jobs.

``ictus adapters`` lists what is installed, built in and third-party alike.
"""

from __future__ import annotations

from ictus.notify.deliver import HEADLINE, Delivered, deliver, send, summarise

__all__ = ["HEADLINE", "Delivered", "deliver", "send", "summarise"]
