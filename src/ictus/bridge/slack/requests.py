"""The one recogniser both ways in share.

A prefix that starts a run over the socket starts the same one when a channel
is read directly. Kept out of ``listen.py`` because neither of these is about
listening: ``watch`` calls ``request_in`` for every message it polls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.runs.launch import Asked

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from ictus.runs.triggers import Trigger

__all__ = ["asked", "request_in"]


def request_in(event: Mapping[str, object], channel: str, trigger: Trigger) -> Asked | None:
    """One message, if it is a request for a run.

    The rule both ways in share, so a prefix that starts a run over the socket
    starts the same one when a channel is read directly.

    ``channel`` is passed rather than read out: an Events API message names the
    channel it arrived from, and a message read back from ``conversations.history``
    does not — that method answers about a channel the caller already named.
    """
    if event.get("type") != "message":
        return None
    # Anything the app said, and anything that is not somebody typing: edits,
    # deletions, joins, and the thread-broadcast copies of those.
    if event.get("bot_id") or event.get("subtype"):
        return None
    text = event.get("text")
    if not isinstance(text, str):
        return None
    found = trigger.pattern.match(text)
    if found is None:
        return None
    # The message's own ts, never its thread_ts: a request made inside a
    # thread is answered in that thread.
    return Asked(
        question=found.group("question").strip(),
        thread=str(event.get("ts", "")),
        channel=channel,
        who=str(event.get("user", "")),
        trigger=trigger,
    )


def asked(envelope: dict[str, object], trigger: Trigger) -> Iterator[Asked]:
    """The request in one envelope, if it holds one."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return
    event = payload.get("event")
    if not isinstance(event, dict):
        return
    request = request_in(event, str(event.get("channel", "")), trigger)
    if request is not None:
        yield request
