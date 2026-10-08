"""Leaving a remark on a named item, and where the credential may go."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from ictus import END, EnvVar, InputPort, Pipeline, PortType, RunSignal, tpl
from ictus.errors import CompositionError
from ictus.graph.stage import Stage
from ictus.integrate import apply_integrations
from ictus.lint import lint_pipeline
from ictus.notify.jira import jira_cloud
from ictus.notify.slack import slack_channel
from ictus.sources import readonly_jira
from ictus.stdlib import MISSING, READ, announce, approval_gate, comment, read_ticket, succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus import Datasource, Integration

SITE = "https://example.atlassian.net"


def _jira() -> Integration:
    return jira_cloud(
        email=EnvVar("JIRA_EMAIL", "the account"),
        token=EnvVar("JIRA_API_TOKEN", "an API token"),
        site=EnvVar("JIRA_SITE", "the site URL"),
    )


def _prelude() -> dict[str, object]:
    """The program's helpers, without the part that would run or send."""
    source = _jira().program
    namespace: dict[str, object] = {}
    exec(source[: source.index("def main(")], namespace)
    return namespace


PRELUDE = _prelude()
ISSUE_OF: Callable[[str, str], tuple[str | None, str]] = PRELUDE["issue_of"]  # type: ignore[assignment]
DOCUMENT: Callable[[str], dict[str, object]] = PRELUDE["document"]  # type: ignore[assignment]


def _blocks(text: str) -> list[dict[str, object]]:
    """The document's top-level blocks, which is all these tests look at."""
    content = DOCUMENT(text)["content"]
    assert isinstance(content, list)
    return content


# --- which item, and whose host ----------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "DB-8790",
        f"{SITE}/browse/DB-8790",
        f"<{SITE}/browse/DB-8790|DB-8790>",
        f"{SITE}/browse/DB-8790?focusedCommentId=1",
        "db-8790",
    ],
)
def test_the_item_is_found_however_it_arrived(target: str) -> None:
    """A key, a link, or the `<url|label>` a Slack message wraps one in."""
    issue, why = ISSUE_OF(target, SITE)
    assert issue == "DB-8790", why


def test_a_link_to_another_host_is_refused_and_nothing_is_sent() -> None:
    """The target comes from a message somebody else wrote, and the request
    carries an API token in a header. Trusting the link's host would make it an
    instruction about where to send that token."""
    issue, why = ISSUE_OF("https://evil.invalid/browse/DB-8790", SITE)
    assert issue is None
    assert "evil.invalid" in why
    assert "nothing was sent" in why


def test_a_slack_wrapped_link_to_another_host_is_refused_too() -> None:
    """The label can say anything; the host is what the credential would go to."""
    issue, why = ISSUE_OF(f"<https://evil.invalid/browse/DB-1|{SITE}/browse/DB-1>", SITE)
    assert issue is None
    assert "evil.invalid" in why


def test_text_with_no_item_in_it_is_refused() -> None:
    issue, why = ISSUE_OF("have a look when you get a minute", SITE)
    assert issue is None
    assert "could not find an issue key" in why


# --- what the remark looks like when it lands --------------------------------


def test_a_fenced_block_stays_a_block() -> None:
    """The usual body here is SQL, and a tracker that ran the paragraphs
    together would make it unreadable at exactly the moment somebody is
    checking it."""
    blocks = _blocks("Here it is:\n\n```sql\nSELECT 1;\nSELECT 2;\n```\n\nThoughts?")
    assert [block["type"] for block in blocks] == ["paragraph", "codeBlock", "paragraph"]
    fenced = blocks[1]["content"]
    assert isinstance(fenced, list)
    assert fenced[0]["text"] == "SELECT 1;\nSELECT 2;"


def test_an_empty_remark_is_still_a_document() -> None:
    """Jira rejects a comment with no body shape at all."""
    assert _blocks("") == [{"type": "paragraph", "content": []}]


# --- the program, end to end, without a network ------------------------------


def _run(target: str, body: str, env: dict[str, str]) -> dict[str, str]:
    done = subprocess.run(
        [sys.executable, "-c", _jira().program, target, "5"],
        input=body,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
        check=False,
    )
    assert done.returncode == 0, "a comment that did not land is not a failed run"
    answer: dict[str, str] = json.loads(done.stdout)
    return answer


def test_a_missing_credential_is_reported_not_raised() -> None:
    answer = _run("DB-1", "hello", {})
    assert answer["posted"] == "false"
    assert "JIRA_EMAIL" in answer["why"]


def test_nothing_to_say_posts_nothing() -> None:
    answer = _run("DB-1", "   ", {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t", "JIRA_SITE": SITE})
    assert answer["posted"] == "false"
    assert "nothing to say" in answer["why"]


def test_a_foreign_host_never_reaches_the_network() -> None:
    """No PATH to a resolver and no network in the test; the refusal has to
    come from the program, before the request is built."""
    answer = _run(
        "https://evil.invalid/browse/DB-1",
        "hello",
        {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t", "JIRA_SITE": SITE},
    )
    assert answer["posted"] == "false"
    assert "evil.invalid" in answer["why"]


# --- the two step kinds cannot be swapped ------------------------------------


def test_a_channel_is_not_commented_on() -> None:
    """Its program is handed a parent and buttons, in those positions. Driven by
    this step it would be handed an item, and post nowhere."""
    channel = slack_channel(token=EnvVar("T", "t"), channel=EnvVar("C", "c"))
    with pytest.raises(CompositionError, match="does not comment on items"):
        comment(node_id="say", on="DB-1", body="hello", to=channel)


def test_a_tracker_is_not_announced_to() -> None:
    with pytest.raises(CompositionError, match="comments on items"):
        announce(node_id="say", text="hello", to=_jira())


def test_the_credential_is_never_written_into_the_program() -> None:
    program = _jira().program
    assert "JIRA_API_TOKEN" in program
    assert "a@b.c" not in program


def test_a_comment_publishes_what_it_did() -> None:
    step = comment(node_id="tell", on="DB-1", body="hello", to=_jira())
    published = {port.name for port in step.declared_outputs}
    assert published == {"issue", "posted", "why"}
    assert step.command == "python3"


# --- a pipeline has to say what it reaches -----------------------------------


def _posting(*, declared: bool) -> Pipeline:
    """A pipeline that comments on a ticket, having said so or not."""
    pipeline = Pipeline(pipeline_id="posts")
    ticket = pipeline.declare_input("ticket", PortType.STRING)
    service = _jira()
    if declared:
        pipeline.integrate(service)
    told = pipeline.add(comment(node_id="told", on=ticket.ref(), body="done", to=service))
    end = pipeline.add(
        succeed(
            node_id="end",
            inputs=(InputPort("posted", PortType.STRING),),
            reason=tpl(told.ref("issue")),
        )
    )
    pipeline.set_entry(told)
    pipeline.connect(told, "posted", end, "posted")
    return pipeline


def test_posting_as_somebody_without_declaring_it_is_refused() -> None:
    """Otherwise a run comments as an account the pipeline never announced, and
    whoever approves it is shown nothing about where it reaches."""
    problems = lint_pipeline(_posting(declared=False))
    assert any("uses 'jira', which this pipeline does not declare" in p for p in problems)


def test_declaring_it_is_what_makes_it_pass() -> None:
    assert not [p for p in lint_pipeline(_posting(declared=True)) if "does not declare" in p]


def test_a_stage_inherits_what_its_caller_announced() -> None:
    """One run, one set of credentials. An integration declared at the top is
    declared for the steps attached inside a stage, which is where
    `apply_integrations` puts them."""
    stage = Stage(stage_id="inner")
    why = stage.body.declare_input("why", PortType.STRING)
    gate = stage.body.add(
        approval_gate(node_id="ok", inputs=(InputPort("why", PortType.STRING),), prompt="Go?")
    )
    done = stage.body.add(succeed(node_id="fin", reason="done"))
    stage.body.set_entry(gate)
    stage.body.connect_input(why, gate, "why")
    stage.body.branch(gate, {"approved": done, "rejected": done})

    parent = Pipeline(pipeline_id="outer")
    reason = parent.declare_input("reason", PortType.STRING)
    parent.integrate(
        slack_channel(
            token=EnvVar("T", "t"), channel=EnvVar("C", "c"), reports=(RunSignal.DECISION_NEEDED,)
        )
    )
    node = stage.instantiate(parent, node_id="inner")
    parent.set_entry(node)
    parent.connect_input(reason, node, "why")
    parent.route(node, END)
    apply_integrations(parent)
    assert not [p for p in lint_pipeline(parent) if "does not declare" in p]


# --- reading a ticket, which is the other direction and another credential ----


def _jira_read() -> Datasource:
    return readonly_jira(
        email=EnvVar("JIRA_READ_EMAIL", "an account that only reads"),
        token=EnvVar("JIRA_READ_TOKEN", "its API token"),
        site=EnvVar("JIRA_SITE", "the site URL"),
    )


def _read_prelude() -> dict[str, object]:
    source = _jira_read().program
    namespace: dict[str, object] = {}
    exec(source[: source.index("def main(")], namespace)
    return namespace


def test_reading_and_commenting_hold_separate_credentials_by_default() -> None:
    """So a pipeline that only needs to read can be given an account that only
    reads, and a reader sees two credentials rather than assuming one account."""
    assert _jira_read().name != _jira().name
    assert "JIRA_READ_TOKEN" in _jira_read().program
    assert "JIRA_READ_TOKEN" not in _jira().program


def test_the_reading_program_promises_read_only_and_has_no_way_to_write() -> None:
    source = _jira_read()
    assert source.read_only
    assert 'method="GET"' in source.program
    assert '"POST"' not in source.program and '"PUT"' not in source.program


def test_a_description_arrives_as_prose_not_a_document_tree() -> None:
    """Atlassian sends a tree of nodes; a step reading it wants text."""
    flatten = _read_prelude()["flatten"]
    document = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Remove the dupes."}]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "SELECT 1"}]},
        ],
    }
    out = flatten(document)  # type: ignore[operator]
    assert "Remove the dupes." in out
    assert "SELECT 1" in out


def test_reading_refuses_a_link_to_another_host_too() -> None:
    """The same reason as commenting: the link usually comes from a message
    somebody else wrote, and the request carries a token."""
    issue, why = _read_prelude()["issue_of"]("https://evil.invalid/browse/DB-1", SITE)  # type: ignore[operator]
    assert issue is None
    assert "evil.invalid" in why


def test_a_missing_credential_is_reported_not_raised_when_reading() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _jira_read().program, "DB-1", "5"],
        capture_output=True,
        text=True,
        input="",
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert done.returncode == 0, "a ticket that could not be read is not a failed run"
    assert json.loads(done.stdout)["got"] == "false"


def test_the_stage_keeps_the_reading_credential_to_itself() -> None:
    """A stage compiles to its own file with its own runtime, so what is
    declared inside it is declared only inside it."""
    stage = read_ticket(stage_id="read", against=_jira_read())
    assert [s.name for s in stage.body.datasources] == ["jira-read"]
    assert set(stage.outcomes) == {READ, MISSING}
    assert {p.name for p in stage.output_ports} >= {"ticket", "why"}


def test_a_ticket_that_could_not_be_read_is_an_outcome_not_a_failure() -> None:
    """A deleted ticket, or a token that expired overnight, is something the
    rest of the run should decide about."""
    stage = read_ticket(against=_jira_read())
    ask = next(n for n in stage.body.nodes if n.node_id == "ask")
    routes = stage.body.outgoing(ask)
    assert [e.describe_target for e in routes] == [READ, MISSING]
    assert routes[1].when is None, "missing is the fall-through, so it cannot be skipped"
