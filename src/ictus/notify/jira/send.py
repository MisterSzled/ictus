"""Adding a comment to a Jira issue.

The program is standard library only, like every program emitted here, and is
handed the issue by the graph rather than configured with one: a run started by
a ticket comments on that ticket.

**The host is pinned to the configured site, and that is not a detail.** The
issue usually arrives in a message somebody else wrote, so it is untrusted
input, and the program authenticates with Basic auth — an email and an API
token in a header. A target URL whose host were taken at face value would be an
instruction to send that credential wherever the URL said. So the site is
declared with the credential, and a URL naming anywhere else is refused before
anything is sent.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from ictus.graph.requirements import Integration

if TYPE_CHECKING:
    from ictus.graph.requirements import EnvVar

__all__ = ["jira_cloud"]

SETUP_HINT = (
    "Create an API token at id.atlassian.com/manage-profile/security/api-tokens, "
    "then export the account email, that token, and the site URL "
    "(https://<your-site>.atlassian.net). The account needs permission to comment "
    "on the projects the pipeline will reach"
)


def jira_cloud(
    *,
    email: EnvVar,
    token: EnvVar,
    site: EnvVar,
    name: str = "jira",
    purpose: str = "Add what this run worked out to the ticket that asked for it",
    setup_hint: str = SETUP_HINT,
) -> Integration:
    """A Jira Cloud site whose issues can be commented on.

    ``site`` is both where comments go and the only host the program will send
    a credential to. Declaring it beside the token rather than reading it off
    whatever URL arrived is what keeps an issue link from being an instruction.
    """
    return Integration(
        name=name,
        purpose=purpose,
        env=(email, token, site),
        command="python3",
        program=_program(email=email.name, token=token.name, site=site.name),
        announces=False,
        comments=True,
        setup_hint=setup_hint,
    )


def _program(*, email: str, token: str, site: str) -> str:
    """The commenting program, with this integration's variable names baked in."""
    return _PROGRAM.substitute(email_name=repr(email), token_name=repr(token), site_name=repr(site))


_PROGRAM = string.Template(
    r"""import base64, json, os, re, sys, urllib.error, urllib.parse, urllib.request

EMAIL_NAME = ${email_name}
TOKEN_NAME = ${token_name}
SITE_NAME = ${site_name}
KEY = re.compile(r"([A-Za-z][A-Za-z0-9_]+-[0-9]+)")
FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)
HINTS = {
    401: "the email and API token were not accepted - check both, and that the "
         "token has not been revoked",
    403: "the account is not allowed to comment on that project",
    404: "no such issue, or the account cannot see it",
}


def issue_of(target, site):
    # The issue key in `target`, refusing a URL that names another host. The
    # target usually comes from a message somebody else wrote; trusting its
    # host would turn an issue link into an instruction about where to send a
    # credential.
    target = (target or "").strip().strip("<>")
    if "|" in target:  # a Slack link arrives as <url|label>
        target = target.split("|", 1)[0]
    if "://" in target:
        where = urllib.parse.urlparse(target)
        if where.netloc.lower() != urllib.parse.urlparse(site).netloc.lower():
            return None, (
                "that link points at " + where.netloc + ", and this is configured for "
                + urllib.parse.urlparse(site).netloc + " - nothing was sent"
            )
        target = where.path
    found = KEY.search(target)
    if not found:
        return None, "could not find an issue key in " + repr(target[:120])
    return found.group(1).upper(), ""


def document(text):
    # The comment as Atlassian document format, keeping fenced code fenced.
    content = []
    last = 0
    for block in FENCE.finditer(text):
        content.extend(paragraphs(text[last : block.start()]))
        body = block.group(1).rstrip("\n")
        if body:
            content.append({"type": "codeBlock", "content": [{"type": "text", "text": body}]})
        last = block.end()
    content.extend(paragraphs(text[last:]))
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}


def paragraphs(text):
    out = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip("\n")
        if chunk.strip():
            out.append({"type": "paragraph", "content": [{"type": "text", "text": chunk}]})
    return out


def main():
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 20
    body = sys.stdin.read().strip()
    email = os.environ.get(EMAIL_NAME, "")
    secret = os.environ.get(TOKEN_NAME, "")
    site = (os.environ.get(SITE_NAME, "") or "").rstrip("/")
    if not (email and secret and site):
        absent = [
            n for n, v in ((EMAIL_NAME, email), (TOKEN_NAME, secret), (SITE_NAME, site)) if not v
        ]
        return fail("", "$$" + " and $$".join(absent) + " not set, so nothing was sent")
    if not body:
        return fail("", "there was nothing to say, so nothing was sent")
    issue, why = issue_of(sys.argv[1] if len(sys.argv) > 1 else "", site)
    if issue is None:
        return fail("", why)
    request = urllib.request.Request(
        site + "/rest/api/3/issue/" + urllib.parse.quote(issue) + "/comment",
        data=json.dumps({"body": document(body)}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    pair = base64.b64encode((email + ":" + secret).encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + pair)
    try:
        with urllib.request.urlopen(request, timeout=seconds) as answer:
            answer.read()
    except urllib.error.HTTPError as exc:
        # The body can repeat the request, and the request carried the header.
        return fail(issue, HINTS.get(exc.code, "Jira refused it with " + str(exc.code)))
    except (urllib.error.URLError, OSError) as exc:
        return fail(issue, "could not reach " + site + ": " + type(exc).__name__)
    print(json.dumps({"issue": issue, "posted": "true", "why": ""}))


def fail(issue, why):
    # Never a non-zero exit. A comment that did not land is a fact for the next
    # step, not a reason to end a run whose work succeeded; the reason goes to
    # stderr, which the dashboard and `ictus trace` both show.
    print(why, file=sys.stderr)
    print(json.dumps({"issue": issue, "posted": "false", "why": why}))


main()
"""
)
