"""Integrations: the air gap, what a report says, and what it must never leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The stand-in Slack lives in conftest.py: two modules drive it.
from conftest import SECRET

from ictus import EnvVar, Integration, RunSignal
from ictus.errors import CompositionError
from ictus.graph.node import NodeKind
from ictus.interfaces import SignalEvent
from ictus.notify import Delivered, deliver, send, summarise
from ictus.notify.slack import slack_channel, slack_webhook
from ictus.stdlib import announce, approval_gate
from ictus.stdlib.steps.announce import THREAD_PORT

SRC = Path(__file__).resolve().parent.parent / "src" / "ictus"


def _event(signal: RunSignal = RunSignal.DECISION_NEEDED, **facts: object) -> SignalEvent:
    return SignalEvent(
        signal=signal,
        run_id="bd13f80e",
        workflow="smoke-events",
        at=1791192899.88,
        event_type="gate_presented",
        **facts,  # type: ignore[arg-type]
    )


def _hook(**over: object) -> Integration:
    fields: dict[str, object] = {
        "url": EnvVar("HOOK_URL", "where to post"),
        "reports": (RunSignal.DECISION_NEEDED,),
    }
    fields.update(over)
    return slack_webhook(**fields)  # type: ignore[arg-type]


def _channel(**over: object) -> Integration:
    fields: dict[str, object] = {
        "token": EnvVar("TEST_TOKEN", "a bot token"),
        "channel": EnvVar("TEST_CHANNEL", "a channel id"),
    }
    fields.update(over)
    return slack_channel(**fields)  # type: ignore[arg-type]


# --- the air gap -------------------------------------------------------------

# Which packages may name a service, and the one-way edge to the bridge, are
# in `test_boundaries.py`.


def test_the_graph_layer_carries_a_program_it_never_reads() -> None:
    """How the air gap is possible: the sending program is opaque data."""
    service = _channel()
    assert service.program
    assert service.command == "python3"


# --- declaring one -----------------------------------------------------------


def test_an_integration_with_no_program_could_never_send_anything() -> None:
    with pytest.raises(CompositionError, match="no program"):
        Integration(name="x", purpose="y")


def test_an_integration_needs_a_purpose_like_every_other_requirement() -> None:
    with pytest.raises(CompositionError, match="needs a purpose"):
        _channel(purpose="")


def test_a_repeated_signal_is_refused() -> None:
    with pytest.raises(CompositionError, match="more than once"):
        _channel(reports=(RunSignal.RUN_FAILED, RunSignal.RUN_FAILED))


def test_a_webhook_cannot_thread_and_says_so() -> None:
    """It never learns where its message landed, so there is no parent."""
    assert not _hook().threads
    assert _channel().threads


# --- what a report says ------------------------------------------------------


def test_a_decision_says_what_it_is_waiting_for() -> None:
    text = summarise(
        _event(step="confirm_start", options=("start", "cancel"), prompt="Start it?\n\nx")
    )
    assert "smoke-events" in text
    assert "needs a decision" in text
    assert "confirm_start" in text
    assert "`start`, `cancel`" in text
    assert "Start it?" in text
    assert "bd13f80e" in text


def test_a_long_prompt_is_cut_to_its_opening() -> None:
    assert len(summarise(_event(prompt="x" * 500))) < 400


def test_an_answer_carries_the_choice_and_the_note() -> None:
    text = summarise(
        _event(RunSignal.DECISION_MADE, choice="rejected", notes=(("notes", "not this time"),))
    )
    assert "`rejected`" in text
    assert "not this time" in text


def test_the_dashboard_link_is_omitted_rather_than_guessed() -> None:
    assert "http" not in summarise(_event())
    assert "http://127.0.0.1:1" in summarise(_event(), dashboard="http://127.0.0.1:1")


def test_a_summary_names_no_service() -> None:
    """Each program wraps it; the words are the same wherever they land."""
    for signal in RunSignal:
        assert "slack" not in summarise(_event(signal)).lower()


# --- a step that reports -----------------------------------------------------


def test_an_announce_node_is_a_script_step_not_a_model_call() -> None:
    node = announce(node_id="tell", text="hello", to=_channel())
    assert node.kind is NodeKind.SUBPROCESS


def test_no_credential_is_in_what_is_emitted() -> None:
    """Slack's own endpoint is public and fine; a token is the authorisation."""
    rendered = " ".join(str(a) for a in announce(node_id="t", text="x", to=_channel()).args)
    assert "TEST_TOKEN" in rendered
    assert "TEST_CHANNEL" in rendered
    assert "xoxb-" not in rendered
    assert "hooks.slack.com" not in rendered


def test_the_message_is_piped_rather_than_put_on_the_command_line() -> None:
    """A gate prompt is prose; argv would break on the first apostrophe."""
    node = announce(node_id="t", text='it\'s "quoted"', to=_channel())
    assert node.stdin == 'it\'s "quoted"'


def test_the_thread_is_published_as_a_typed_port() -> None:
    ports = [p.name for p in announce(node_id="t", text="x", to=_channel()).outputs]
    assert ports == [THREAD_PORT, "posted"]


def test_replying_under_a_message_needs_a_service_that_threads() -> None:
    opener = announce(node_id="open", text="x", to=_channel())
    with pytest.raises(CompositionError, match="has no threads"):
        announce(node_id="reply", text="y", to=_hook(), thread=opener.ref(THREAD_PORT))


def test_a_reply_declares_the_input_it_needs_without_being_asked() -> None:
    opener = announce(node_id="open", text="x", to=_channel())
    reply = announce(node_id="r", text="y", to=_channel(), thread=opener.ref(THREAD_PORT))
    assert [p.name for p in reply.inputs] == ["open"]


def test_buttons_are_read_off_the_gate_they_answer() -> None:
    gate = approval_gate(node_id="ship_it", prompt="Deploy?")
    node = announce(node_id="ask", text="?", to=_channel(), answers=gate)
    spec = json.loads(str(node.args[3]))
    assert spec["gate"] == "ship_it"
    assert spec["step"] == "ask", "a press finds its run through the step that posted it"
    assert spec["buttons"] == [
        ["approved", "Approve", "", False],
        ["rejected", "Reject", "notes", True],
    ]


def test_buttons_need_a_service_that_can_carry_an_answer_back() -> None:
    gate = approval_gate(node_id="ship_it", prompt="Deploy?")
    with pytest.raises(CompositionError, match="cannot carry an answer back"):
        announce(node_id="ask", text="?", to=_hook(), answers=gate)


def test_the_program_is_valid_python() -> None:
    """It is a string in a YAML file, so nothing else would check it."""
    compile(_channel().program, "<integration>", "exec")
    compile(_hook().program, "<integration>", "exec")


# --- the watcher's half ------------------------------------------------------


def test_the_watcher_sends_through_the_same_program(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """One way to send, so a report cannot drift between the two callers."""
    url, received = collector
    (result,) = deliver(_event(), [_hook()], env={"HOOK_URL": url})
    assert result == Delivered("slack", "decision_needed", sent=True)
    assert "needs a decision" in str(received[0][1]["text"])


def test_a_moment_a_step_already_announced_is_not_delivered_again(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """It would arrive twice, the second time outside the thread and without buttons."""
    url, received = collector
    assert deliver(_event(at_a_step=True), [_hook()], env={"HOOK_URL": url}) == []
    assert received == []


def test_what_happened_before_anybody_was_watching_is_not_delivered(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """A watcher restarted mid-run reads the history; it is not news."""
    url, received = collector
    assert deliver(_event(replayed=True), [_hook()], env={"HOOK_URL": url}) == []
    assert received == []


def test_a_report_from_outside_lands_in_the_run_s_thread(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, received = collector
    failed = _event(RunSignal.STEP_FAILED, reason="lint failed")
    (result,) = deliver(
        failed,
        [_channel(reports=(RunSignal.STEP_FAILED,))],
        threads={"slack": "1700000000.000001"},
        env={"TEST_TOKEN": "xoxb-pretend", "TEST_CHANNEL": "C0TEST", "SLACK_API_URL": url},
    )
    assert result.sent, result.detail
    assert received[0][1]["thread_ts"] == "1700000000.000001"


def test_only_subscribers_to_that_signal_are_told(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, received = collector
    wants = _hook(name="wants", reports=(RunSignal.DECISION_NEEDED,))
    ignores = _hook(name="ignores", reports=(RunSignal.RUN_FAILED,))
    results = deliver(_event(), [wants, ignores], env={"HOOK_URL": url})
    assert [r.integration for r in results] == ["wants"]
    assert len(received) == 1


def test_a_failure_is_reported_with_the_program_s_own_reason() -> None:
    """It knows what the service said and what to do about it."""
    (result,) = deliver(_event(), [_hook()], env={"HOOK_URL": ""})
    assert not result.sent
    assert "HOOK_URL" in result.detail


def test_one_broken_integration_does_not_stop_the_others(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, received = collector
    broken = _hook(name="broken", url=EnvVar("MISSING_URL", "unset"))
    working = _hook(name="working")
    results = deliver(_event(), [broken, working], env={"HOOK_URL": url, "MISSING_URL": ""})
    assert [r.sent for r in results] == [False, True]
    assert len(received) == 1


def test_a_missing_interpreter_is_reported_not_raised() -> None:
    absent = Integration(
        name="absent",
        purpose="a service whose sender is not installed",
        reports=(RunSignal.DECISION_NEEDED,),
        command="definitely-not-a-command",
        program="pass",
    )
    (result,) = deliver(_event(), [absent], env={})
    assert not result.sent
    assert "not on PATH" in result.detail


@pytest.mark.parametrize("url", ["http://127.0.0.1:1" + SECRET, "hooks.slack.com" + SECRET])
def test_send_never_puts_a_credential_in_its_reason(url: str) -> None:
    why = send(_hook(), "x", env={"HOOK_URL": url})
    assert why
    assert SECRET not in why
