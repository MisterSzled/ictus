"""Reading a Jira issue, and nothing else.

A source rather than an integration, because this is the direction data comes
*from*: the thing it answers with is a ticket, and what asks is a step that
wanted to read one. ``ictus.notify.jira`` is the other direction — leaving a
remark — and the two hold separate credentials on purpose, so a pipeline that
only needs to read can be given an account that only reads.

``read_only`` here is a claim about the program, not about the account. The
program issues one ``GET`` and has no path that writes, so a step built on it
cannot comment, transition or edit however it is driven. What stops a *stolen*
token doing those things is the token's own permissions, which is why the setup
hint asks for an account with read scope and nothing more.

Two credentials exist and they are not interchangeable. A *classic* API token
authenticates as ``Basic email:token`` against the site itself. A *scoped* one
— the kind worth asking for, because its permissions can be narrowed to reading
— is refused there, and goes to Atlassian's gateway instead: ``Bearer token``
against ``api.atlassian.com/ex/jira/<cloud id>``. The program tries the gateway
first and falls back, so whoever issues the credential does not have to tell
anybody which kind it is.

The host is pinned to the configured site either way, for the same reason as
the commenting program: the issue usually arrives in a message somebody else
wrote, so a URL whose host were taken at face value would be an instruction
about where to send a credential. The cloud id is read from the configured
site and nowhere else, so the gateway path is pinned by the same declaration.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from ictus.graph.requirements import Datasource

if TYPE_CHECKING:
    from ictus.graph.requirements import EnvVar

__all__ = ["readonly_jira"]

SETUP_HINT = (
    "Create an API token at id.atlassian.com/manage-profile/security/api-tokens for "
    "an account with read access to the projects this pipeline will look at, then "
    "export the account email, that token, and the site URL "
    "(https://<your-site>.atlassian.net). Read scope is enough: this never writes"
)


def readonly_jira(
    *,
    email: EnvVar,
    token: EnvVar,
    site: EnvVar,
    name: str = "jira-read",
    purpose: str = "Read the ticket a run was asked about",
    setup_hint: str = SETUP_HINT,
) -> Datasource:
    """A Jira Cloud site whose issues can be read and not changed.

    Named apart from the commenting integration by default, so a pipeline that
    declares both says so twice and a reader sees two credentials rather than
    assuming one account does everything.
    """
    return Datasource(
        name=name,
        purpose=purpose,
        env=(email, token, site),
        read_only=True,
        command="python3",
        program=_program(email=email.name, token=token.name, site=site.name),
        setup_hint=setup_hint,
    )


def _program(*, email: str, token: str, site: str) -> str:
    """The reading program, with this source's variable names baked in."""
    return _PROGRAM.substitute(email_name=repr(email), token_name=repr(token), site_name=repr(site))


_PROGRAM = string.Template(
    r"""import base64, json, os, re, sys, urllib.error, urllib.parse, urllib.request

EMAIL_NAME = ${email_name}
TOKEN_NAME = ${token_name}
SITE_NAME = ${site_name}
KEY = re.compile(r"([A-Za-z][A-Za-z0-9_]+-[0-9]+)")
GATEWAY = "https://api.atlassian.com"
WANTED = ("summary,description,status,issuetype,priority,labels,components,"
          "reporter,assignee,attachment")
# Text attachments come back inline; the data a ticket is *about* is often
# the CSV somebody dragged onto it, and a step that cannot see it stops one
# question short of the answer. Anything else is named and left alone.
READABLE = ("text/", "application/json", "application/csv")
INLINE_LIMIT = 100000
HINTS = {
    401: "the email and API token were not accepted - check both, and that the "
         "token has not been revoked",
    403: "the account is not allowed to read that project",
    404: "no such issue, or the account cannot see it",
}


def issue_of(target, site):
    # The issue key in `target`, refusing a URL that names another host. The
    # target usually comes from a message somebody else wrote; trusting its
    # host would turn a link into an instruction about where to send a token.
    target = (target or "").strip().strip("<>")
    if "|" in target:  # a Slack link arrives as <url|label>
        target = target.split("|", 1)[0]
    if "://" in target:
        where = urllib.parse.urlparse(target)
        if where.netloc.lower() != urllib.parse.urlparse(site).netloc.lower():
            return None, (
                "that link points at " + where.netloc + ", and this is configured for "
                + urllib.parse.urlparse(site).netloc + " - nothing was requested"
            )
        target = where.path
    found = KEY.search(target)
    if not found:
        return None, "could not find an issue key in " + repr(target[:120])
    return found.group(1).upper(), ""


def flatten(node):
    # Atlassian document format to plain text. A description arrives as a tree
    # of paragraphs and code blocks, and a step reading it wants prose.
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten(part) for part in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    inner = flatten(node.get("content", []))
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind in ("paragraph", "heading", "listItem", "blockquote"):
        return inner + "\n"
    if kind == "codeBlock":
        return "\n```\n" + inner + "\n```\n"
    return inner


def named(value):
    if isinstance(value, dict):
        return value.get("displayName") or value.get("name") or value.get("value") or ""
    return value if isinstance(value, str) else ""


def ask(url, header, seconds):
    # Returns (status, body-or-text). 0 means it never reached anything.
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if header:
        request.add_header("Authorization", header)
    try:
        with urllib.request.urlopen(request, timeout=seconds) as answer:
            return 200, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return 0, None


def authorize(site, issue, seconds):
    # A scoped token is refused at the site and accepted at the gateway; a
    # classic one is the other way round. Try the gateway first, because a
    # scoped credential is the one worth asking for. Returns the base and the
    # header that worked, so whatever hangs off the issue uses the same.
    where = "/rest/api/3/issue/" + urllib.parse.quote(issue) + "?fields=" + WANTED
    code, tenant = ask(site + "/_edge/tenant_info", None, seconds)
    if code == 200 and isinstance(tenant, dict) and tenant.get("cloudId"):
        # The cloud id came from the configured site, so this is still pinned
        # by the same declaration the URL check uses.
        base = GATEWAY + "/ex/jira/" + str(tenant["cloudId"])
        header = "Bearer " + os.environ[TOKEN_NAME]
        code, body = ask(base + where, header, seconds)
        if code == 200:
            return base, header, 200, body
        if code not in (401, 403):
            return base, header, code, None
    pair = base64.b64encode(
        (os.environ[EMAIL_NAME] + ":" + os.environ[TOKEN_NAME]).encode("utf-8")
    ).decode("ascii")
    header = "Basic " + pair
    code, body = ask(site + where, header, seconds)
    return site, header, code, body


def download(url, header, seconds):
    # Bytes rather than JSON: an attachment is whatever somebody dragged on.
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", header)
    try:
        with urllib.request.urlopen(request, timeout=seconds) as answer:
            return answer.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def attached(fields, header, seconds):
    # Named always, read when it is text and small enough to put in a prompt.
    out = []
    for item in fields.get("attachment") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("mimeType") or "")
        size = item.get("size") or 0
        note = {"filename": item.get("filename") or "", "type": kind, "size": size}
        if kind.startswith(READABLE) and size <= INLINE_LIMIT and item.get("content"):
            raw = download(str(item["content"]), header, seconds)
            if raw is None:
                note["why"] = "could not be downloaded"
            else:
                note["text"] = raw.decode("utf-8", "replace")
        elif not kind.startswith(READABLE):
            note["why"] = "not text, so it is named here and not read"
        elif size > INLINE_LIMIT:
            note["why"] = "larger than " + str(INLINE_LIMIT) + " bytes, so not read"
        out.append(note)
    return out


def main():
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 20
    asked = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or sys.stdin.read().strip()
    email = os.environ.get(EMAIL_NAME, "")
    secret = os.environ.get(TOKEN_NAME, "")
    site = (os.environ.get(SITE_NAME, "") or "").rstrip("/")
    if not (email and secret and site):
        absent = [
            n for n, v in ((EMAIL_NAME, email), (TOKEN_NAME, secret), (SITE_NAME, site)) if not v
        ]
        return fail("$$" + " and $$".join(absent) + " not set, so nothing was read")
    issue, why = issue_of(asked, site)
    if issue is None:
        return fail(why)
    base, header, code, body = authorize(site, issue, seconds)
    if code != 200 or not isinstance(body, dict):
        # The body can repeat the request, and the request carried the header.
        if code == 0:
            return fail("could not reach " + site)
        return fail(HINTS.get(code, "Jira refused it with " + str(code)))
    fields = body.get("fields") or {}
    ticket = {
        "key": issue,
        "summary": fields.get("summary") or "",
        "description": flatten(fields.get("description")).strip(),
        "status": named(fields.get("status")),
        "type": named(fields.get("issuetype")),
        "priority": named(fields.get("priority")),
        "reporter": named(fields.get("reporter")),
        "assignee": named(fields.get("assignee")),
        "labels": fields.get("labels") or [],
        "components": [named(c) for c in (fields.get("components") or [])],
        "attachments": attached(fields, header, seconds),
    }
    print(json.dumps({"found": json.dumps(ticket), "got": "true", "why": ""}))


def fail(why):
    # Never a non-zero exit. A ticket that could not be read is a fact for the
    # next step, not a reason to end a run; the reason goes to stderr, which the
    # dashboard and `ictus trace` both show.
    print(why, file=sys.stderr)
    print(json.dumps({"found": "", "got": "false", "why": why}))


main()
"""
)
