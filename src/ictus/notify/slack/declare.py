"""What a pipeline declares when it reports to Slack.

Pure data: both constructors return an ``Integration`` carrying a program
nothing in ictus reads. The program itself is in ``program.py`` and the live
Web API client is in ``api.py``; this module is the only one of the three a
pipeline imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.requirements import Integration
from ictus.notify.slack.api import API, API_ENV, LABEL_LIMIT, SECTION_LIMIT, TIMEOUT_SECONDS
from ictus.notify.slack.program import DEADLINE_SECONDS, PROGRAM

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.requirements import EnvVar
    from ictus.graph.signals import RunSignal

__all__ = ["slack_channel", "slack_webhook"]


def slack_channel(
    *,
    token: EnvVar,
    channel: EnvVar,
    name: str = "slack",
    purpose: str = "Report what this run is doing, and ask for decisions",
    reports: Sequence[RunSignal] = (),
    setup_hint: str = (
        "Create a Slack app with chat:write, install it, invite it to the channel, "
        "then export the bot token and the channel id"
    ),
) -> Integration:
    """A channel reported into through ``chat.postMessage``.

    Threads, so several runs at once stay legible. Needs a bot token.

    Listens too, either way round, and neither credential reaches a run:
    `ictus-bridge listen` holds a socket open as the app, which needs an
    app-level token and the app in the channel; `ictus-bridge overhear` reads
    the channel as a person, which needs only a user token and that person's
    membership — and cannot answer a gate, since no user token hears a press.
    """
    return Integration(
        name=name,
        purpose=purpose,
        env=(token, channel),
        reports=tuple(reports),
        command="python3",
        program=_program(secret=token.name, channel=channel.name),
        threads=True,
        listens=True,
        comments=False,
        setup_hint=setup_hint,
    )


def slack_webhook(
    *,
    url: EnvVar,
    name: str = "slack",
    purpose: str = "Report what this run is doing",
    reports: Sequence[RunSignal] = (),
    setup_hint: str = "Create an incoming webhook on a Slack app and export its URL",
) -> Integration:
    """A channel posted into through an incoming webhook.

    Simpler to set up and strictly less capable: no threads, no buttons, and
    one-way, so it cannot start a run.
    """
    return Integration(
        name=name,
        purpose=purpose,
        env=(url,),
        reports=tuple(reports),
        command="python3",
        program=_program(secret=url.name, channel=""),
        threads=False,
        setup_hint=setup_hint,
    )


def _program(*, secret: str, channel: str) -> str:
    """The sending program, with this integration's variable names baked in."""
    return PROGRAM.substitute(
        secret_name=repr(secret),
        channel_name=repr(channel),
        api=repr(API),
        api_env=repr(API_ENV),
        request_timeout=str(TIMEOUT_SECONDS),
        deadline=str(DEADLINE_SECONDS),
        section_limit=str(SECTION_LIMIT),
        label_limit=str(LABEL_LIMIT),
    )
