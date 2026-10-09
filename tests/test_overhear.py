"""Reading a channel as yourself: what starts a run, and what this way in cannot do."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from typer.testing import CliRunner

from ictus.bridge.cli import app as bridge
from ictus.bridge.cli import became_of, only
from ictus.bridge.slack.errors import SlackError
from ictus.bridge.slack.requests import asked, request_in
from ictus.bridge.slack.watch import MOST_PAGES, PAGE, Heard, latest, overheard, since
from ictus.interfaces.conductor.emit.manifest import SUFFIX, VERSION
from ictus.runs.launch import Asked, Started
from ictus.runs.triggers import DEFAULT_PREFIX, Trigger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

TRIGGER = Trigger(workflow=Path("demo_work/pipelines/asked/build/asked.yaml"))
CHANNEL = "C0TEST"


def _said(ts: str, text: str, **over: object) -> dict[str, object]:
    """One message as `conversations.history` answers with it — naming no channel."""
    message: dict[str, object] = {"type": "message", "user": "U123", "ts": ts, "text": text}
    message.update(over)
    return message


# --- one rule, both ways in ---------------------------------------------------


def test_a_message_read_back_starts_what_the_same_message_pushed_would_have() -> None:
    """The socket and the poll share a recogniser, so a prefix cannot work on one only."""
    text = f"{DEFAULT_PREFIX} why is the bus failing?"
    envelope: dict[str, object] = {
        "payload": {
            "type": "event_callback",
            "event": {
                "type": "message",
                "user": "U123",
                "ts": "9.1",
                "text": text,
                "channel": CHANNEL,
            },
        }
    }
    (pushed,) = asked(envelope, TRIGGER)
    polled = request_in(_said("9.1", text), CHANNEL, TRIGGER)
    assert polled == pushed


def test_the_channel_asked_about_is_the_one_recorded() -> None:
    """A history message names no channel; the method answers about one you named."""
    request = request_in(_said("1.0", f"{DEFAULT_PREFIX} x"), "C0OTHER", TRIGGER)
    assert request is not None
    assert request.channel == "C0OTHER"
    assert request.thread == "1.0", "the message's own ts, so the answer lands under it"


def test_what_is_not_somebody_typing_is_ignored_here_too() -> None:
    text = f"{DEFAULT_PREFIX} x"
    assert request_in(_said("1.0", text, bot_id="B1"), CHANNEL, TRIGGER) is None
    assert request_in(_said("1.0", text, subtype="channel_join"), CHANNEL, TRIGGER) is None
    assert request_in(_said("1.0", "an ordinary remark"), CHANNEL, TRIGGER) is None


# --- a channel that answers ----------------------------------------------------


class _Channel(BaseHTTPRequestHandler):
    """Stands in for `conversations.history`: newest first, `oldest` exclusive."""

    said: ClassVar[list[dict[str, object]]] = []
    calls: ClassVar[list[dict[str, str]]] = []
    faults: ClassVar[list[tuple[int, dict[str, object], dict[str, str]] | None]] = []
    """Popped one per call. A ``None`` answers normally, so a fault can be
    aimed at a later call than the one that fixes the cursor."""

    def do_GET(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = {key: value[0] for key, value in urllib.parse.parse_qs(query).items()}
        type(self).calls.append(params)
        fault = type(self).faults.pop(0) if type(self).faults else None
        if fault is not None:
            status, body, headers = fault
            self._reply(status, body, headers)
            return
        self._reply(200, self._window(params), {})

    def _window(self, params: dict[str, str]) -> dict[str, object]:
        oldest = float(params.get("oldest") or 0)
        limit = int(params.get("limit") or PAGE)
        start = int(params.get("cursor") or 0)
        newest_first = [one for one in reversed(type(self).said) if float(str(one["ts"])) > oldest]
        page = newest_first[start : start + limit]
        body: dict[str, object] = {"ok": True, "messages": page}
        if start + limit < len(newest_first):
            body["has_more"] = True
            body["response_metadata"] = {"next_cursor": str(start + limit)}
        return body

    def _reply(self, status: int, body: dict[str, object], headers: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_: object) -> None:
        """Silence; the assertions are the output."""


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Channel]]:
    _Channel.said = []
    _Channel.calls = []
    _Channel.faults = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Channel)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "SLACK_API_URL", f"http://127.0.0.1:{server.server_port}/api/chat.postMessage"
    )
    yield _Channel
    server.shutdown()


class _EnoughError(Exception):
    """Ends an endless loop from inside its own pause."""


def _polls(
    rounds: int = 1,
    *,
    every: float = 0.0,
    triggers: Sequence[Trigger] = (TRIGGER,),
    between: Callable[[int], None] | None = None,
) -> tuple[list[Asked], list[float]]:
    """Run `overheard` for `rounds` polls: what it yielded, and what it waited.

    ``between`` is called with the poll just finished, which is the only place a
    test can say something *after* the cursor has been fixed.
    """
    waits: list[float] = []
    found: list[Asked] = []

    def pause(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= rounds:
            raise _EnoughError
        if between is not None:
            between(len(waits))

    with pytest.raises(_EnoughError):
        for request in overheard(
            "xoxp-pretend", channels=[CHANNEL], triggers=list(triggers), every=every, pause=pause
        ):
            found.append(request)
    return found, waits


# --- where listening starts ----------------------------------------------------


def test_what_was_said_before_this_was_running_starts_nothing(
    channel: type[_Channel],
) -> None:
    """A fortnight of backlog would otherwise be a fortnight of runs, all at once."""
    channel.said = [_said(f"{n}.0", f"{DEFAULT_PREFIX} job {n}") for n in range(1, 20)]
    found, _ = _polls(rounds=2)
    assert found == []


def test_the_next_thing_said_starts_one(channel: type[_Channel]) -> None:
    channel.said = [_said("1.0", "chatter")]
    found, _ = _polls(
        rounds=2,
        between=lambda _: channel.said.append(
            _said("2.0", f"{DEFAULT_PREFIX} why is the bus failing?")
        ),
    )
    assert [one.question for one in found] == ["why is the bus failing?"]
    assert [one.who for one in found] == ["U123"]


def test_an_empty_channel_still_starts_from_now(channel: type[_Channel]) -> None:
    """There is no newest message to start after, and zero would mean everything."""
    channel.said = []
    cursor = latest("xoxp-pretend", CHANNEL)
    assert float(cursor) > 1_700_000_000, "a clock, not the beginning of the channel"


def test_one_message_is_read_once(channel: type[_Channel]) -> None:
    """Three polls over one request: the cursor moves past it, not back over it."""
    channel.said = [_said("1.0", "chatter")]
    said_at = 1

    def say_once(poll: int) -> None:
        if poll == said_at:
            channel.said.append(_said("2.0", f"{DEFAULT_PREFIX} once please"))

    found, waits = _polls(rounds=4, between=say_once)
    assert len(waits) == 4, "it kept polling"
    assert [one.question for one in found] == ["once please"]


def test_the_first_trigger_that_matches_wins(channel: type[_Channel]) -> None:
    """The same rule `events` follows: one message, one run."""
    first = Trigger(workflow=Path("a.yaml"), prefix="Go:", pipeline="first")
    second = Trigger(workflow=Path("b.yaml"), prefix="Go:", pipeline="second")
    channel.said = [_said("1.0", "chatter")]
    found, _ = _polls(
        rounds=2,
        triggers=(first, second),
        between=lambda _: channel.said.append(_said("2.0", "Go: both of these claim it")),
    )
    assert [one.trigger.pipeline for one in found if one.trigger] == ["first"]


def test_a_poll_answers_oldest_first(channel: type[_Channel]) -> None:
    """Slack pages newest first; a run per message should still start in order."""
    channel.said = [_said("1.0", "chatter")]
    heard = since("xoxp-pretend", CHANNEL, "1.0")
    assert heard.messages == ()

    channel.said += [_said(f"{n}.0", f"{DEFAULT_PREFIX} job {n}") for n in (2, 3, 4)]
    heard = since("xoxp-pretend", CHANNEL, "1.0")
    assert [one["ts"] for one in heard.messages] == ["2.0", "3.0", "4.0"]
    assert heard.cursor == "4.0", "the newest read, whichever end it arrived from"


def test_more_in_one_poll_than_the_pages_hold_is_said_not_swallowed(
    channel: type[_Channel], caplog: pytest.LogCaptureFixture
) -> None:
    channel.said = [_said("1.0", "chatter")]
    channel.said += [_said(f"{n}.0", "chatter") for n in range(2, 40)]
    heard = since("xoxp-pretend", CHANNEL, "1.0", limit=5, pages=2)
    assert len(heard.messages) == 10
    assert "the oldest were not read" in caplog.text
    assert MOST_PAGES > 1, "the default leaves room for a busy channel"


# --- when Slack says no --------------------------------------------------------


def test_rate_limiting_is_obeyed_to_the_second_slack_names(channel: type[_Channel]) -> None:
    channel.said = [_said("1.0", "chatter")]
    channel.faults = [None, (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "47"})]
    _, waits = _polls(rounds=1, every=5.0)
    assert waits == [47.0], "Slack knows when the window reopens; doubling spends the next one"


def test_a_rate_limited_poll_does_not_move_the_cursor(channel: type[_Channel]) -> None:
    channel.said = [_said("1.0", "chatter"), _said("2.0", f"{DEFAULT_PREFIX} do not lose me")]
    heard = since("xoxp-pretend", CHANNEL, "1.0")  # cursor would move to 2.0
    assert heard.cursor == "2.0"
    channel.faults = [(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "3"})]
    refused_poll = since("xoxp-pretend", CHANNEL, "1.0")
    assert refused_poll.cursor == "1.0"
    assert refused_poll.messages == ()
    assert refused_poll.after == 3.0


def test_rate_limiting_without_a_number_still_waits(channel: type[_Channel]) -> None:
    channel.said = [_said("1.0", "chatter")]
    channel.faults = [None, (429, {"ok": False, "error": "ratelimited"}, {})]
    _, waits = _polls(rounds=1, every=1.0)
    assert waits[0] >= 60.0


def test_a_refused_credential_stops_it_rather_than_being_retried(
    channel: type[_Channel],
) -> None:
    channel.faults = [(200, {"ok": False, "error": "missing_scope"}, {})]
    with pytest.raises(SlackError, match="channels:history"):
        _polls(rounds=1)


def test_a_channel_this_token_cannot_read_is_named(channel: type[_Channel]) -> None:
    """Not a credential problem, and no amount of waiting fixes it either."""
    channel.faults = [(200, {"ok": False, "error": "not_in_channel"}, {})]
    with pytest.raises(SlackError, match="View channel details"):
        _polls(rounds=1)


def test_a_poll_that_merely_failed_is_survived(channel: type[_Channel]) -> None:
    channel.said = [_said("1.0", "chatter")]
    channel.faults = [(500, {}, {})]
    heard = since("xoxp-pretend", CHANNEL, "1.0")
    assert heard.cursor == "1.0", "nothing was read, so nothing is behind us"
    assert "HTTP 500" in heard.why
    assert heard.after == 0.0


def test_a_failed_poll_does_not_end_the_listener(
    channel: type[_Channel], caplog: pytest.LogCaptureFixture
) -> None:
    channel.said = [_said("1.0", "chatter")]
    channel.faults = [None, (500, {}, {})]
    found, waits = _polls(
        rounds=3,
        between=lambda poll: (
            channel.said.append(_said(f"{poll + 1}.0", f"{DEFAULT_PREFIX} job"))
            if poll == 1
            else None
        ),
    )
    assert len(waits) == 3, "it polled again"
    assert [one.question for one in found] == ["job"], "and caught up with what it missed"
    assert "trying again" in caplog.text


def test_slack_being_unreachable_at_the_start_is_not_the_end_of_it(
    channel: type[_Channel], caplog: pytest.LogCaptureFixture
) -> None:
    """The cursor is fixed by the first poll that lands, not the first attempted.

    So what is said during the outage is backlog and starts nothing — the same
    bargain the socket makes while it is redialling — and what is said after
    the first poll lands is read normally.
    """
    channel.said = [_said("1.0", "chatter")]
    channel.faults = [(500, {}, {})]

    def say(poll: int) -> None:
        if poll == 1:
            channel.said.append(_said("2.0", f"{DEFAULT_PREFIX} during the outage"))
        if poll == 2:
            channel.said.append(_said("3.0", f"{DEFAULT_PREFIX} after it came back"))

    found, _ = _polls(rounds=4, between=say)
    assert "could not start reading" in caplog.text
    assert [one.question for one in found] == ["after it came back"]


def test_a_channel_that_could_not_be_read_yet_is_never_read_from_the_beginning(
    channel: type[_Channel], caplog: pytest.LogCaptureFixture
) -> None:
    channel.said = [_said(f"{n}.0", f"{DEFAULT_PREFIX} backlog {n}") for n in range(1, 10)]
    channel.faults = [(500, {}, {})]
    found, _ = _polls(rounds=3)
    assert "could not start reading" in caplog.text
    assert found == [], "the backlog was there the whole time and started nothing"


# --- what this way in cannot do ------------------------------------------------


def test_only_requests_come_back(channel: type[_Channel]) -> None:
    """No user token receives a press, so nothing here can be mistaken for one."""
    channel.said = [_said("1.0", "chatter")]
    found, _ = _polls(
        rounds=2,
        between=lambda _: channel.said.append(_said("2.0", f"{DEFAULT_PREFIX} a run")),
    )
    assert found, "something was read"
    assert all(isinstance(one, Asked) for one in found)
    assert Heard().messages == (), "an empty poll carries nothing either"


def test_the_listener_s_own_reply_does_not_start_another_run() -> None:
    """`overhear` posts as the person listening, so its line comes back as an
    ordinary message on the next poll — nothing marks it as the listener's own."""
    request = Asked(
        question="why is the bus failing?",
        thread="1.0",
        channel=CHANNEL,
        who="U123",
        trigger=TRIGGER,
    )
    started = became_of(request, Started(dashboard="http://127.0.0.1:50984", run_id="abc"))
    assert "Working on it" in started
    assert request_in(_said("2.0", started), CHANNEL, TRIGGER) is None

    refused = became_of(request, Started(why="conductor is not on PATH"))
    assert request_in(_said("3.0", refused), CHANNEL, TRIGGER) is None


def test_a_prefix_quoted_back_inside_the_reply_still_starts_nothing() -> None:
    """The nastiest shape: somebody types the prefix twice, so it is in the
    question, so it is in the line posted under it."""
    request = Asked(
        question=f"{DEFAULT_PREFIX} and again",
        thread="1.0",
        channel=CHANNEL,
        who="U123",
        trigger=TRIGGER,
    )
    line = became_of(request, Started())
    assert DEFAULT_PREFIX in line, "the prefix really is in there"
    assert request_in(_said("2.0", line), CHANNEL, TRIGGER) is None, "but not at the start of it"


# --- pointing a listener at one pipeline ---------------------------------------


def _trigger(name: str, prefix: str = "Alert:") -> Trigger:
    return Trigger(workflow=Path(f"{name}.yaml"), prefix=prefix, pipeline=name)


def test_naming_nothing_keeps_every_pipeline_found() -> None:
    """One listener serving a whole folder is what this has always done."""
    found = [_trigger("smoke_stop"), _trigger("trustless-manual-db", "New DB ticket raised:")]
    kept, why = only(found, ())
    assert kept == found
    assert why == ""


def test_a_listener_can_be_pointed_at_one_pipeline() -> None:
    found = [_trigger("smoke_stop"), _trigger("trustless-manual-db", "New DB ticket raised:")]
    kept, why = only(found, ["smoke_stop"])
    assert [trigger.pipeline for trigger in kept] == ["smoke_stop"]
    assert why == ""


def test_more_than_one_can_be_named() -> None:
    found = [_trigger("a"), _trigger("b"), _trigger("c")]
    kept, _ = only(found, ["a", "c"])
    assert [trigger.pipeline for trigger in kept] == ["a", "c"]


def test_a_name_nothing_claims_stops_the_listener() -> None:
    """The failure this exists for. A typo, or a pipeline never emitted, would
    otherwise leave a listener running and watching for a prefix nothing sends."""
    found = [_trigger("smoke_stop")]
    kept, why = only(found, ["smoke-stop"])
    assert kept == []
    assert "no pipeline called smoke-stop" in why
    assert "smoke_stop" in why, "and it says what was actually there"


def test_two_pipelines_claiming_one_prefix_is_why_this_exists() -> None:
    """Sorted-path order decides silently otherwise, and the loser never fires."""
    found = [_trigger("aardvark"), _trigger("smoke_stop")]
    first = next(
        request
        for request in (request_in(_said("1.0", "Alert: the bus"), CHANNEL, t) for t in found)
        if request is not None
    )
    assert first.trigger is not None
    assert first.trigger.pipeline == "aardvark", "whichever sorted first wins"

    kept, _ = only(found, ["smoke_stop"])
    picked = request_in(_said("1.0", "Alert: the bus"), CHANNEL, kept[0])
    assert picked is not None and picked.trigger is not None
    assert picked.trigger.pipeline == "smoke_stop"


def _emitted(where: Path, pipeline: str, prefix: str = "Alert:") -> Path:
    """One manifest on disk, which is the whole of what a listener reads.

    Written rather than compiled: this file is about reading a channel, and a
    listener has never needed the compiler, the pipeline source or its config.
    """
    (where / f"{pipeline}.yaml").write_text("workflow: {}", encoding="utf-8")
    (where / f"{pipeline}{SUFFIX}").write_text(
        json.dumps(
            {
                "manifest": VERSION,
                "pipeline": pipeline,
                "workflow": f"{pipeline}.yaml",
                "listeners": [{"prefix": prefix, "inputs": {"question": "question"}}],
            }
        ),
        encoding="utf-8",
    )
    return where


def test_the_option_reaches_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A name nothing claims stops it at startup, before a socket or a poll."""
    _emitted(tmp_path, "hears_things")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-pretend")
    result = CliRunner().invoke(
        bridge,
        ["overhear", str(tmp_path), "--channel", "C0TEST", "--pipeline", "no-such-thing"],
    )
    assert result.exit_code == 1
    assert "no pipeline called no-such-thing" in result.output
    assert "hears_things" in result.output, "it says what it did find"


def test_naming_a_pipeline_with_nowhere_to_find_it_is_refused() -> None:
    """`listen` takes no folder when it only answers gates; naming one to start
    without saying where it was built is a contradiction, not a default."""
    runner = CliRunner(env={"SLACK_APP_TOKEN": "xapp-x", "SLACK_BOT_TOKEN": "xoxb-x"})
    result = runner.invoke(bridge, ["listen", "--pipeline", "smoke_stop"])
    assert result.exit_code == 1
    assert "no folder says where to find it" in result.output
