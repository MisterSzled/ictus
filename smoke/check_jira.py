"""Check a Jira credential the same way a run would, before wiring it in.

Runs the exact program `ictus.sources.readonly_jira` compiles into a workflow,
against the variables already in the environment — so a pass here means a run
will work, and a failure says which of the three is wrong rather than leaving
it to be discovered in a Slack thread.

It also checks the shape of the permission: a token that can *write* is a token
this pipeline should not be holding, so the second half tries one write and
expects to be refused. Nothing is created either way — the attempt targets an
issue key that cannot exist.

    python3 smoke/check_jira.py DB-8790

Needs $JIRA_EMAIL, $JIRA_API_TOKEN and $JIRA_SITE.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from ictus.graph.requirements import EnvVar
from ictus.sources import readonly_jira

NEEDED = ("JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_SITE")


def _read(issue: str) -> dict[str, str]:
    """Run the compiled reading program exactly as a step would."""
    source = readonly_jira(
        email=EnvVar("JIRA_EMAIL", "the account that reads tickets"),
        token=EnvVar("JIRA_API_TOKEN", "an API token for that account"),
        site=EnvVar("JIRA_SITE", "the site"),
    )
    done = subprocess.run(
        [sys.executable, "-c", source.program, issue, "20"],
        capture_output=True,
        text=True,
        input="",
        check=False,
    )
    answer: dict[str, str] = json.loads(done.stdout)
    return answer


def _authorization() -> tuple[str, str]:
    """Where this credential has power, and the header that proves it.

    A scoped token is refused at the site and accepted at Atlassian's gateway;
    a classic one is the other way round. Testing the wrong one reports a
    reassuring refusal that means nothing, so resolve it the way the compiled
    program does before asking what the token may do.
    """
    site = os.environ["JIRA_SITE"].rstrip("/")
    token = os.environ["JIRA_API_TOKEN"]
    try:
        with urllib.request.urlopen(f"{site}/_edge/tenant_info", timeout=20) as answer:
            cloud = json.loads(answer.read().decode("utf-8")).get("cloudId")
    except (urllib.error.URLError, OSError, ValueError):
        cloud = None
    if cloud:
        probe = urllib.request.Request(
            f"https://api.atlassian.com/ex/jira/{cloud}/rest/api/3/myself", method="GET"
        )
        probe.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(probe, timeout=20) as answer:
                answer.read()
        except urllib.error.HTTPError:
            pass
        else:
            return f"https://api.atlassian.com/ex/jira/{cloud}", f"Bearer {token}"
    pair = base64.b64encode(f"{os.environ['JIRA_EMAIL']}:{token}".encode()).decode("ascii")
    return site, f"Basic {pair}"


def _may_write(base: str, header: str) -> str | None:
    """Whether the credential can comment. ``None`` when it is refused.

    Aimed at an issue key no project uses, so a token that *can* write still
    writes nothing: the answer arrives as 404 (allowed, no such issue) or 403
    (not allowed at all), and those are different facts.
    """
    body = {"body": {"type": "doc", "version": 1, "content": []}}
    request = urllib.request.Request(
        f"{base}/rest/api/3/issue/ZZZZ-999999/comment",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", header)
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            answer.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None  # refused outright: the credential cannot comment
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return f"could not reach {base}: {type(exc).__name__}"
    return "the comment was accepted"


def main() -> int:
    absent = [name for name in NEEDED if not os.environ.get(name)]
    if absent:
        print("not set: " + ", ".join(absent), file=sys.stderr)
        return 1
    issue = sys.argv[1] if len(sys.argv) > 1 else "DB-8790"

    print(f"reading {issue} from {os.environ['JIRA_SITE']} ...")
    got = _read(issue)
    if got["got"] != "true":
        print(f"  FAILED  {got['why']}", file=sys.stderr)
        return 1
    ticket = json.loads(got["found"])
    print(f"  ok      {ticket['key']}: {ticket['summary'][:70]}")
    print(f"          status={ticket['status']!r} reporter={ticket['reporter']!r}")
    body = ticket["description"]
    print(
        f"          description: {len(body)} characters" if body else "          description: empty"
    )
    for item in ticket.get("attachments") or []:
        read = f"{len(item['text'])} characters read" if item.get("text") else item.get("why", "")
        print(f"          attached: {item['filename']} ({item['type']}) — {read}")

    base, header = _authorization()
    kind = "scoped (gateway, Bearer)" if "api.atlassian.com" in base else "classic (site, Basic)"
    print(f"\nchecking it cannot write ... [{kind}]")
    allowed = _may_write(base, header)
    if allowed is None:
        print("  ok      commenting is refused — this credential only reads")
        return 0
    print(f"  WARN    not refused ({allowed}).", file=sys.stderr)
    print("          The token can write; narrow its scopes, or use an account", file=sys.stderr)
    print("          whose project role has no write permission.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
