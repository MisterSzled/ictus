"""The watcher: finding a run, reading it as signals, and letting go of it."""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from ictus import RunSignal
from ictus.cli import app
from ictus.interfaces import ENDED, SignalEvent
from ictus.interfaces.conductor.control.events import signals_from, watch
from ictus.interfaces.conductor.control.live import LiveRun, live_runs, token_for

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = Path(__file__).parent / "fixtures"

RUN = LiveRun(run_id="abc123", workflow="smoke-events", port=1234, pid=9, started_at="2026")


def _a_dead_pid() -> int:
    """A pid nothing is using. Spawn one, wait for it, and take its number —
    which beats picking a constant that might belong to something on the day."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _recorded(name: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


# --- reading a recorded run --------------------------------------------------


def test_a_whole_run_reads_as_the_signals_that_happened() -> None:
    seen = [e.signal for e in signals_from(_recorded("run-events-approved.jsonl"), RUN)]
    assert seen == [
        RunSignal.RUN_STARTED,
        RunSignal.DECISION_NEEDED,
        RunSignal.DECISION_MADE,
        RunSignal.DECISION_NEEDED,
        RunSignal.DECISION_MADE,
        RunSignal.RUN_FINISHED,
    ]


def test_the_trace_events_are_dropped_rather_than_reported() -> None:
    """17 events in, 6 out. A integration forwarding the rest would be a firehose."""
    events = _recorded("run-events-approved.jsonl")
    assert len(events) == 17
    assert len(list(signals_from(events, RUN))) == 6


def test_a_signal_carries_the_run_it_happened_in() -> None:
    first = next(iter(signals_from(_recorded("run-events-approved.jsonl"), RUN)))
    assert (first.run_id, first.workflow) == ("abc123", "smoke-events")
    assert first.event_type == "workflow_started"
    assert first.at > 0


def test_a_decision_carries_what_is_being_asked() -> None:
    gate = next(
        e
        for e in signals_from(_recorded("run-events-approved.jsonl"), RUN)
        if e.signal is RunSignal.DECISION_NEEDED
    )
    assert gate.step == "confirm_start"
    assert gate.options == ("start", "cancel")


def test_the_answer_comes_back_with_the_free_text() -> None:
    """What a report needs to say who chose what."""
    answered = [
        e
        for e in signals_from(_recorded("run-events-rejected.jsonl"), RUN)
        if e.signal is RunSignal.DECISION_MADE
    ]
    assert answered[-1].choice == "rejected"
    assert answered[-1].notes == (("notes", "not this time — from the spike client"),)


# --- what it must stop doing -------------------------------------------------


def test_nothing_is_reported_after_the_run_ends() -> None:
    """Holding on past the end is what stops a detached run reaping."""
    events = [*_recorded("run-events-approved.jsonl")]
    events.append({"type": "gate_presented", "timestamp": 9.9, "data": {}})
    seen = list(signals_from(events, RUN))
    assert seen[-1].signal is RunSignal.RUN_FINISHED
    assert seen[-1].ends_the_run
    assert len(seen) == 6


def test_both_endings_end_it() -> None:
    assert {RunSignal.RUN_FINISHED, RunSignal.RUN_FAILED} == ENDED
    failed = [{"type": "workflow_failed", "timestamp": 1.0, "data": {}}]
    assert next(iter(signals_from(failed, RUN))).ends_the_run


def test_the_overlap_between_history_and_the_socket_is_absorbed() -> None:
    """Connect-then-seed duplicates events on purpose; losing one is the bug."""
    events = _recorded("run-events-approved.jsonl")
    doubled = [*events, *events]
    assert len(list(signals_from(doubled, RUN))) == len(list(signals_from(events, RUN)))


def test_an_unknown_event_is_dropped_rather_than_raising() -> None:
    stream: list[dict[str, object]] = [
        {"type": "something_new_in_0_2_0", "timestamp": 1.0, "data": {}},
        {"type": "workflow_completed", "timestamp": 2.0, "data": {}},
    ]
    assert [e.signal for e in signals_from(stream, RUN)] == [RunSignal.RUN_FINISHED]


def test_a_malformed_event_does_not_stop_the_stream() -> None:
    """A truncated or half-written line on a live run is normal."""
    stream: list[dict[str, object]] = [
        {"no_type": True},
        {"type": 42},
        {"type": "workflow_completed", "timestamp": 1.0},
    ]
    assert [e.signal for e in signals_from(stream, RUN)] == [RunSignal.RUN_FINISHED]


def test_a_stage_finishing_is_not_the_run_finishing() -> None:
    """A stage is a child workflow emitting its own lifecycle into the same stream.

    Read as the run's, it reports "finished" partway through.
    """
    stream: list[dict[str, object]] = [
        {"type": "workflow_completed", "timestamp": 1.0, "data": {"subworkflow_path": ["review"]}},
        {"type": "gate_presented", "timestamp": 2.0, "data": {"agent_name": "ship_it"}},
        {"type": "workflow_completed", "timestamp": 3.0, "data": {}},
    ]
    seen = [e.signal for e in signals_from(stream, RUN)]
    assert seen == [RunSignal.DECISION_NEEDED, RunSignal.RUN_FINISHED]


def test_what_happened_before_attaching_is_marked_as_such() -> None:
    stream: list[dict[str, object]] = [
        {"type": "workflow_started", "timestamp": 10.0, "data": {}},
        {"type": "budget_exceeded", "timestamp": 30.0, "data": {}},
    ]
    seen = list(signals_from(stream, RUN, live_from=20.0))
    assert [e.replayed for e in seen] == [True, False]


def test_what_a_step_already_announced_is_said_to_be() -> None:
    """Every gate and ending has a step in front of it; an engine failure does not."""
    stream: list[dict[str, object]] = [
        {"type": "gate_presented", "timestamp": 1.0, "data": {"agent_name": "g"}},
        {"type": "iteration_limit_reached", "timestamp": 2.0, "data": {"agent_name": "g"}},
        {"type": "budget_exceeded", "timestamp": 3.0, "data": {}},
    ]
    announced = {e.event_type: e.at_a_step for e in signals_from(stream, RUN)}
    assert announced == {
        "gate_presented": True,
        "iteration_limit_reached": False,
        "budget_exceeded": False,
    }
    explicit = [{"type": "workflow_failed", "timestamp": 1.0, "data": {"is_explicit": True}}]
    raised = [{"type": "workflow_failed", "timestamp": 1.0, "data": {"error_type": "KeyError"}}]
    assert next(iter(signals_from(explicit, RUN))).at_a_step
    assert not next(iter(signals_from(raised, RUN))).at_a_step


def test_the_waits_no_gate_stands_in_front_of_are_decisions_too() -> None:
    """A background run parks on its iteration limit until somebody answers."""
    stream: list[dict[str, object]] = [
        {"type": "iteration_limit_reached", "timestamp": 1.0, "data": {"agent_name": "loop"}},
        {"type": "dialog_started", "timestamp": 2.0, "data": {"agent_name": "chat"}},
    ]
    assert [e.signal for e in signals_from(stream, RUN)] == [
        RunSignal.DECISION_NEEDED,
        RunSignal.DECISION_NEEDED,
    ]


def test_one_dedupe_holds_across_reconnects() -> None:
    """Each reconnect reads the history again; none of it is new the second time."""
    seen: set[tuple[str, float]] = set()
    events = _recorded("run-events-approved.jsonl")[:-1]  # stop short of the end
    first = list(signals_from(events, RUN, seen=seen))
    again = list(signals_from(events, RUN, seen=seen))
    assert first
    assert again == []


# --- the watch itself, against a stand-in dashboard --------------------------


def _frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message).encode()
    if len(payload) < 126:
        return bytes([0x81, len(payload)]) + payload
    return bytes([0x81, 126]) + struct.pack("!H", len(payload)) + payload


def _dashboard(
    history: list[dict[str, object]], sockets: list[Callable[[socket.socket], None]]
) -> int:
    """A dashboard serving ``history`` at /api/state and one socket per plan, in order."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    plans = iter(sockets)

    def serve() -> None:
        while True:
            connection, _ = listener.accept()
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            line = request.split(b"\r\n", 1)[0]
            if b" /api/state" in line:
                body = json.dumps(history).encode()
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                connection.close()
                continue
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
            plan = next(plans, None)
            if plan is not None:
                plan(connection)
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    port: int = listener.getsockname()[1]
    return port


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port: int = probe.getsockname()[1]
    probe.close()
    return port


def test_a_dropped_socket_is_dialled_again_while_the_run_lives() -> None:
    """A watcher used to treat any disconnect as the run ending, and let go."""
    history: list[dict[str, object]] = [
        {"type": "workflow_started", "timestamp": 1.0, "data": {}},
        {"type": "gate_presented", "timestamp": 2.0, "data": {"agent_name": "g"}},
    ]
    ended = {"type": "workflow_completed", "timestamp": time.time() + 1000, "data": {}}

    def drops(_: socket.socket) -> None:
        """Hang up at once, with no close frame."""

    def ends(connection: socket.socket) -> None:
        connection.sendall(_frame(ended))
        time.sleep(0.5)

    port = _dashboard(history, [drops, ends])
    run = LiveRun(run_id="r", workflow="w", port=port, pid=os.getpid(), started_at="2026")
    seen = list(watch(run, token="t", pause=lambda _: None))
    assert [(e.signal, e.replayed) for e in seen] == [
        (RunSignal.RUN_STARTED, True),
        (RunSignal.DECISION_NEEDED, True),
        (RunSignal.RUN_FINISHED, False),
    ]


def test_a_run_whose_engine_vanished_is_reported_as_failed() -> None:
    """Killed, crashed or asleep, it says nothing; nothing inside it can either."""
    run = LiveRun(run_id="r", workflow="w", port=_closed_port(), pid=_a_dead_pid(), started_at="x")
    (gone,) = list(watch(run, token="t", pause=lambda _: None))
    assert gone.signal is RunSignal.RUN_FAILED
    assert gone.event_type == "engine_gone"
    assert not gone.at_a_step and not gone.replayed, "this one has to be delivered"


def test_a_live_process_whose_dashboard_never_answers_is_given_up_on() -> None:
    """A recycled pid can belong to something that is not a run."""
    run = LiveRun(run_id="r", workflow="w", port=_closed_port(), pid=os.getpid(), started_at="x")
    with pytest.raises(ConnectionError, match="stopped answering"):
        list(watch(run, token="t", pause=lambda _: None))


def test_a_watcher_whose_run_breaks_unexpectedly_says_so_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated read of the history killed the thread silently, and the watch hung."""
    run = LiveRun(run_id="r1", workflow="w", port=1, pid=os.getpid(), started_at="2026")

    def breaks(_: LiveRun) -> list[SignalEvent]:
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr("ictus.cli.watching.live_runs", lambda: [run])
    monkeypatch.setattr("ictus.cli.watching.watch_run", breaks)
    result = CliRunner().invoke(app, ["watch"])
    assert result.exit_code == 0
    assert "IncompleteRead" in result.output


# --- discovery ---------------------------------------------------------------


def _record(directory: Path, run_id: str, **fields: object) -> None:
    """A record for a live run. The pid is this process's, since `live_runs` checks it."""
    body: dict[str, object] = {
        "run_id": run_id,
        "workflow_name": "smoke-events",
        "port": 50000,
        "pid": os.getpid(),
        "started_at": "2026-10-05T09:34:59+00:00",
    }
    body.update(fields)
    (directory / f"{run_id}.json").write_text(json.dumps(body))


def test_a_run_is_found_from_its_record(tmp_path: Path) -> None:
    _record(tmp_path, "aaa", event_log_path="/tmp/aaa.events.jsonl")
    (found,) = live_runs(runs_dir=tmp_path)
    assert (found.run_id, found.port, found.workflow) == ("aaa", 50000, "smoke-events")
    assert found.event_log == Path("/tmp/aaa.events.jsonl")
    assert found.dashboard == "http://127.0.0.1:50000"
    assert found.socket_url == "ws://127.0.0.1:50000/ws"


def test_a_foreground_run_is_not_watchable(tmp_path: Path) -> None:
    """No port means no socket and no way to answer it from outside."""
    _record(tmp_path, "aaa", port=None)
    assert live_runs(runs_dir=tmp_path) == []


def test_a_record_being_written_is_skipped_not_fatal(tmp_path: Path) -> None:
    """Normal on a busy machine; a watcher that died on one would die often."""
    _record(tmp_path, "good")
    (tmp_path / "half.json").write_text('{"run_id": "half", "po')
    assert [r.run_id for r in live_runs(runs_dir=tmp_path)] == ["good"]


def test_runs_come_back_oldest_first(tmp_path: Path) -> None:
    _record(tmp_path, "later", started_at="2026-10-05T12:00:00+00:00")
    _record(tmp_path, "earlier", started_at="2026-10-05T09:00:00+00:00")
    assert [r.run_id for r in live_runs(runs_dir=tmp_path)] == ["earlier", "later"]


def test_a_record_whose_process_died_is_not_a_live_run(tmp_path: Path) -> None:
    """The engine archives a record on a graceful exit only, so a killed run,
    a crash or a closed laptop leaves one behind."""
    _record(tmp_path, "zombie", pid=_a_dead_pid())
    assert live_runs(runs_dir=tmp_path) == []


def test_a_record_with_no_pid_is_believed(tmp_path: Path) -> None:
    """Nothing recorded is nothing to disprove; an older engine wrote no pid."""
    _record(tmp_path, "old", pid=0)
    assert [r.run_id for r in live_runs(runs_dir=tmp_path)] == ["old"]


def test_a_reaped_run_is_gone_because_the_engine_moved_it(tmp_path: Path) -> None:
    """The archive lives in terminal/, so globbing finds only live runs."""
    (tmp_path / "terminal").mkdir()
    _record(tmp_path / "terminal", "finished")
    assert live_runs(runs_dir=tmp_path) == []


def test_the_token_is_read_from_the_file_beside_the_record(tmp_path: Path) -> None:
    (tmp_path / "dashboard-50000.token").write_text("s3cret\n")
    assert token_for(50000, runs_dir=tmp_path) == "s3cret"


def test_the_environment_overrides_the_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dashboard-50000.token").write_text("from-file")
    monkeypatch.setenv("CONDUCTOR_GATE_TOKEN", "from-env")
    assert token_for(50000, runs_dir=tmp_path) == "from-env"


def test_records_are_found_where_conductor_home_puts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine honours $CONDUCTOR_HOME for run records, though not for tokens."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    (tmp_path / "runs").mkdir()
    _record(tmp_path / "runs", "elsewhere", port=50001)
    assert [r.run_id for r in live_runs()] == ["elsewhere"]


def test_a_missing_token_is_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading state needs none, so a listener still works without one."""
    monkeypatch.delenv("CONDUCTOR_GATE_TOKEN", raising=False)
    assert token_for(50000, runs_dir=tmp_path) is None


def test_an_event_reaches_a_caller_as_engine_neutral_facts() -> None:
    """Nothing above the backend reads an engine's payload keys."""
    event = SignalEvent(
        signal=RunSignal.DECISION_NEEDED,
        run_id="abc",
        workflow="w",
        at=1.0,
        event_type="gate_presented",
        step="approve",
    )
    assert not event.ends_the_run
    assert event.step == "approve"
    assert not hasattr(event, "data")
