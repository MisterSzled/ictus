"""The stand-in for Slack is driven by the code that drives the real one.

`smoke/` is the one place in this repository `make soundcheck` never executes —
the Makefile says so, and says why: it holds a harness for a live engine, which
`pytest` cannot run, so type checking is its only gate. That is true of
`subscribe.py` and `check_jira.py`. It was taken to be true of
`fake_channel.py`, which is neither: it is a pure-standard-library stand-in for
Slack, and every smoke test built on it proves only as much as it resembles the
service.

It stopped resembling it. `conversations.history` was added to it stamping
messages from a counter starting at 2000.0, where Slack's `ts` is epoch
seconds — so `ictus-bridge overhear`, which starts an empty channel's cursor
from the clock, found every typed message to be older than the moment it began
listening and read none of them. mypy passed. The shape was right and the
contract was not, which is the one failure a type checker cannot see, and it
cost a live run to find.

So: the production reader against the stand-in, both directions. A field
renamed, an order reversed, a `ts` that is not a Slack `ts` — each breaks a
test here rather than a demo somebody is following.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ictus.bridge.slack.watch import latest, since
from ictus.notify.slack.api import endpoint, reply

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
STAND_IN = ROOT / "smoke" / "fake_channel.py"
CHANNEL = "C0PRETEND"
TOKEN = "xoxp-pretend"


def _loaded() -> ModuleType:
    """`smoke/` is not a package, so the stand-in is loaded from its path.

    Importing it starts nothing: its server is behind `if __name__`.
    """
    spec = importlib.util.spec_from_file_location("fake_channel", STAND_IN)
    assert spec is not None and spec.loader is not None, f"cannot load {STAND_IN}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    module = _loaded()
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Slack)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "SLACK_API_URL", f"http://127.0.0.1:{server.server_port}/api/chat.postMessage"
    )
    yield module
    server.shutdown()


def _types(module: ModuleType, *lines: str) -> None:
    """Say each line into the channel, through the stand-in's own stdin reader.

    Not by appending to `SAID` directly: the stamp is the thing under test, and
    reaching past the code that writes it would test nothing.
    """
    before = len(module.SAID)
    original = sys.stdin
    sys.stdin = io.StringIO("".join(f"{line}\n" for line in lines))
    try:
        module._typing()
    finally:
        sys.stdin = original
    assert len(module.SAID) == before + len(lines), "the stand-in dropped a line"


def _history(*, oldest: str) -> list[dict[str, object]]:
    """One raw `conversations.history`, to see the order before anything sorts it."""
    url = f"{endpoint('conversations.history')}?channel={CHANNEL}&oldest={oldest}&limit=100"
    with urllib.request.urlopen(url, timeout=5) as answer:
        loaded = json.loads(answer.read())
    assert isinstance(loaded, dict) and loaded.get("ok"), f"the stand-in refused: {loaded}"
    found = loaded.get("messages")
    assert isinstance(found, list)
    return [one for one in found if isinstance(one, dict)]


def test_what_is_typed_is_what_the_listener_reads(stand_in: ModuleType) -> None:
    """The whole bug, in order: a cursor is fixed, then somebody types.

    With a counter-based `ts` the cursor is a wall clock far in the future of it,
    `since` finds nothing, and the listener sits silent while the demo's author
    concludes the feature does not work.
    """
    cursor = latest(TOKEN, CHANNEL)
    _types(stand_in, "New DB ticket raised: DB-8790 the nightly sync is dropping rows")
    heard = since(TOKEN, CHANNEL, cursor)
    assert [one["text"] for one in heard.messages] == [
        "New DB ticket raised: DB-8790 the nightly sync is dropping rows"
    ]


def test_the_stand_in_stamps_the_way_slack_does(stand_in: ModuleType) -> None:
    """Slack's `ts` is epoch seconds, and a listener starting on an empty channel
    has nothing but its own clock to start after. Any other scale is unreadable."""
    before = time.time()
    _types(stand_in, "hello")
    stamped = float(str(stand_in.SAID[-1]["ts"]))
    assert before <= stamped <= time.time() + 1


def test_an_empty_channel_gives_a_cursor_nothing_sorts_before(stand_in: ModuleType) -> None:
    """`latest` falls back to the clock here; the two scales have to be the same one."""
    assert not stand_in.SAID
    cursor = float(latest(TOKEN, CHANNEL))
    _types(stand_in, "said afterwards")
    assert float(str(stand_in.SAID[-1]["ts"])) > cursor


def test_the_cursor_moves_past_what_was_read(stand_in: ModuleType) -> None:
    """`oldest` is exclusive, so a second poll re-reads nothing."""
    cursor = latest(TOKEN, CHANNEL)
    _types(stand_in, "Start test run: once")
    first = since(TOKEN, CHANNEL, cursor)
    assert len(first.messages) == 1
    assert since(TOKEN, CHANNEL, first.cursor).messages == ()


def test_it_answers_newest_first_as_slack_does(stand_in: ModuleType) -> None:
    """`since` sorts what it collects rather than trusting the order, and this is
    the contract that makes that a defence rather than the only thing holding it."""
    cursor = latest(TOKEN, CHANNEL)
    _types(stand_in, "first", "second", "third")
    assert [one["text"] for one in _history(oldest=cursor)] == ["third", "second", "first"]
    assert [one["text"] for one in since(TOKEN, CHANNEL, cursor).messages] == [
        "first",
        "second",
        "third",
    ], "and the reader puts them back in the order they were said"


def test_it_still_answers_the_shape_a_run_reports_in(stand_in: ModuleType) -> None:
    """The older half of the stand-in's contract, and the one every smoke run
    depends on: a report has to come back with the `ts` it landed at, or a run
    cannot thread its next message under its own."""
    assert reply(token="xoxb-pretend", channel=CHANNEL, thread_ts="", text="a report") == ""
    assert stand_in.Slack.roots, "the stand-in recorded no thread for it"
