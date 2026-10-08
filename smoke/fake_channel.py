#!/usr/bin/env python3
"""Stands in for Slack, so a report can be watched without a workspace.

Answers both shapes ictus posts in:

* an **incoming webhook** — any other path. Takes `{"text": ...}` and returns
  `ok`, exactly as Slack's does, including returning no timestamp, which is why
  a webhook cannot be threaded onto.
* **chat.postMessage** — returns `{"ok": true, "ts": ..., "message": {...}}`, so a
  run can learn its own thread and reply under it, and can tell a reply that
  landed from one Slack quietly put at the top of the channel instead.

    python3 smoke/fake_channel.py
    export SLACK_BOT_TOKEN=xoxb-pretend
    export SLACK_CHANNEL=C0PRETEND
    export SLACK_API_URL=http://127.0.0.1:8723/api/chat.postMessage

The transcript it prints is indented by thread, which is the thing worth
checking: several runs at once should read as separate conversations.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

PORT = 8723


class Slack(BaseHTTPRequestHandler):
    """Prints what arrived, nested under the thread it belongs to."""

    roots: ClassVar[dict[str, str]] = {}
    next_ts: ClassVar[float] = 1000.0

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
        # A parent nobody has seen is one that was deleted. Slack does not
        # refuse that — it accepts the message and puts it at the top of the
        # channel — so neither does this, because the whole point of standing
        # in for Slack is to reproduce the behaviour that caught somebody out.
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
    HTTPServer(("127.0.0.1", PORT), Slack).serve_forever()
