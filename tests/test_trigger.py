"""Starting a run because somebody asked for one in a channel."""

from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path

import pytest

from ictus import (
    EnvVar,
    Executable,
    Integration,
    Pipeline,
    PortType,
    RunSignal,
    WorkflowInput,
)
from ictus.assemble.announcements import OPENER_ID, apply_integrations
from ictus.bridge.slack.listen import events
from ictus.bridge.slack.requests import asked
from ictus.errors import CompositionError
from ictus.interfaces.conductor import conductor
from ictus.interfaces.conductor.control.launch import (
    TYPED_INPUT_FLAG,
    launch_command,
)
from ictus.interfaces.conductor.control.live import LiveRun
from ictus.notify.slack import slack_channel, slack_webhook
from ictus.runs.launch import Asked, start
from ictus.runs.triggers import DEFAULT_PREFIX, Need, Trigger, triggers_in
from ictus.stdlib import approval_gate, succeed

STR = PortType.STRING
TRIGGER = Trigger(
    workflow=Path("demo_work/pipelines/asked/build/asked.yaml"), thread_input="reply_to"
)


def _envelope(text: str, **over: object) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "message",
        "text": text,
        "ts": "1700000000.000100",
        "channel": "C0TEST",
        "user": "U123",
    }
    event.update(over)
    return {
        "envelope_id": "e1",
        "type": "events_api",
        "payload": {"type": "event_callback", "event": event},
    }


# --- recognising the ask -----------------------------------------------------


def test_the_question_is_whatever_followed_the_prefix() -> None:
    (ask,) = asked(_envelope("Start test run: why is the bus failing?"), TRIGGER)
    assert ask.question == "why is the bus failing?"
    assert ask.channel == "C0TEST"
    assert ask.who == "U123"


def test_the_message_s_own_timestamp_is_the_conversation() -> None:
    """Replies hang under the question, which is the whole point."""
    (ask,) = asked(_envelope(f"{DEFAULT_PREFIX} anything"), TRIGGER)
    assert ask.thread == "1700000000.000100"


def test_an_ask_inside_someone_else_s_thread_answers_in_that_thread() -> None:
    """Its own ts, never the parent's: the answer belongs to the question."""
    (ask,) = asked(
        _envelope(f"{DEFAULT_PREFIX} x", ts="1700000009.000999", thread_ts="1700000000.000100"),
        TRIGGER,
    )
    assert ask.thread == "1700000009.000999"


def test_capitalisation_and_spacing_do_not_decide_it() -> None:
    """Somebody typing in a channel is not writing a command line."""
    (ask,) = asked(_envelope("  start TEST run:   spaced  "), TRIGGER)
    assert ask.question == "spaced"


def test_an_ordinary_message_starts_nothing() -> None:
    assert list(asked(_envelope("start test run is a phrase I used"), TRIGGER)) == []
    assert list(asked(_envelope("Start test run:"), TRIGGER)) == []


def test_the_app_s_own_messages_are_ignored() -> None:
    """It reports into the channel it watches; without this it starts itself."""
    assert list(asked(_envelope(f"{DEFAULT_PREFIX} loop", bot_id="B1"), TRIGGER)) == []


def test_edits_joins_and_deletions_are_ignored() -> None:
    """A subtype is the message changing, not somebody asking."""
    for subtype in ("message_changed", "message_deleted", "channel_join", "thread_broadcast"):
        assert list(asked(_envelope(f"{DEFAULT_PREFIX} x", subtype=subtype), TRIGGER)) == []


def test_a_press_is_not_an_ask() -> None:
    envelope: dict[str, object] = {"payload": {"type": "block_actions", "actions": []}}
    assert list(asked(envelope, TRIGGER)) == []


def test_whitespace_in_the_prefix_matches_whitespace_in_the_message() -> None:
    """A long `--prefix` copied out of a wrapped terminal carries the break.

    It escapes to a literal newline no single-line message can match.
    """
    wrapped = Trigger(workflow=Path("x.yaml"), prefix="New DB ticket\n  raised:")
    (ask,) = asked(_envelope("New DB ticket raised: DB-8790"), wrapped)
    assert ask.question == "DB-8790"


def test_the_typist_s_spacing_does_not_decide_it() -> None:
    spaced = Trigger(workflow=Path("x.yaml"), prefix="New  DB   ticket raised:")
    (ask,) = asked(_envelope("New DB ticket raised: DB-8790"), spaced)
    assert ask.question == "DB-8790"


def test_a_prefix_typed_in_bold_still_starts_a_run() -> None:
    """Slack composes for a reader: emphasis is in the text an app receives.

    The leading ``*`` alone defeats a prefix anchored at the start.
    """
    bold = Trigger(workflow=Path("x.yaml"), prefix="New DB ticket raised:")
    (ask,) = asked(
        _envelope(
            "*New DB ticket raised:* <https://example.invalid/browse/DB-8790|DB-8790>"
            " - remove duplicate timesheet records\n*Raised by:* A Person"
        ),
        bold,
    )
    assert ask.question.startswith("<https://example.invalid/browse/DB-8790|DB-8790>")
    assert "A Person" in ask.question, "the whole message is the ticket, not its first line"


def test_a_custom_prefix_is_honoured() -> None:
    mine = Trigger(workflow=Path("x.yaml"), prefix="!run")
    (ask,) = asked(_envelope("!run the thing"), mine)
    assert ask.question == "the thing"
    assert list(asked(_envelope(f"{DEFAULT_PREFIX} x"), mine)) == []


def test_the_listener_yields_asks_alongside_presses() -> None:
    """Both arrive down one socket; neither needs its own connection."""
    (ask,) = events(_envelope(f"{DEFAULT_PREFIX} x"), [TRIGGER])
    assert isinstance(ask, Asked)
    assert list(events(_envelope(f"{DEFAULT_PREFIX} x"))) == [], "no trigger, no asks"


# --- launching ---------------------------------------------------------------


def _ask() -> Asked:
    return Asked(question="why's it failing?", thread="1.5", channel="C", who="U")


def test_a_missing_conductor_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listener is driven by somebody typing; it must not die of this."""

    def _absent() -> str:
        raise FileNotFoundError("'conductor' is not on PATH")

    monkeypatch.setattr("ictus.runs.launch.binary", _absent)
    assert "not on PATH" in start(_ask(), TRIGGER).why


def test_a_refusal_comes_back_as_its_last_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight has already said which variable is missing; do not bury it."""

    def _refuse(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 1, "", "lint said no\nerror: $SLACK_BOT_TOKEN is not set"
        )

    monkeypatch.setattr(subprocess, "run", _refuse)
    assert start(_ask(), TRIGGER).why == "error: $SLACK_BOT_TOKEN is not set"


def _handed_over(command: list[str]) -> dict[str, object]:
    """The inputs in an argv, whichever flag carried them.

    By meaning rather than spelling: which flag is right depends on whether the
    engine would retype the value, and a test that pins the flag would have to
    change every time that answer does.
    """
    found: dict[str, object] = {}
    for flag, pair in itertools.pairwise(command):
        name, sep, value = pair.partition("=")
        if not sep:
            continue
        if flag == "-i":
            found[name] = value
        elif flag == TYPED_INPUT_FLAG:
            found[name] = json.loads(value)
    return found


def _ran(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the argv `start` would have run, running nothing."""
    seen: list[list[str]] = []

    def _record(command: list[str], **__: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _record)
    return seen


def test_the_question_and_the_thread_are_passed_as_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through argv, so an apostrophe in a question is only an apostrophe."""
    seen = _ran(monkeypatch)
    assert start(_ask(), TRIGGER).ok
    assert _handed_over(seen[0]) == {"question": "why's it failing?", "reply_to": "1.5"}


def test_a_conversation_reaches_the_run_as_the_text_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`conductor run -i` guesses a type, and a Slack ts is a string that looks
    like a number. Coerced, `1700000000.000200` comes back `1700000000.0002` —
    which matches no message, so a run's reports land at the top of the channel
    and the sending program blames a deleted message. One ts in ten ends in a
    zero.
    """
    seen = _ran(monkeypatch)
    asked = Asked(question="why's it failing?", thread="1700000000.000200", channel="C", who="U")
    assert start(asked, TRIGGER).ok
    handed = _handed_over(seen[0])
    assert handed["reply_to"] == "1700000000.000200"
    assert isinstance(handed["reply_to"], str), "a timestamp is not a number"


def test_a_pipeline_that_reports_nowhere_is_handed_no_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest names the thread input exactly when one was declared, so a
    default here would hand every run a value under a name it never chose."""
    seen: list[list[str]] = []

    def _record(command: list[str], **__: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _record)
    quiet = Trigger(workflow=Path("demo_work/pipelines/asked/build/asked.yaml"))
    assert quiet.thread_input == "", "nothing was declared, so nothing is named"
    assert start(_ask(), quiet).ok
    assert _handed_over(seen[0]) == {"question": "why's it failing?"}


# --- a run that reports into somebody else's conversation --------------------


def _pipeline(*, named: str = "reply_to") -> tuple[Pipeline, WorkflowInput, Integration]:
    p = Pipeline(pipeline_id="asked")
    where = p.declare_input(named, STR, required=False)
    service = slack_channel(
        token=EnvVar("T", "t"),
        channel=EnvVar("C", "c"),
        reports=(RunSignal.DECISION_NEEDED,),
    )
    gate = p.add(approval_gate(node_id="go_ahead", prompt="Go?"))
    done = p.add(succeed(node_id="done", reason="done"))
    p.set_entry(gate)
    p.branch(gate, {"approved": done, "rejected": done})
    return p, where, service


def test_a_given_thread_means_no_opener_is_added() -> None:
    """The conversation was open before the run started."""
    p, where, service = _pipeline()
    p.integrate(service, thread=where)
    apply_integrations(p)
    assert not any(n.node_id == OPENER_ID for n in p.nodes)
    assert any(n.node_id.startswith("report_") for n in p.nodes)


def test_without_one_a_run_opens_its_own() -> None:
    p, _, service = _pipeline()
    p.integrate(service)
    apply_integrations(p)
    assert any(n.node_id == OPENER_ID for n in p.nodes)


def test_the_thread_input_must_be_declared_by_the_pipeline() -> None:
    p, _, service = _pipeline()
    other = Pipeline(pipeline_id="elsewhere")
    stray = other.declare_input("reply_to", STR, required=False)
    with pytest.raises(CompositionError, match="does not declare as an input"):
        p.integrate(service, thread=stray)


def test_a_thread_input_must_be_a_string() -> None:
    p = Pipeline(pipeline_id="asked")
    wrong = p.declare_input("reply_to", PortType.NUMBER, required=False)
    service = slack_channel(token=EnvVar("T", "t"), channel=EnvVar("C", "c"))
    with pytest.raises(CompositionError, match="addressed by a string"):
        p.integrate(service, thread=wrong)


def test_an_input_called_thread_collides_with_what_announcements_publish() -> None:
    """A node's ports share one namespace, so the two cannot both be `thread`."""
    p, _, service = _pipeline(named="thread")
    p.integrate(service, thread=p.workflow_inputs[0])
    with pytest.raises(CompositionError, match="share one namespace"):
        apply_integrations(p)


def test_the_dashboard_comes_from_the_run_s_own_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher prints it only to a terminal, so a subprocess sees nothing."""
    run = LiveRun(run_id="new1", workflow="asked", port=5123, pid=1, started_at="2026")
    calls = iter([[], [run]])

    monkeypatch.setattr("ictus.runs.launch.live_runs", lambda *_, **__: next(calls))
    monkeypatch.setattr(
        subprocess, "run", lambda c, **__: subprocess.CompletedProcess(c, 0, "", "")
    )
    started = start(_ask(), TRIGGER)
    assert started.dashboard == "http://127.0.0.1:5123"
    assert started.run_id == "new1"


def test_two_launches_at_once_report_no_address_rather_than_the_wrong_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody else's dashboard is a worse answer than none."""
    pair = [
        LiveRun(run_id="a", workflow="asked", port=1, pid=1, started_at="2026"),
        LiveRun(run_id="b", workflow="asked", port=2, pid=2, started_at="2026"),
    ]
    calls = iter([[], pair])
    monkeypatch.setattr("ictus.runs.launch.live_runs", lambda *_, **__: next(calls))
    monkeypatch.setattr(
        subprocess, "run", lambda c, **__: subprocess.CompletedProcess(c, 0, "", "")
    )
    started = start(_ask(), TRIGGER)
    assert started.ok
    assert started.dashboard == ""


# --- declaring what starts a pipeline ----------------------------------------


def _listening() -> tuple[Pipeline, WorkflowInput, Integration]:
    """A pipeline that reports to a service and can be started from it."""
    p = Pipeline(pipeline_id="asked")
    ticket = p.declare_input("ticket", STR)
    reply_to = p.declare_input("reply_to", STR, required=False)
    service = slack_channel(
        token=EnvVar("SLACK_BOT_TOKEN", "a bot token"),
        channel=EnvVar("SLACK_CHANNEL", "the channel id"),
        reports=(RunSignal.DECISION_NEEDED,),
    )
    p.integrate(service, thread=reply_to)
    gate = p.add(approval_gate(node_id="go_ahead", prompt="Go?"))
    done = p.add(succeed(node_id="done", reason="done"))
    p.set_entry(gate)
    p.branch(gate, {"approved": done, "rejected": done})
    return p, ticket, service


def test_the_conversation_comes_from_the_integration_not_a_second_argument() -> None:
    """Named twice, the two could disagree about where a run answers."""
    p, ticket, service = _listening()
    listener = p.listen_on(service, prefix="New DB ticket raised:", into=ticket)
    assert listener.into.name == "ticket"
    assert listener.thread is not None, "integrate() already said where it reports"
    assert listener.thread.name == "reply_to"


def test_a_pipeline_can_be_startable_without_holding_a_credential() -> None:
    """Being startable is not a reason to hold one.

    The listener reads the channel with its own credential; a run that says
    nothing there needs none. Naming a service in order to be started was how a
    pipeline came to declare a token it never used — which `trigger.missing()`
    then refused to launch without.
    """
    p = Pipeline(pipeline_id="asked")
    question = p.declare_input("question", STR)
    listener = p.listen_on(prefix="Alert:", into=question)
    assert listener.service is None
    assert listener.thread is None, "it reports nowhere, so there is nothing to answer under"
    assert p.integrations == (), "and nothing to configure"


def test_a_credential_free_listener_asks_the_environment_for_nothing(tmp_path: Path) -> None:
    """The whole point, read back off the artifact a listener actually reads."""
    p = Pipeline(pipeline_id="asked")
    question = p.declare_input("question", STR)
    p.set_entry(p.add(succeed(node_id="done", reason="done")))
    p.listen_on(prefix="Alert:", into=question)
    built = _built(tmp_path, p)

    document = json.loads((built / "asked.listen.json").read_text(encoding="utf-8"))
    assert document["requires"]["env"] == []
    assert document["listeners"] == [
        {"service": "", "prefix": "Alert:", "inputs": {"question": "question"}}
    ]

    (trigger,) = triggers_in(built)
    assert trigger.missing() == [], "nothing to go and set before it may run"
    assert trigger.thread_input == "", "it reports nowhere, so there is nothing to answer under"


def test_a_second_serviceless_listener_is_refused() -> None:
    """One prefix starts it, the same rule one service has always had."""
    p = Pipeline(pipeline_id="asked")
    question = p.declare_input("question", STR)
    spare = p.declare_input("spare", STR)
    p.listen_on(prefix="Alert:", into=question)
    with pytest.raises(CompositionError, match="already listens for a message"):
        p.listen_on(prefix="Warning:", into=spare)


def test_naming_a_service_still_takes_its_conversation_from_the_integration() -> None:
    """The optional argument changes nothing for a pipeline that does report."""
    p, ticket, service = _listening()
    listener = p.listen_on(service, prefix="New DB ticket raised:", into=ticket)
    assert listener.service is service
    assert listener.thread is not None and listener.thread.name == "reply_to"


def test_listening_on_a_service_that_is_not_integrated_is_refused() -> None:
    """One declaration says both, so a run cannot answer somewhere it never said."""
    _, _, service = _listening()
    other = Pipeline(pipeline_id="elsewhere")
    spare = other.declare_input("ticket", STR)
    with pytest.raises(CompositionError, match="without integrating it"):
        other.listen_on(service, prefix="x:", into=spare)


def test_a_write_only_destination_cannot_be_listened_on() -> None:
    """A webhook is an address to post to: nothing to hold open, nothing to read."""
    p = Pipeline(pipeline_id="asked")
    ticket = p.declare_input("ticket", STR)
    hook = slack_webhook(url=EnvVar("SLACK_WEBHOOK_URL", "the webhook"))
    p.integrate(hook)
    with pytest.raises(CompositionError, match="cannot be listened on"):
        p.listen_on(hook, prefix="x:", into=ticket)


def test_a_blank_prefix_is_refused_because_every_message_matches_it() -> None:
    p, ticket, service = _listening()
    with pytest.raises(CompositionError, match="blank prefix"):
        p.listen_on(service, prefix="   ", into=ticket)


def test_the_input_it_listens_into_must_be_one_the_pipeline_declares() -> None:
    """The failure this replaces arrived in Slack, as `are not inputs of`."""
    p, _, service = _listening()
    other = Pipeline(pipeline_id="elsewhere")
    stray = other.declare_input("ticket", STR)
    with pytest.raises(CompositionError, match="does not declare as an input"):
        p.listen_on(service, prefix="x:", into=stray)


def test_what_somebody_types_is_a_string() -> None:
    p, _, service = _listening()
    number = p.declare_input("count", PortType.NUMBER)
    with pytest.raises(CompositionError, match="is a string"):
        p.listen_on(service, prefix="x:", into=number)


def test_the_question_and_the_conversation_cannot_share_an_input() -> None:
    p = Pipeline(pipeline_id="asked")
    both = p.declare_input("reply_to", STR, required=False)
    service = slack_channel(token=EnvVar("T", "t"), channel=EnvVar("C", "c"))
    p.integrate(service, thread=both)
    with pytest.raises(CompositionError, match="two values"):
        p.listen_on(service, prefix="x:", into=both)


def test_one_service_starts_a_pipeline_one_way() -> None:
    p, ticket, service = _listening()
    p.listen_on(service, prefix="x:", into=ticket)
    with pytest.raises(CompositionError, match="already listens"):
        p.listen_on(service, prefix="y:", into=ticket)


# --- the manifest, and reading it back ----------------------------------------


def _built(tmp_path: Path, pipeline: Pipeline) -> Path:
    """Compile ``pipeline`` into ``tmp_path`` the way `ictus emit` would."""
    for document in conductor.compile(pipeline):
        (tmp_path / document.filename).write_text(document.content, encoding="utf-8")
    return tmp_path


def test_a_pipeline_that_listens_compiles_a_manifest_beside_its_workflow(
    tmp_path: Path,
) -> None:
    p, ticket, service = _listening()
    p.listen_on(service, prefix="New DB ticket raised:", into=ticket)
    built = _built(tmp_path, p)
    (trigger,) = triggers_in(built)
    assert trigger.workflow == built / "asked.yaml"
    assert trigger.prefix == "New DB ticket raised:"
    assert trigger.question_input == "ticket"
    assert trigger.thread_input == "reply_to"
    assert trigger.pipeline == "asked"


def test_a_pipeline_nothing_starts_compiles_no_manifest(tmp_path: Path) -> None:
    """Only `listen_on` makes one; a workflow is not startable by accident."""
    p, _, _ = _listening()
    assert triggers_in(_built(tmp_path, p)) == []


def test_the_manifest_carries_what_preflight_would_have_checked(tmp_path: Path) -> None:
    """Preflight is a command, not an artifact. Ship only the workflow and a
    missing credential stops being a refusal and becomes a mid-run failure."""
    p, ticket, service = _listening()
    p.require_executable(Executable(name="python3", purpose="Sends the reports"))
    p.listen_on(service, prefix="x:", into=ticket)
    (trigger,) = triggers_in(_built(tmp_path, p))
    assert Need("python3", "Sends the reports") in trigger.commands
    assert {need.name for need in trigger.env} == {"SLACK_BOT_TOKEN", "SLACK_CHANNEL"}


def test_a_credential_the_environment_lacks_is_named_before_anything_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    trigger = Trigger(
        workflow=Path("x.yaml"),
        env=(Need("SLACK_BOT_TOKEN", "a bot token"),),
        commands=(Need("definitely-not-a-real-command", "nothing"),),
    )
    gaps = trigger.missing()
    assert any("$SLACK_BOT_TOKEN is not set" in gap for gap in gaps)
    assert any("definitely-not-a-real-command is not on PATH" in gap for gap in gaps)
    assert "a bot token" in " ".join(gaps), "say what it is for, not just that it is absent"


def test_a_missing_credential_stops_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the subprocess: the whole worth of this check is being free."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    def _never(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("nothing should have been launched")

    monkeypatch.setattr(subprocess, "run", _never)
    trigger = Trigger(workflow=Path("x.yaml"), env=(Need("SLACK_BOT_TOKEN", "a bot token"),))
    assert "$SLACK_BOT_TOKEN is not set" in start(_ask(), trigger).why


def test_a_manifest_from_a_later_ictus_is_skipped_not_guessed_at(tmp_path: Path) -> None:
    """A listener serves many pipelines; one it cannot read is not all of them."""
    (tmp_path / "asked.yaml").write_text("workflow: {}", encoding="utf-8")
    (tmp_path / "asked.listen.json").write_text(
        json.dumps({"manifest": 99, "workflow": "asked.yaml", "listeners": []}), encoding="utf-8"
    )
    assert triggers_in(tmp_path) == []


def test_a_manifest_whose_workflow_is_gone_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "asked.listen.json").write_text(
        json.dumps(
            {
                "manifest": 1,
                "workflow": "asked.yaml",
                "listeners": [{"prefix": "x:", "inputs": {"question": "q"}}],
            }
        ),
        encoding="utf-8",
    )
    assert triggers_in(tmp_path) == []


def test_one_message_starts_one_run(tmp_path: Path) -> None:
    """Two pipelines sharing a prefix is a mistake; starting both charges for it."""
    first = Trigger(workflow=tmp_path / "a.yaml", prefix="go:")
    second = Trigger(workflow=tmp_path / "b.yaml", prefix="go:")
    found = list(events(_envelope("go: the thing"), [first, second]))
    assert len(found) == 1
    assert isinstance(found[0], Asked)
    assert found[0].trigger is first, "the order manifests were found in, which is sorted"


# --- a run is handed what it declared, and nothing else -----------------------


def test_a_pipeline_can_refuse_what_its_directory_says_about_itself() -> None:
    """`--workspace-instructions` walks to the git root and prepends AGENTS.md
    to every prompt. Right for a pipeline that works on a repository, wrong
    for one working on a tracker ticket."""
    pipeline = Pipeline(pipeline_id="asks")
    question = pipeline.declare_input("question", STR)
    service = slack_channel(token=EnvVar("T", "t"), channel=EnvVar("C", "c"))
    pipeline.integrate(service)
    pipeline.listen_on(service, prefix="go:", into=question)
    pipeline.add(succeed(node_id="done", reason="done"))
    pipeline.set_entry(pipeline.nodes[0])

    pipeline.workspace_instructions = False
    rendered = next(d for d in conductor.compile(pipeline) if d.filename.endswith(".listen.json"))
    assert json.loads(rendered.content)["workspace_instructions"] is False


def test_the_flag_reaches_the_launch(tmp_path: Path) -> None:
    """Carried in the manifest because the process that starts a run is the one
    that has to pass it."""
    (tmp_path / "asks.yaml").write_text("workflow: {}", encoding="utf-8")
    (tmp_path / "asks.listen.json").write_text(
        json.dumps(
            {
                "manifest": 1,
                "workflow": "asks.yaml",
                "workspace_instructions": False,
                "listeners": [{"prefix": "go:", "inputs": {"question": "q"}}],
            }
        ),
        encoding="utf-8",
    )
    (trigger,) = triggers_in(tmp_path)
    assert trigger.workspace_instructions is False
    argv = launch_command(
        "conductor",
        trigger.workflow,
        inputs={},
        dashboard=True,
        background=True,
        workspace_instructions=trigger.workspace_instructions,
    )
    assert "--workspace-instructions" not in argv


def test_a_manifest_that_says_nothing_still_gets_it() -> None:
    """On by default: a step otherwise arrives knowing nothing a project says
    about how it wants to be worked in."""
    assert Trigger(workflow=Path("x.yaml")).workspace_instructions is True
