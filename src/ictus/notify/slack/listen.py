"""Receiving a button press, and saying what became of it.

Socket Mode: the app dials out to Slack and events arrive down that socket, so
nothing here has to be publicly reachable. Slack recommends HTTP for production
and is right about it at scale; this is the shape that works behind a firewall,
and everything above ``presses`` is transport-agnostic.

Three properties of the connection are not preferences:

* Every envelope is acknowledged before anything is done about it. Slack
  retries what it thinks did not arrive, and a retry looks exactly like a
  second press.
* Slack replaces the connection when it sends ``disconnect``; the URL is
  single-use.
* It also drops without asking — a laptop sleeps, a VPN reconnects. Any drop is
  a reason to dial again, after a pause that grows while dialling keeps failing.
  Only Slack refusing the credential ends the listener; the first version ended
  on the first dropped connection, and a listener that has quietly stopped looks
  exactly like a quiet channel.

A press that arrives while no socket is open is lost; there is no replay. That
is the trade against an HTTP endpoint, and why the dashboard stays the thing of
record.

Nothing here knows which engine runs a gate. A press becomes a ``Click``, and
finding its run and answering it is ``ictus.answer``'s.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ictus.errors import IctusError
from ictus.notify.slack.send import SECTION_LIMIT, api_call, reply
from ictus.notify.slack.trigger import Asked, Trigger, asked
from ictus.websocket import HandshakeError, connect

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Click",
    "Note",
    "SlackError",
    "SlackUnreachableError",
    "events",
    "open_form",
    "open_socket",
    "presses",
    "retire",
    "say",
    "verdict",
]

#: The form a choice that needs text opens, so its submission is recognised.
FORM_ID = "ictus_gate_note"

RETRY_FIRST_SECONDS = 1.0
RETRY_MOST_SECONDS = 60.0

#: Slack refusing the credential itself. Dialling again changes nothing.
_FATAL = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "not_allowed_token_type",
        "missing_scope",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "invalid_token",
    }
)


class SlackError(IctusError):
    """Slack refused the connection: the app-level token, or its scope."""


class SlackUnreachableError(SlackError):
    """Slack could not be reached, or asked to be tried later."""


@dataclass(frozen=True, slots=True)
class Click:
    """Somebody pressed a button that answers a gate."""

    gate: str
    choice: str
    step: str
    """The step that posted the button. Which run it belongs to is found from this."""

    who: str
    """The Slack user id; the only authorisation signal there is."""

    channel: str = ""
    message_ts: str = ""
    """The message the button is on: which time the question was asked."""

    thread_ts: str = ""
    """What the answer goes under. A thread root has no ``thread_ts`` of its own —
    its ``ts`` is the thread."""

    ask: str = ""
    """The text this choice needs, by name; empty when it needs none."""

    multiline: bool = False
    label: str = ""
    trigger_id: str = ""
    """Opens a form for about three seconds after the press."""

    text: str = ""
    """The message's own words, kept so its buttons can be retired around them."""


@dataclass(frozen=True, slots=True)
class Note:
    """Somebody sent the form a choice opened."""

    click: Click
    text: str


def events(
    envelope: Mapping[str, object], triggers: Sequence[Trigger] = ()
) -> Iterator[Click | Note | Asked]:
    """Everything actionable in one envelope: presses, forms, and requests.

    The first trigger that matches wins. Two pipelines sharing a prefix is a
    thing somebody wrote by mistake, and starting both would charge twice for
    it; the order is the order manifests were found, which is sorted by path.
    """
    for trigger in triggers:
        request = next(asked(dict(envelope), trigger), None)
        if request is not None:
            yield request
            break
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return
    if payload.get("type") == "block_actions":
        yield from _pressed(payload)
    elif payload.get("type") == "view_submission":
        note = _noted(payload)
        if note is not None:
            yield note


def _pressed(payload: dict[str, object]) -> Iterator[Click]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return
    user = payload.get("user")
    who = str(user.get("id", "")) if isinstance(user, dict) else ""
    channel, message_ts, thread_ts, text = _where(payload)
    trigger = payload.get("trigger_id")
    for action in actions:
        if not isinstance(action, dict):
            continue
        try:
            value = json.loads(str(action.get("value", "")))
        except json.JSONDecodeError:
            continue  # somebody else's button, in a channel we also watch
        if not isinstance(value, dict) or not {"gate", "choice", "step"} <= set(value):
            continue
        label = action.get("text")
        yield Click(
            gate=str(value["gate"]),
            choice=str(value["choice"]),
            step=str(value["step"]),
            who=who,
            channel=channel,
            message_ts=message_ts,
            thread_ts=thread_ts,
            ask=str(value.get("ask") or ""),
            multiline=bool(value.get("multiline")),
            label=str(label.get("text", "")) if isinstance(label, dict) else "",
            trigger_id=str(trigger) if isinstance(trigger, str) else "",
            text=text,
        )


def _where(payload: dict[str, object]) -> tuple[str, str, str, str]:
    """The channel, the message, the thread and the words a press came from."""
    channel = payload.get("channel")
    where = str(channel.get("id", "")) if isinstance(channel, dict) else ""
    message = payload.get("message")
    if not isinstance(message, dict):
        container = payload.get("container")
        message = container if isinstance(container, dict) else {}
    own = str(message.get("ts") or message.get("message_ts") or "")
    thread = str(message.get("thread_ts") or own)
    text = message.get("text")
    return where, own, thread, text if isinstance(text, str) else ""


def _noted(payload: dict[str, object]) -> Note | None:
    view = payload.get("view")
    if not isinstance(view, dict) or view.get("callback_id") != FORM_ID:
        return None
    try:
        kept = json.loads(str(view.get("private_metadata", "")))
    except json.JSONDecodeError:
        return None
    if not isinstance(kept, dict) or not {"gate", "choice", "step"} <= set(kept):
        return None
    user = payload.get("user")
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    block = values.get("note") if isinstance(values, dict) else None
    field = block.get("text") if isinstance(block, dict) else None
    text = field.get("value") if isinstance(field, dict) else None
    click = Click(
        gate=str(kept["gate"]),
        choice=str(kept["choice"]),
        step=str(kept["step"]),
        who=str(user.get("id", "")) if isinstance(user, dict) else "",
        channel=str(kept.get("channel", "")),
        message_ts=str(kept.get("message_ts", "")),
        thread_ts=str(kept.get("thread_ts", "")),
        ask=str(kept.get("ask", "")),
        label=str(kept.get("label", "")),
        text=str(kept.get("text", "")),
    )
    return Note(click=click, text=text if isinstance(text, str) else "")


# -- the connection -------------------------------------------------------------


def open_socket(app_token: str) -> str:
    """Ask Slack for a websocket URL. Single use, and it expires quickly."""
    answer, why = api_call("apps.connections.open", app_token, {})
    if why:
        error = str(answer.get("error", ""))
        if error in _FATAL:
            raise SlackError(
                f"Slack refused the connection: {error} - the app-level token needs "
                "connections:write, and Socket Mode has to be enabled under "
                "Settings > Socket Mode"
            )
        raise SlackUnreachableError(why)
    url = answer.get("url")
    if not isinstance(url, str):
        raise SlackUnreachableError("Slack opened a connection but named no url")
    return url


def presses(
    app_token: str,
    *,
    triggers: Sequence[Trigger] = (),
    pause: Callable[[float], None] = time.sleep,
) -> Generator[Click | Note | Asked, None, None]:
    """Every press, submitted form and request to start a run.

    Raises only when Slack refuses the credential.
    """
    wait = RETRY_FIRST_SECONDS
    while True:
        try:
            socket = connect(open_socket(app_token))
        except (SlackUnreachableError, HandshakeError, OSError) as exc:
            logger.warning("could not connect to Slack (%s); trying again in %gs", exc, wait)
            pause(wait)
            wait = min(wait * 2, RETRY_MOST_SECONDS)
            continue
        wait = RETRY_FIRST_SECONDS
        try:
            for raw in socket.messages():
                envelope = _parsed(raw)
                if envelope is None:
                    continue
                kind = envelope.get("type")
                if kind == "disconnect":
                    break
                if kind == "hello":
                    continue
                envelope_id = envelope.get("envelope_id")
                if isinstance(envelope_id, str):
                    socket.send(json.dumps({"envelope_id": envelope_id}))
                yield from events(envelope, triggers)
        except (OSError, NotImplementedError) as exc:
            logger.warning("the connection to Slack dropped (%s); reconnecting", type(exc).__name__)
        finally:
            socket.close()


def _parsed(raw: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


# -- answering in the channel ---------------------------------------------------


def open_form(token: str, click: Click) -> str:
    """Ask whoever pressed for the text their choice needs. "" if asked.

    Without this, "reject, with notes" was answered with no notes and the
    revision ran on nothing. What the form answers travels inside it, because
    its submission arrives later and on its own.
    """
    kept = {key: value for key, value in asdict(click).items() if key not in ("who", "trigger_id")}
    kept["text"] = click.text[:1500]  # a form keeps at most 3000 characters
    view = {
        "type": "modal",
        "callback_id": FORM_ID,
        "private_metadata": json.dumps(kept),
        "title": {"type": "plain_text", "text": _clip(click.label or click.choice, 24)},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "note",
                "label": {"type": "plain_text", "text": _clip(click.ask, 2000)},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": click.multiline,
                },
            }
        ],
    }
    _, why = api_call("views.open", token, {"trigger_id": click.trigger_id, "view": view})
    return why


def retire(token: str, click: Click, line: str) -> str:
    """Take the buttons off an answered question. "" if done.

    Left pressable, an answered question invites a second answer — and in a
    loop the same buttons come back on a newer message.
    """
    words = click.text or f"`{click.gate}`"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(words, SECTION_LIMIT)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(line, SECTION_LIMIT)}]},
    ]
    body = {"channel": click.channel, "ts": click.message_ts, "text": words, "blocks": blocks}
    _, why = api_call("chat.update", token, body)
    return why


def verdict(click: Click, *, answered: bool, reason: str = "", run_id: str = "") -> str:
    """One line for the thread: what became of this press."""
    on = f" on run `{run_id}`" if run_id else ""
    if answered:
        return f"<@{click.who}> answered *{click.choice}*{on}"
    return f"<@{click.who}> pressed *{click.choice}*, and nothing was done: {reason}"


def say(click: Click, text: str, *, token: str) -> None:
    """Post ``text`` under the question. Logged, never raised, when it fails.

    A thread that goes quiet after a press looks like nothing happened.
    """
    if not token or not click.channel:
        return
    why = reply(token=token, channel=click.channel, thread_ts=click.thread_ts, text=text)
    if why:
        logger.warning("could not say what happened to %s: %s", click.gate, why)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"
