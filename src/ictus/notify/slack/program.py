"""The sending program, as data.

Not Python this module runs: a string substituted into an ``Integration`` and
executed by the engine as a subprocess. That is what keeps Slack out of
``graph`` and ``stdlib`` — the step has no ictus on its path and no import of
this package. It is 47% of what used to be one file and none of it is reachable
from here, which is why it is on its own.

Three rules it exists to hold:

* The credential is read from the environment, never written into a pipeline
  or an emitted workflow.
* The message goes down stdin, not argv: a prompt is prose.
* Threading needs ``chat.postMessage``. A webhook accepts ``thread_ts`` but
  never returns the ``ts`` of what it posted, so it cannot hold a conversation.
"""

from __future__ import annotations

import string

__all__ = ["DEADLINE_SECONDS", "PROGRAM"]

#: Below the step's own timeout, which the engine treats as a failure.
DEADLINE_SECONDS = 20


#: Reads the message on stdin, and the parent thread, the buttons and a
#: deadline from argv; prints ``{"thread", "posted"}`` and exits 0 whatever
#: happened, so a failed report does not fail the run. The reason goes to
#: stderr. The deadline runs on a second thread, since a socket timeout does
#: not bound a hung name lookup.
#:
#: A reason never quotes an exception: urllib puts the URL in some of its own,
#: and a webhook URL is the whole credential.
#:
#: ``string.Template`` so the dicts read as Python. It may contain no ``{{``,
#: ``{%`` or ``{#``: the engine renders every argument as a template.
PROGRAM = string.Template(
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
    # Alongside the report, not instead of it: the message landed, so
    # `posted` is still true.
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
    # No run id: the run is found from the message the button is on.
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
    # Replying under a deleted message does not fail: Slack accepts it and
    # puts it at the top of the channel instead.
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
