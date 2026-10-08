"""Building a Slack report, and posting one.

The program below is what an announcement step runs. It is a string because the
step is a subprocess: the graph carries it without reading it, the way it
carries an MCP server's command, which is what keeps Slack out of ``graph`` and
``stdlib``.

Three decisions in it are not preferences:

* The credential is read from the environment, never written into a pipeline or
  an emitted workflow.
* The message goes down stdin, not argv. A gate prompt is prose, and the first
  apostrophe would end a shell-interpolated one.
* Threading needs ``chat.postMessage``. A webhook accepts ``thread_ts`` but
  never returns the ``ts`` of what it posted, so there is no parent to reply
  under. Both shapes are here; only one can hold a conversation.
"""

from __future__ import annotations

import http.client
import json
import os
import string
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from ictus.graph.requirements import Integration

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ictus.graph.requirements import EnvVar
    from ictus.graph.signals import RunSignal

__all__ = ["API", "API_ENV", "api_call", "endpoint", "reply", "slack_channel", "slack_webhook"]

API = "https://slack.com/api/chat.postMessage"

#: Overrides it — a proxy, an Enterprise Grid host, or a local stand-in so the
#: whole path can be exercised without a workspace.
API_ENV = "SLACK_API_URL"

TIMEOUT_SECONDS = 15

#: Below the step's own timeout, which the engine treats as a failure.
DEADLINE_SECONDS = 20

#: Block Kit's ceilings. Past either, Slack refuses the message, buttons and all.
SECTION_LIMIT = 3000
LABEL_LIMIT = 75


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

    Listens, too: `ictus listen` holds a socket open to the same workspace, so
    a pipeline can declare that a message here starts it. That needs an
    app-level token as well, read by the listener rather than by a run.
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

    Simpler to set up and strictly less capable: no threads and no buttons, so
    runs interleave and nothing can be answered from Slack. One-way as well —
    a webhook is an address to post to, with nothing to hold open and nothing
    to read — so it cannot start a run either.
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
    return _PROGRAM.substitute(
        secret_name=repr(secret),
        channel_name=repr(channel),
        api=repr(API),
        api_env=repr(API_ENV),
        request_timeout=str(TIMEOUT_SECONDS),
        deadline=str(DEADLINE_SECONDS),
        section_limit=str(SECTION_LIMIT),
        label_limit=str(LABEL_LIMIT),
    )


#: Reads the message on stdin, and the parent thread, the buttons and a deadline
#: from argv; prints ``{"thread", "posted"}`` and exits 0 whatever happened.
#:
#: Exiting 0 is the point. The program runs as a step and a failed step fails
#: the run, so an outage or a rotated token ended runs whose work had succeeded.
#: A report that cannot be sent says ``posted: "false"``, with the reason on
#: stderr. The deadline runs on a second thread because a socket timeout does
#: not bound a hung name lookup, and the engine fails a step that overruns.
#:
#: A reason never quotes an exception: urllib puts the URL in some of its own,
#: and a webhook URL is the whole credential.
#:
#: ``string.Template`` so the dicts read as Python. It may contain no ``{{``,
#: ``{%`` or ``{#``: the engine renders every argument as a template.
_PROGRAM = string.Template(
    r"""import json, os, sys, threading, urllib.error, urllib.request

SECRET_NAME = ${secret_name}
CHANNEL_NAME = ${channel_name}
ENDPOINT = os.environ.get(${api_env}) or ${api}
SECTION_LIMIT = ${section_limit}
LABEL_LIMIT = ${label_limit}
HINTS = {
    "missing_scope": "the bot token needs chat:write - add it under OAuth & Permissions, "
    "then reinstall the app to the workspace",
    "not_in_channel": "the app is not in that channel - /invite it there",
    "channel_not_found": "check the channel id; it looks like C0ABC123 and is at the bottom "
    "of View channel details",
    "invalid_auth": "the token is wrong or has been revoked",
    "account_inactive": "the app has been disabled in that workspace",
    "token_revoked": "the token has been rotated; export the new one",
    "ratelimited": "Slack is rate limiting this app; the run carries on without the report",
}

raw_secret = os.environ.get(SECRET_NAME) or ""
secret = raw_secret.strip()
lock = threading.Lock()
said = []


def scrub(text):
    for value in (raw_secret, secret):
        if value:
            text = text.replace(value, "***")
    return text


def note(why):
    # Said alongside the report rather than instead of it: the message landed,
    # so `posted` is still true, and this is the part a reader needs to know.
    # `say` writes once and is spoken for by the outcome.
    sys.stderr.write(scrub(why) + "\n")
    sys.stderr.flush()


def say(thread, posted, why=""):
    # Once, from whichever of the send and the deadline gets here first.
    with lock:
        if said:
            return
        said.append(True)
        if why:
            sys.stderr.write(scrub(why) + "\n")
            sys.stderr.flush()
        sys.stdout.write(json.dumps({"thread": thread, "posted": posted}) + "\n")
        sys.stdout.flush()


def reason(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "the service answered HTTP " + str(exc.code)
    if isinstance(exc, urllib.error.URLError):
        return "could not reach the service (" + type(exc.reason).__name__ + ")"
    if isinstance(exc, TimeoutError):
        return "the service did not answer in time"
    return "the report failed (" + type(exc).__name__ + ")"


def clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def post(url, body, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def button(asks, value, label, ask, multiline):
    # No run id: the run is found from the message the button is on, which the
    # run's own history records, so a press cannot name the wrong one.
    pressed = {
        "gate": asks["gate"],
        "step": asks["step"],
        "choice": value,
        "ask": ask,
        "multiline": multiline,
    }
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": clip(label, LABEL_LIMIT)},
        "value": json.dumps(pressed),
        "action_id": clip("ictus_gate_" + value, 255),
    }


def send(text, parent, asks, timeout):
    if not secret:
        return None, SECRET_NAME + " is not set, so there was nowhere to report"
    if not CHANNEL_NAME:
        if not secret.startswith(("https://", "http://")):
            return None, SECRET_NAME + " does not look like a webhook URL; it should begin https://"
        post(secret, {"text": clip(text, 40000)}, {"Content-Type": "application/json"}, timeout)
        # A webhook never says where the message landed.
        return "", ""
    channel = (os.environ.get(CHANNEL_NAME) or "").strip()
    if not channel:
        return None, CHANNEL_NAME + " is not set, so there was no channel to post in"
    body = {"channel": channel, "text": clip(text, 40000)}
    if parent:
        body["thread_ts"] = parent
    if asks:
        body["blocks"] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": clip(text, SECTION_LIMIT)}},
            {
                "type": "actions",
                "elements": [button(asks, *spec) for spec in asks["buttons"]],
            },
        ]
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + secret,
    }
    answer = json.loads(post(ENDPOINT, body, headers, timeout))
    if not answer.get("ok"):
        code = str(answer.get("error"))
        hint = HINTS.get(code, "")
        return None, "slack refused the message: " + code + ((" - " + hint) if hint else "")
    # Asking to reply under a message that is no longer there does not fail: the
    # message is accepted and placed at the top of the channel instead. Several
    # runs then read as one stream of unattributed updates, and nothing says why
    # — which is how it was found, by someone noticing it had stopped replying.
    if parent:
        placed = answer.get("message")
        landed = str(placed.get("thread_ts", "")) if isinstance(placed, dict) else ""
        if landed != parent:
            note(
                "asked to reply under " + parent + " and slack put this in the channel "
                "instead; that message was probably deleted"
            )
    # Slack calls it ts; the graph calls it a thread.
    return str(answer.get("ts", "")), ""


def main():
    try:
        deadline = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else ${deadline}
    except ValueError:
        deadline = ${deadline}

    def give_up():
        say("", "false", "no answer within %gs; the run goes on without this report" % deadline)
        os._exit(0)

    timer = threading.Timer(deadline, give_up)
    timer.daemon = True
    timer.start()
    try:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
        parent = sys.argv[1].strip() if len(sys.argv) > 1 else ""
        asks = json.loads(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {}
        thread, problem = send(text, parent, asks, min(${request_timeout}, deadline))
    except Exception as exc:
        thread, problem = None, reason(exc)
    if problem:
        say("", "false", problem)
    else:
        say(thread or "", "true")


main()
"""
)


def endpoint(method: str) -> str:
    """Where a Web API method lives, honouring the override the program honours.

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

    Never raises, and never quotes an exception, for the program's reason. The
    answer comes back on a refusal too: its ``error`` decides whether a caller
    gives up or tries again.
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

    Not a click's ``response_url``: that posts where the *message* lives, which
    for a button in a thread is the channel root — so the answer landed beside
    every other run's. Never raises; the gate is already answered by now.
    """
    body: dict[str, object] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    _, why = api_call("chat.postMessage", token, body, timeout=timeout)
    return why
