"""Reading a channel to find out what was said, rather than being told.

``listen`` is Slack pushing events to an app down a socket it holds open. This
asks instead, on a timer. Nothing has to be made reachable, no app-level token
is involved and no event subscription has to exist — that, and not which kind
of credential is held, is the difference between the two.

Either kind is held here. ``conversations.history`` answers a bot token for a
channel the bot was invited to, and a user token for one its owner is in; this
passes whichever it was given. A channel you cannot get a bot into can be read
with your own credential, and a channel the bot is already in needs none.

Four costs, none of them hidden:

* **No button presses.** An interaction is pushed to the app that posted the
  message and is never in a channel's history, so no amount of reading finds
  one. This yields ``Asked`` and nothing else, and no caller can wire it to a
  gate by mistake. Gates stay answerable from the dashboard, or from ``listen``.
* **No replay.** Each cursor starts at the newest message, never at the
  beginning of the channel: a listener restarted after a fortnight would
  otherwise launch a fortnight of runs at once, in parallel, each costing
  money. ``presses`` loses what arrives while it is down for the same reason,
  and this matches it rather than inventing a second rule.
* **Top-level messages only.** ``conversations.history`` answers with parents,
  not replies, and a reply to an old thread never brings its parent back into
  the window. A request typed inside a thread starts a run over the socket and
  does not start one here.
* **Latency is a poll, and Slack sets the floor.** ``conversations.history`` is
  rate-limited hard for apps outside the Marketplace. A refusal is obeyed to
  the second Slack names rather than guessed at, and said once.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ictus.bridge.slack.errors import SlackError, SlackUnreachableError, refused
from ictus.bridge.slack.requests import request_in
from ictus.notify.slack.api import TIMEOUT_SECONDS, endpoint

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from ictus.runs.launch import Asked
    from ictus.runs.triggers import Trigger

logger = logging.getLogger(__name__)

__all__ = [
    "MOST_PAGES",
    "PAGE",
    "POLL_SECONDS",
    "Heard",
    "latest",
    "overheard",
    "since",
]

#: Between polls, when Slack has not asked for longer.
POLL_SECONDS = 30.0

#: Asked for per page. Slack gives fewer to an app outside the Marketplace,
#: which is why ``has_more`` is followed rather than trusted to be false.
PAGE = 100

#: Pages one poll will walk before giving up and saying what it skipped.
#: Unbounded, a channel busier than the rate limit would never catch up.
MOST_PAGES = 4

#: What Slack says when the token may not read this conversation. Separate from
#: a credential refusal: the token is fine, the channel is the problem, and no
#: amount of waiting fixes either.
_UNREADABLE = frozenset({"channel_not_found", "not_in_channel", "is_archived"})

#: When Slack rate-limits without saying for how long.
_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class Heard:
    """One poll of one channel."""

    messages: tuple[Mapping[str, object], ...] = ()
    """Oldest first, whichever end Slack chose to page from."""

    cursor: str = ""
    """Where the next poll starts. The one given back, when nothing was read."""

    why: str = ""
    """Empty when the poll worked. Otherwise what went wrong, to be logged."""

    after: float = 0.0
    """Seconds Slack asked to be left alone for, when it said so."""


# --- reading ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Reply:
    """What one Web API read came back with."""

    body: dict[str, object] = field(default_factory=dict)
    why: str = ""
    after: float = 0.0


def _read(method: str, token: str, params: Mapping[str, str], *, timeout: float) -> _Reply:
    """GET one Web API method. Never raises, and never quotes an exception.

    A GET, and not the JSON POST ``api_call`` makes: Slack's read methods take
    query parameters, and answer some JSON bodies with ``invalid_arguments``.
    """
    url = f"{endpoint(method)}?{urllib.parse.urlencode(dict(params))}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            asked_for = exc.headers.get("Retry-After") if exc.headers else None
            return _Reply(why="Slack is rate limiting this token", after=_seconds(asked_for))
        return _Reply(why=f"Slack answered HTTP {exc.code} to {method}")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return _Reply(why=f"could not reach Slack ({type(exc).__name__})")
    if not isinstance(answer, dict):
        return _Reply(why=f"Slack sent an unreadable answer to {method}")
    if not answer.get("ok"):
        # Slack also rate-limits with a 200 and an error, depending on the method.
        if answer.get("error") == "ratelimited":
            return _Reply(answer, "Slack is rate limiting this token", _BACKOFF_SECONDS)
        return _Reply(answer, f"Slack refused {method}: {answer.get('error')}")
    return _Reply(answer)


def _seconds(asked_for: str | None) -> float:
    """How long Slack asked to be left alone. A minute, when it did not say."""
    try:
        return max(float(str(asked_for)), 1.0)
    except (TypeError, ValueError):
        return _BACKOFF_SECONDS


def _hopeless(reply: _Reply, channel: str) -> None:
    """Raise when neither waiting nor trying again could change the answer."""
    error = str(reply.body.get("error", ""))
    if refused(error):
        raise SlackError(
            f"Slack refused the credential: {error} - reading a channel needs "
            "channels:history, or groups:history for a private one. It goes under Bot "
            "Token Scopes for a bot token and User Token Scopes for a user token, and "
            "the two are separate lists; a scope added after the app was installed is "
            "not granted until you reinstall"
        )
    if error in _UNREADABLE:
        raise SlackError(
            f"Slack will not read {channel}: {error} - check the channel id, which looks "
            "like C0ABC123 and is at the bottom of View channel details, and that the "
            "account this token belongs to is in that channel"
        )


def latest(token: str, channel: str, *, timeout: float = TIMEOUT_SECONDS) -> str:
    """The newest message in ``channel``: where listening starts.

    Called once per channel, before anything is yielded. What was said before
    this was running starts nothing.
    """
    reply = _read(
        "conversations.history", token, {"channel": channel, "limit": "1"}, timeout=timeout
    )
    _hopeless(reply, channel)
    if reply.why:
        raise SlackUnreachableError(reply.why)
    found = reply.body.get("messages")
    newest = found[0] if isinstance(found, list) and found else None
    if isinstance(newest, dict) and newest.get("ts"):
        return str(newest["ts"])
    # An empty channel has no timestamp to start after, and starting at zero
    # would mean "everything". Nothing was said, so nothing is missed by using
    # this clock instead of Slack's.
    return f"{time.time():.6f}"


def since(
    token: str,
    channel: str,
    after: str,
    *,
    limit: int = PAGE,
    pages: int = MOST_PAGES,
    timeout: float = TIMEOUT_SECONDS,
) -> Heard:
    """Everything said in ``channel`` since ``after``, oldest first.

    Raises only when Slack refuses the credential or the channel; a poll that
    merely failed comes back saying so, with the cursor untouched.
    """
    collected: list[Mapping[str, object]] = []
    cursor = ""
    for _ in range(max(pages, 1)):
        params = {"channel": channel, "oldest": after, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        reply = _read("conversations.history", token, params, timeout=timeout)
        _hopeless(reply, channel)
        if reply.why:
            return Heard(cursor=after, why=reply.why, after=reply.after)
        found = reply.body.get("messages")
        if isinstance(found, list):
            collected += [one for one in found if isinstance(one, dict)]
        if not reply.body.get("has_more"):
            break
        cursor = _next_page(reply.body)
        if not cursor:
            break
    else:
        # Never silently: the oldest unread messages are dropped, and whoever
        # typed one would otherwise just see nothing happen.
        logger.warning(
            "%s said more in one poll than %d pages hold; the oldest were not read",
            channel,
            pages,
        )
    # Sorted here rather than trusted: Slack pages from whichever end of the
    # window it chooses, and the cursor has to be the newest either way.
    collected.sort(key=_when)
    moved = str(collected[-1].get("ts", "")) if collected else ""
    return Heard(messages=tuple(collected), cursor=moved or after)


def _next_page(body: Mapping[str, object]) -> str:
    meta = body.get("response_metadata")
    found = meta.get("next_cursor") if isinstance(meta, dict) else None
    return str(found) if isinstance(found, str) else ""


def _when(message: Mapping[str, object]) -> float:
    """A message's timestamp as a number. Unreadable sorts oldest."""
    try:
        return float(str(message.get("ts", "0")))
    except ValueError:
        return 0.0


# --- the loop -----------------------------------------------------------------


def overheard(
    token: str,
    *,
    channels: Sequence[str],
    triggers: Sequence[Trigger],
    every: float = POLL_SECONDS,
    pause: Callable[[float], None] = time.sleep,
) -> Generator[Asked, None, None]:
    """Every request typed into ``channels``, read with ``token``.

    ``Asked`` and nothing else: a press is pushed to an app and never appears
    in a channel's history, so this cannot be handed to anything answering a
    gate.

    The first trigger that matches wins, in the order manifests were found —
    the same rule ``events`` follows, so one message starts one run either way.

    Each channel's cursor is fixed at the first poll that reaches Slack, not at
    the first poll attempted: a channel that could not be read yet is read from
    whenever it can be, and never from the beginning.

    Raises only when Slack refuses the credential or one of the channels.
    """
    cursors: dict[str, str] = {}
    throttled = False
    while True:
        wait = every
        for channel in channels:
            if channel not in cursors:
                try:
                    cursors[channel] = latest(token, channel)
                except SlackUnreachableError as exc:
                    # Starting is the one call with nothing behind it, so a blip
                    # here used to end the listener. Waiting the longer of the
                    # two, because an unreachable Slack and a throttled one look
                    # the same from here and only one of them tolerates haste.
                    logger.warning("could not start reading %s (%s); trying again", channel, exc)
                    wait = max(wait, _BACKOFF_SECONDS)
                    continue
            heard = since(token, channel, cursors[channel])
            if heard.after:
                # Slack's own number, not a doubling: it knows when the window
                # reopens and guessing only spends the next one too.
                wait = max(wait, heard.after)
                if not throttled:
                    logger.warning(
                        "Slack is rate limiting this token; polling every %gs instead",
                        heard.after,
                    )
                    throttled = True
                continue
            throttled = False
            if heard.why:
                logger.warning("could not read %s (%s); trying again", channel, heard.why)
                continue
            cursors[channel] = heard.cursor
            for message in heard.messages:
                for trigger in triggers:
                    request = request_in(message, channel, trigger)
                    if request is not None:
                        yield request
                        break
        pause(wait)
