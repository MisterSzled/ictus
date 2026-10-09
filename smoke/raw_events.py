"""Print every envelope Slack sends, exactly as it sends it.

For when the listener is running and not reacting: is the event arriving, and
what does its text actually say? Slack composes for a human reader — a link
becomes ``<url|label>``, bold becomes ``*bold*`` — so the string a trigger
matches is rarely the string on screen.

Run it with the listener stopped: Slack delivers an event to exactly one of an
app's open sockets.

    python3 smoke/raw_events.py            # everything
    python3 smoke/raw_events.py message    # only this envelope type

Needs $SLACK_APP_TOKEN. Ctrl-C to stop.
"""

from __future__ import annotations

import json
import os
import sys

from ictus.bridge.slack.listen import _parsed, open_socket
from ictus.net.websocket import connect

APP_TOKEN_ENV = "SLACK_APP_TOKEN"


def _event_of(envelope: dict[str, object]) -> dict[str, object] | None:
    """The inner event, when this envelope carries one."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    return event if isinstance(event, dict) else None


def _describe(envelope: dict[str, object]) -> str:
    """One line naming what arrived, and the detail a trigger would read."""
    kind = str(envelope.get("type", "?"))
    event = _event_of(envelope)
    if event is None:
        payload = envelope.get("payload")
        inner = payload.get("type") if isinstance(payload, dict) else None
        return f"{kind} ({inner})"
    # `subtype` is the field that silently disqualifies a message: an edit, a
    # join, or the copy Slack sends when a link finishes unfurling.
    marks = [f"type={event.get('type')}"]
    for field in ("subtype", "bot_id", "app_id", "user", "channel", "thread_ts"):
        if event.get(field):
            marks.append(f"{field}={event[field]}")
    return f"{kind}: " + " ".join(marks)


def main() -> int:
    token = os.environ.get(APP_TOKEN_ENV, "")
    if not token:
        print(f"${APP_TOKEN_ENV} is not set", file=sys.stderr)
        return 1
    only = sys.argv[1] if len(sys.argv) > 1 else ""

    socket = connect(open_socket(token))
    print("connected. post in the channel; Ctrl-C to stop.\n", flush=True)
    try:
        for raw in socket.messages():
            envelope = _parsed(raw)
            if envelope is None:
                continue
            if envelope.get("type") == "hello":
                continue
            # Acknowledge regardless of the filter, or Slack redelivers
            # everything this run chose not to print.
            envelope_id = envelope.get("envelope_id")
            if isinstance(envelope_id, str):
                socket.send(json.dumps({"envelope_id": envelope_id}))
            event = _event_of(envelope)
            if only and not (
                envelope.get("type") == only or (event is not None and event.get("type") == only)
            ):
                continue
            print(_describe(envelope), flush=True)
            if event is not None and "text" in event:
                # repr, not the string: the whole point is to see the
                # characters Slack actually sent, including the ones that
                # render as something else.
                print(f"  text = {event['text']!r}", flush=True)
            print(flush=True)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
