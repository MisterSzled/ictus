"""Slack, as a place a run reports to. Nothing above this package names it.

    declare.py  what a pipeline declares: the two constructors
    program.py  the sender the engine runs as a subprocess, as data
    api.py      the live Web API client the bridge calls back through

Pure data: ``slack_channel`` and ``slack_webhook`` return an ``Integration``
carrying a program nothing in ictus reads.

Receiving from Slack is a process rather than a declaration, and lives in
``ictus.bridge``.
"""

from __future__ import annotations

from ictus.notify.slack.api import API, API_ENV, api_call, endpoint, reply
from ictus.notify.slack.declare import slack_channel, slack_webhook

__all__ = [
    "API",
    "API_ENV",
    "api_call",
    "endpoint",
    "reply",
    "slack_channel",
    "slack_webhook",
]
