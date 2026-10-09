#!/usr/bin/env python3
"""Stands in for Slack, so a report can be watched without a workspace.

Answers both shapes ictus posts in:

* an **incoming webhook** — any other path. Takes `{"text": ...}` and returns
  `ok` with no timestamp, which is why a webhook cannot be threaded onto.
* **chat.postMessage** — returns `{"ok": true, "ts": ..., "message": {...}}`,
  so a run can learn its own thread and reply under it.

and the one shape `ictus-bridge overhear` reads:

* **conversations.history** — whatever you have typed at this program, newest
  first, honouring `oldest` and `limit`. Type a line here and `overhear` starts
  a run from it, the same way a channel would.

    python3 smoke/fake_channel.py
    export SLACK_BOT_TOKEN=xoxb-pretend
    export SLACK_CHANNEL=C0PRETEND
    export SLACK_API_URL=http://127.0.0.1:8723/api/chat.postMessage

The transcript is indented by thread, so several runs at once read as
separate conversations.

What a run posts is *not* added to the history. A real `overhear` posts as the
person listening, so its line is an ordinary message — it starts nothing only
because its wording never begins with a prefix, and reproducing that subtlety
in a stand-in would teach the wrong lesson about where the guard is.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

PORT = 8723

#: What has been typed at this program, oldest first, as Slack would hold it.
SAID: list[dict[str, object]] = []
_SAID_LOCK = threading.Lock()


def _typing() -> None:
    """Turn every line typed here into a message in the channel.

    Stamped from the clock, because Slack's `ts` is epoch seconds and a
    listener starting on an empty channel has nothing else to start after. A
    counter from 2000.0 sorts before every such cursor, so nothing typed here
    was ever read — which is how this was found.
    """
    for line in sys.stdin:
        text = line.rstrip("\n")
        if not text:
            continue
        said_at = f"{time.time():.6f}"
        with _SAID_LOCK:
            SAID.append(
                {
                    "type": "message",
                    "user": "U0YOU",
                    "ts": said_at,
                    "text": text,
                }
            )
        # A thread of its own, so what a listener says about it nests under it
        # rather than landing at the top of the channel.
        Slack.roots[said_at] = said_at
        print(f"  (you said) {text}")
        sys.stdout.flush()


class Slack(BaseHTTPRequestHandler):
    """Prints what arrived, nested under the thread it belongs to."""

    roots: ClassVar[dict[str, str]] = {}
    next_ts: ClassVar[float] = 1000.0

    def do_GET(self) -> None:
        if not self.path.split("?")[0].endswith("conversations.history"):
            self._reply(404, b'{"ok": false, "error": "unknown_method"}')
            return
        query = urllib.parse.urlparse(self.path).query
        params = {key: value[0] for key, value in urllib.parse.parse_qs(query).items()}
        oldest = float(params.get("oldest") or 0)
        limit = int(params.get("limit") or 100)
        with _SAID_LOCK:
            window = [one for one in SAID if float(str(one["ts"])) > oldest]
        # Newest first, which is the order Slack answers in and the reason
        # `since` sorts what it collects rather than trusting the order.
        page = list(reversed(window))[:limit]
        self._reply(200, json.dumps({"ok": True, "messages": page}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"text": raw.decode(errors="replace")}
        text = str(body.get("text", ""))

        if self.path.endswith("chat.postMessage"):
            self._threaded(body, text)
        else:
            print("\n(unthreaded, straight to the channel)")
            print(text)
            self._reply(200, b"ok")
        sys.stdout.flush()

    def _threaded(self, body: dict[str, object], text: str) -> None:
        parent = str(body.get("thread_ts") or "")
        type(self).next_ts += 1
        ts = f"{type(self).next_ts:.6f}"
        # A parent nobody has seen is one that was deleted. Slack accepts the
        # message and puts it at the top of the channel, so this does too.
        known = parent in type(self).roots
        if parent and known:
            root = type(self).roots[parent]
            print(f"      ↳ [thread {root}] " + text.replace("\n", "\n        "))
            type(self).roots[ts] = root
        else:
            if parent:
                print(f"\n=== new thread {ts} (asked for {parent}, which is not here) ===")
            else:
                print(f"\n=== new thread {ts} ===")
            print(text)
            type(self).roots[ts] = ts
        # `message` carries what actually happened, which is the only way a
        # caller can tell a reply from one that silently landed at the root.
        posted: dict[str, str] = {"ts": ts}
        if parent and known:
            posted["thread_ts"] = type(self).roots[ts]
        self._reply(
            200,
            json.dumps({"ok": True, "ts": ts, "channel": "C0PRETEND", "message": posted}).encode(),
        )

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """Quiet: the transcript above is the output worth reading."""


if __name__ == "__main__":
    print(f"listening on http://127.0.0.1:{PORT} — ctrl-c to stop")
    print("type a line to say it in the channel, e.g. 'Start test run: why is the bus failing?'")
    threading.Thread(target=_typing, daemon=True).start()
    HTTPServer(("127.0.0.1", PORT), Slack).serve_forever()
