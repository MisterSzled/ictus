"""Slack's Web API, as something ictus calls directly.

The live half of this package: ``notify`` is otherwise pure data, and these
three functions open a socket. The bridge is their only caller — it asks this
host the same questions in the other direction — and the sending program does
not use them at all, because it runs as a subprocess with no ictus on its path.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "API",
    "API_ENV",
    "LABEL_LIMIT",
    "SECTION_LIMIT",
    "TIMEOUT_SECONDS",
    "api_call",
    "endpoint",
    "reply",
]

API = "https://slack.com/api/chat.postMessage"

#: Overrides it: a proxy, an Enterprise Grid host, or a local stand-in.
API_ENV = "SLACK_API_URL"

TIMEOUT_SECONDS = 15
"""How long one Web API call may take. Read by the bridge too, which asks the
same host the same questions in the other direction."""

#: Below the step's own timeout, which the engine treats as a failure.
DEADLINE_SECONDS = 20

#: Block Kit's ceilings. Past either, Slack refuses the message, buttons and all.
SECTION_LIMIT = 3000
LABEL_LIMIT = 75


def endpoint(method: str) -> str:
    """Where a Web API method lives, honouring :data:`API_ENV`.

    The override names ``chat.postMessage``; every other method sits beside it.
    """
    override = os.environ.get(API_ENV, "").strip()
    return (override or API).rsplit("/", 1)[0] + "/" + method


def api_call(
    method: str,
    token: str,
    body: Mapping[str, object],
    *,
    timeout: float = TIMEOUT_SECONDS,
) -> tuple[dict[str, object], str]:
    """Call one Web API method: ``(answer, "")``, or ``(answer, why not)``.

    Never raises, and never quotes an exception. The answer comes back on a
    refusal too, since its ``error`` decides whether to try again.
    """
    request = urllib.request.Request(
        endpoint(method),
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {}, f"Slack answered HTTP {exc.code} to {method}"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return {}, f"could not reach Slack ({type(exc).__name__})"
    if not isinstance(answer, dict):
        return {}, f"Slack sent an unreadable answer to {method}"
    if not answer.get("ok"):
        return answer, f"Slack refused {method}: {answer.get('error')}"
    return answer, ""


def reply(
    *,
    token: str,
    channel: str,
    thread_ts: str,
    text: str,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Say ``text`` under ``thread_ts``. Returns "" on success, else why not.

    Not a click's ``response_url``, which posts where the message lives — the
    channel root, for a button in a thread. Never raises.
    """
    body: dict[str, object] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    _, why = api_call("chat.postMessage", token, body, timeout=timeout)
    return why
