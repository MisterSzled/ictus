"""The sending program, driven against a real HTTP server.

``notify/slack/program.py`` is not Python this project runs: it is a string
substituted into an ``Integration`` and executed by the engine as a subprocess,
with no ictus on its path. So it is tested the way the engine runs it — started
as a process, pointed at a stand-in Slack, and asked what it posted.

Split out of ``test_notify.py`` when the module it mirrors was split out of
``send.py``. What stayed there is what a pipeline *declares* and what the
watcher delivers; this is the program itself.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

import pytest

# The stand-in Slack lives in conftest.py: two modules drive it.
from conftest import SECRET, _Collector

from ictus import EnvVar, Integration, RunSignal
from ictus.graph.composition import END
from ictus.graph.pipeline import Pipeline
from ictus.interfaces.conductor import ConductorBackend
from ictus.notify.slack import slack_channel, slack_webhook
from ictus.stdlib import announce, approval_gate


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


# --- a server that records what arrives --------------------------------------


def _run(
    service: Integration, text: str, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        [sys.executable, "-c", service.program, *args],
        input=text,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def test_a_webhook_program_posts_what_it_is_given(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, received = collector
    done = _run(_hook(), 'it\'s "quoted"', HOOK_URL=url)
    assert done.returncode == 0, done.stderr
    assert received[0][1] == {"text": 'it\'s "quoted"'}


def test_a_channel_program_threads_and_publishes_the_thread(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, received = collector
    done = _run(
        _channel(),
        "under here",
        "1699999999.000001",
        "",
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert done.returncode == 0, done.stderr
    assert received[0][1]["thread_ts"] == "1699999999.000001"
    assert json.loads(done.stdout)["thread"] == "1700000000.000100", "named for what it is"


def test_a_button_names_its_gate_its_choice_and_the_step_that_posted_it(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """No run id: a foreground run never exported the variable it was read from."""
    url, received = collector
    gate = approval_gate(node_id="ship_it", prompt="Deploy?")
    node = announce(node_id="ask", text="Deploy?", to=_channel(), answers=gate)
    done = _run(
        _channel(),
        "Deploy?",
        "",
        str(node.args[3]),
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert done.returncode == 0, done.stderr
    blocks = received[0][1]["blocks"]
    assert isinstance(blocks, list)
    assert [json.loads(e["value"]) for e in blocks[1]["elements"]] == [
        {"gate": "ship_it", "step": "ask", "choice": "approved", "ask": "", "multiline": False},
        {"gate": "ship_it", "step": "ask", "choice": "rejected", "ask": "notes", "multiline": True},
    ]


def _verdict(done: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """What the program printed about itself. It must always print something."""
    assert done.returncode == 0, f"a report must never fail its step: {done.stderr}"
    loaded = json.loads(done.stdout)
    assert isinstance(loaded, dict)
    return loaded


def test_an_unset_credential_is_reported_without_failing_the_step() -> None:
    """Preflight refuses this at launch; at run time it must not end the run."""
    done = _run(_hook(), "x", HOOK_URL="")
    assert _verdict(done) == {"thread": "", "posted": "false"}
    assert "HOOK_URL" in done.stderr


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_refused_post_leaves_the_run_going(
    collector: tuple[str, list[tuple[str, dict[str, object]]]], status: int
) -> None:
    """A rate limit or an outage says nothing about whether the work succeeded."""
    url, _ = collector
    _Collector.status = status
    done = _run(_hook(), "x", HOOK_URL=url)
    assert _verdict(done)["posted"] == "false"
    assert f"HTTP {status}" in done.stderr
    assert SECRET not in done.stderr + done.stdout


def test_slack_refusing_the_message_is_explained_and_survived(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    url, _ = collector
    _Collector.reply = {"ok": False, "error": "channel_not_found"}
    done = _run(
        _channel(), "x", TEST_TOKEN="xoxb-pretend", TEST_CHANNEL="C0NOPE", SLACK_API_URL=url
    )
    assert _verdict(done) == {"thread": "", "posted": "false"}
    assert "channel_not_found" in done.stderr
    assert "channel id" in done.stderr, "the remedy travels with the refusal"


def test_an_unreachable_service_leaves_the_run_going() -> None:
    done = _run(_hook(), "x", HOOK_URL="http://127.0.0.1:1" + SECRET)
    assert _verdict(done)["posted"] == "false"
    assert "could not reach" in done.stderr
    assert SECRET not in done.stderr


def test_a_service_that_never_answers_is_abandoned_before_the_engine_kills_the_step(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """The engine fails a step that overruns its timeout; the program must not."""
    url, _ = collector
    _Collector.hang = 30.0
    started = time.monotonic()
    done = _run(_hook(), "x", "", "", "1", HOOK_URL=url)
    assert time.monotonic() - started < 10
    assert _verdict(done)["posted"] == "false"
    assert "no answer within 1s" in done.stderr


def test_a_webhook_without_a_scheme_is_refused_without_repeating_it() -> None:
    """urllib quotes the whole URL in its error for this, and the URL is the secret."""
    done = _run(_hook(), "x", HOOK_URL="hooks.slack.com" + SECRET)
    assert _verdict(done)["posted"] == "false"
    assert "should begin https://" in done.stderr
    assert SECRET not in done.stderr + done.stdout


def test_a_token_with_a_windows_line_ending_still_posts(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """A CRLF .env leaves a \\r on the value, and urllib quotes a bad header whole."""
    url, _ = collector
    done = _run(
        _channel(), "x", TEST_TOKEN="xoxb-pretend\r", TEST_CHANNEL="C0TEST", SLACK_API_URL=url
    )
    assert _verdict(done)["posted"] == "true"
    assert _Collector.headers_seen[0]["Authorization"] == "Bearer xoxb-pretend"


def test_a_long_message_and_label_fit_inside_slack_s_limits(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """Past either ceiling Slack refuses the whole message, buttons included."""
    url, received = collector
    gate = approval_gate(node_id="ship_it", prompt="Deploy?", approve_label="A" * 200)
    node = announce(node_id="ask", text="?", to=_channel(), answers=gate)
    done = _run(
        _channel(),
        "x" * 5000,
        "",
        str(node.args[3]),
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert _verdict(done)["posted"] == "true"
    blocks = received[0][1]["blocks"]
    assert isinstance(blocks, list)
    assert len(blocks[0]["text"]["text"]) <= 3000
    assert all(len(e["text"]["text"]) <= 75 for e in blocks[1]["elements"])


def test_the_program_survives_the_engine_rendering_it_as_a_template() -> None:
    """Every argument of a script step is rendered before the step runs.

    Nothing the engine reads as the start of a template expression, block or
    comment may appear in it.
    """
    for service in (_channel(), _hook()):
        assert not re.search(r"\{\{|\{%|\{#", service.program)


def test_an_announcement_is_not_a_contract_the_engine_enforces() -> None:
    """The engine's output check fails the run before any route is evaluated."""
    p = Pipeline(pipeline_id="demo")
    tell = p.add(announce(node_id="tell", text="hello", to=_channel()))
    p.set_entry(tell)
    p.route(tell, END)
    agents = ConductorBackend().document(p)["agents"]
    assert isinstance(agents, list)
    (agent,) = agents
    assert isinstance(agent, dict)
    assert "output" not in agent


def test_a_report_that_slack_declines_to_thread_says_so(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """Asking to reply under a deleted message does not fail: Slack accepts it
    and puts it at the top of the channel instead."""
    url, _ = collector
    _Collector.reply = {
        "ok": True,
        "ts": "1700000000.000100",
        "message": {"ts": "1700000000.000100"},
    }
    done = _run(
        _channel(),
        "under a message that is gone",
        "1699999999.000001",
        "",
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["posted"] == "true", "the message did land"
    assert "put this in the channel instead" in done.stderr
    assert "1699999999.000001" in done.stderr, "name the parent it wanted"


def test_a_report_that_threads_cleanly_says_nothing(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """The warning has to be rare enough to mean something."""
    url, _ = collector
    _Collector.reply = {
        "ok": True,
        "ts": "1700000000.000100",
        "message": {"ts": "1700000000.000100", "thread_ts": "1699999999.000001"},
    }
    done = _run(
        _channel(),
        "under here",
        "1699999999.000001",
        "",
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert done.returncode == 0
    assert done.stderr.strip() == ""


def test_a_report_with_no_parent_is_not_warned_about(
    collector: tuple[str, list[tuple[str, dict[str, object]]]],
) -> None:
    """A run started by hand has no conversation to reply into, and the top of
    the channel is where it belongs."""
    url, _ = collector
    _Collector.reply = {
        "ok": True,
        "ts": "1700000000.000100",
        "message": {"ts": "1700000000.000100"},
    }
    done = _run(
        _channel(),
        "no parent",
        "",
        "",
        TEST_TOKEN="xoxb-pretend",
        TEST_CHANNEL="C0TEST",
        SLACK_API_URL=url,
    )
    assert done.returncode == 0
    assert done.stderr.strip() == ""
