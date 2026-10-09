"""Button presses: decoding them, finding their run, and answering only the current question."""

from __future__ import annotations

import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar

import pytest

from ictus.bridge.slack.errors import SlackError, SlackUnreachableError
from ictus.bridge.slack.listen import (
    FORM_ID,
    Click,
    Note,
    events,
    open_form,
    open_socket,
    presses,
    retire,
    verdict,
)
from ictus.interfaces.conductor.control.live import LiveRun
from ictus.interfaces.conductor.control.respond import answer_gate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

RUN = LiveRun(run_id="abc12345", workflow="w", port=59999, pid=1, started_at="2026")
OTHER = LiveRun(run_id="ffff0000", workflow="w", port=59998, pid=1, started_at="2026")


def _envelope(*, message: dict[str, object] | None = None, **value: object) -> dict[str, object]:
    return {
        "envelope_id": "env-1",
        "type": "interactive",
        "payload": {
            "type": "block_actions",
            "trigger_id": "trig-1",
            "user": {"id": "U123"},
            "channel": {"id": "C0TEST"},
            "message": message or {"ts": "1700000000.000200", "text": "Ship it?"},
            "actions": [
                {"type": "button", "text": {"text": "Reject"}, "value": json.dumps(value)}
                if value
                else {}
            ],
        },
    }


def _click(**over: object) -> Click:
    fields: dict[str, object] = {
        "gate": "ship_it",
        "choice": "approved",
        "step": "report_ship_it",
        "who": "U123",
        "channel": "C0TEST",
        "message_ts": "200.2",
        "thread_ts": "100.0",
    }
    fields.update(over)
    return Click(**fields)  # type: ignore[arg-type]


# --- decoding a press --------------------------------------------------------


def test_a_press_names_the_gate_the_choice_and_the_step_that_posted_it() -> None:
    (click,) = events(_envelope(gate="ship_it", choice="approved", step="report_ship_it"))
    assert isinstance(click, Click)
    assert (click.gate, click.choice, click.step) == ("ship_it", "approved", "report_ship_it")
    assert (click.who, click.channel) == ("U123", "C0TEST")
    assert click.message_ts == "1700000000.000200", "which time the question was asked"
    assert click.thread_ts == "1700000000.000200", "a root's own ts is its thread"


def test_a_button_already_inside_a_thread_replies_in_that_thread() -> None:
    """Its own ts is the reply's, not the thread's; the parent is thread_ts."""
    message: dict[str, object] = {"ts": "1700000009.000999", "thread_ts": "1700000000.000100"}
    (click,) = events(_envelope(message=message, gate="g", choice="c", step="s"))
    assert isinstance(click, Click)
    assert click.thread_ts == "1700000000.000100"
    assert click.message_ts == "1700000009.000999"


def test_a_choice_that_asks_for_text_says_so() -> None:
    envelope = _envelope(gate="g", choice="rejected", step="s", ask="notes", multiline=True)
    (click,) = events(envelope)
    assert isinstance(click, Click)
    assert (click.ask, click.multiline, click.trigger_id) == ("notes", True, "trig-1")
    assert click.label == "Reject"


def test_a_button_that_is_not_ours_is_ignored() -> None:
    """Another app's buttons arrive too if it shares the channel."""
    envelope = _envelope()
    payload = envelope["payload"]
    assert isinstance(payload, dict)
    payload["actions"] = [{"type": "button", "value": "not json at all"}]
    assert list(events(envelope)) == []


def test_a_button_of_ours_missing_a_field_is_ignored() -> None:
    assert list(events(_envelope(gate="g", choice="c"))) == []


def test_anything_that_is_not_a_press_or_a_form_is_ignored() -> None:
    """Slash commands and shortcuts come down the same socket."""
    assert list(events({"payload": {"type": "shortcut"}})) == []
    assert list(events({"type": "hello"})) == []


# --- the dashboard's side ----------------------------------------------------


class _Dashboard(BaseHTTPRequestHandler):
    bodies: ClassVar[list[dict[str, object]]] = []
    drop: ClassVar[bool] = False

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        if type(self).drop:
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "accepted"}')

    def log_message(self, *_: object) -> None:
        """Silence; the assertions are the output."""


@pytest.fixture
def dashboard() -> Iterator[LiveRun]:
    _Dashboard.bodies = []
    _Dashboard.drop = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Dashboard)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield LiveRun(run_id="r", workflow="w", port=server.server_port, pid=1, started_at="2026")
    server.shutdown()


def test_a_note_reaches_the_run_as_the_choice_s_text(dashboard: LiveRun) -> None:
    outcome = answer_gate(dashboard, gate="g", choice="rejected", note="fix the tests", token="t")
    assert outcome.accepted
    assert _Dashboard.bodies[0]["additional_input"] == "fix the tests"


def test_a_proxy_in_the_environment_is_not_used_for_the_dashboard(
    dashboard: LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to be, unless no_proxy named 127.0.0.1 — with the dashboard's token."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert answer_gate(dashboard, gate="g", choice="c", token="t").accepted


def test_a_dashboard_that_drops_the_answer_is_reported_not_raised(dashboard: LiveRun) -> None:
    """A run shutting down closes the connection mid-request."""
    _Dashboard.drop = True
    outcome = answer_gate(dashboard, gate="g", choice="c", token="t")
    assert not outcome.accepted
    assert "stopped answering" in outcome.detail


def test_answering_a_run_that_is_not_there_is_reported() -> None:
    gone = LiveRun(run_id="x", workflow="w", port=1, pid=1, started_at="2026")
    outcome = answer_gate(gone, gate="g", choice="c", token="t")
    assert not outcome.accepted
    assert "no longer listening" in outcome.detail


def test_answering_without_a_token_refuses_before_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONDUCTOR_GATE_TOKEN", raising=False)
    monkeypatch.setattr(
        "ictus.interfaces.conductor.control.respond.token_for",
        lambda *a, **k: None,  # noqa: ARG005
    )
    outcome = answer_gate(RUN, gate="g", choice="c")
    assert not outcome.accepted
    assert "token" in outcome.detail


# --- Slack's side --------------------------------------------------------------


class _Slack(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, dict[str, object]]]] = []
    answer: ClassVar[dict[str, object]] = {"ok": True}
    status: ClassVar[int] = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        type(self).calls.append((self.path.rsplit("/", 1)[-1], json.loads(self.rfile.read(length))))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps(type(self).answer).encode())

    def log_message(self, *_: object) -> None:
        """Silence; the assertions are the output."""


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, object]]]]:
    _Slack.calls = []
    _Slack.answer = {"ok": True}
    _Slack.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slack)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "SLACK_API_URL", f"http://127.0.0.1:{server.server_port}/api/chat.postMessage"
    )
    yield _Slack.calls
    server.shutdown()


def test_a_form_carries_the_press_it_finishes(slack: list[tuple[str, dict[str, object]]]) -> None:
    """Its submission arrives later and on its own, so it has to say what it answers."""
    click = _click(choice="rejected", ask="notes", multiline=True, trigger_id="trig-1")
    assert open_form("xoxb", click) == ""
    ((method, body),) = slack
    assert method == "views.open"
    assert body["trigger_id"] == "trig-1"
    view = body["view"]
    assert isinstance(view, dict)
    submission = {
        "payload": {
            "type": "view_submission",
            "user": {"id": "U777"},
            "view": {
                "callback_id": view["callback_id"],
                "private_metadata": view["private_metadata"],
                "state": {"values": {"note": {"text": {"value": "the tests are missing"}}}},
            },
        }
    }
    (note,) = events(submission)
    assert isinstance(note, Note)
    assert note.text == "the tests are missing"
    assert (note.click.gate, note.click.choice, note.click.step) == (
        "ship_it",
        "rejected",
        "report_ship_it",
    )
    assert note.click.ask == "notes"
    assert note.click.message_ts == click.message_ts
    assert note.click.who == "U777", "the answer is whoever sent the form"


def test_a_form_from_somewhere_else_is_ignored() -> None:
    other = {"payload": {"type": "view_submission", "view": {"callback_id": "not-ours"}}}
    assert list(events(other)) == []
    assert FORM_ID != "not-ours"


def test_an_answered_question_loses_its_buttons(
    slack: list[tuple[str, dict[str, object]]],
) -> None:
    click = _click(text="Ship it?")
    assert retire("xoxb", click, "U123 answered approved") == ""
    ((method, body),) = slack
    assert method == "chat.update"
    assert body["ts"] == click.message_ts
    blocks = body["blocks"]
    assert isinstance(blocks, list)
    assert [block["type"] for block in blocks] == ["section", "context"]


def test_what_happened_is_said_both_ways() -> None:
    assert (
        verdict(_click(), answered=True, run_id="abc") == "<@U123> answered *approved* on run `abc`"
    )
    refused = verdict(_click(), answered=False, reason="it moved on")
    assert refused.endswith("nothing was done: it moved on")


@pytest.mark.usefixtures("slack")
def test_a_bad_token_is_told_from_a_bad_network() -> None:
    """One ends the listener with the fix; the other is a reason to dial again."""
    _Slack.answer = {"ok": False, "error": "invalid_auth"}
    with pytest.raises(SlackError, match="connections:write") as caught:
        open_socket("xapp")
    assert not isinstance(caught.value, SlackUnreachableError)
    _Slack.answer = {"ok": False, "error": "ratelimited"}
    with pytest.raises(SlackUnreachableError):
        open_socket("xapp")
    _Slack.status = 503
    with pytest.raises(SlackUnreachableError):
        open_socket("xapp")


# --- the connection ------------------------------------------------------------


def _frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message).encode()
    if len(payload) < 126:
        return bytes([0x81, len(payload)]) + payload
    return bytes([0x81, 126]) + struct.pack("!H", len(payload)) + payload


def _read_frame(connection: socket.socket) -> bytes:
    def exactly(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    _, second = exactly(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", exactly(2))[0]
    mask = exactly(4)
    return bytes(byte ^ mask[i % 4] for i, byte in enumerate(exactly(size)))


def _peer(behave: Callable[[socket.socket], None]) -> tuple[int, threading.Thread]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run() -> None:
        connection, _ = listener.accept()
        connection.settimeout(5.0)
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
        try:
            behave(connection)
        finally:
            connection.close()
            listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def test_a_dropped_connection_is_dialled_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A laptop sleeping or a VPN reconnecting used to end the listener for good."""
    acks: list[bytes] = []

    def hangs_up(connection: socket.socket) -> None:
        connection.sendall(_frame({"type": "hello"}))  # then gone, with no close frame

    def delivers(connection: socket.socket) -> None:
        connection.sendall(_frame(_envelope(gate="g", choice="yes", step="s")))
        acks.append(_read_frame(connection))

    first, _ = _peer(hangs_up)
    second, delivered = _peer(delivers)
    urls = iter([f"ws://127.0.0.1:{first}/link", f"ws://127.0.0.1:{second}/link"])
    monkeypatch.setattr("ictus.bridge.slack.listen.open_socket", lambda token: next(urls))  # noqa: ARG005

    stream = presses("xapp", pause=lambda seconds: None)  # noqa: ARG005
    click = next(stream)
    delivered.join(timeout=5)
    stream.close()
    assert isinstance(click, Click)
    assert click.choice == "yes"
    assert json.loads(acks[0]) == {"envelope_id": "env-1"}, "acknowledged before handing on"


def test_an_unreachable_slack_is_retried_with_growing_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    attempts = iter(
        [SlackUnreachableError("down"), SlackUnreachableError("down"), SlackError("no")]
    )

    def dial(token: str) -> str:  # noqa: ARG001
        raise next(attempts)

    monkeypatch.setattr("ictus.bridge.slack.listen.open_socket", dial)
    with pytest.raises(SlackError, match="no"):
        next(presses("xapp", pause=pauses.append))
    assert pauses == [1.0, 2.0]
