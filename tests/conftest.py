"""Shared fixtures.

``collector`` is a stand-in Slack that records what arrives. It is here rather
than in one test module because two need it: ``test_slack_program.py`` drives
the sending program against it, and ``test_notify.py`` checks that the watcher
reaches the same program the same way.

The backend's own validator is the checker, not a schema model:
``WorkflowConfig.model_validate`` accepts a dangling ``options[].route``, which
is how every gate edge ictus emits is spelled.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar

import pytest

from ictus.interfaces.conductor import ConductorBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from ictus.graph.pipeline import Pipeline


@pytest.fixture(scope="session")
def backend() -> ConductorBackend:
    """The backend under test. Fails hard rather than skipping when its CLI is absent."""
    if shutil.which("conductor") is None:
        pytest.fail("conductor is not on PATH; the conformance suite cannot verify output")
    return ConductorBackend()


@pytest.fixture
def validates(backend: ConductorBackend, tmp_path: Path) -> Callable[[Pipeline], None]:
    """Assert a pipeline and every stage it contains pass the backend's validator."""

    def _check(pipeline: Pipeline) -> None:
        for document in backend.compile(pipeline):
            (tmp_path / document.filename).write_text(document.content)
        entry = tmp_path / f"{pipeline.pipeline_id}.yaml"
        results = backend.validate([entry])
        bad = [r for r in results if not r.ok]
        assert not bad, (
            f"{backend.capabilities().name} rejected {pipeline.pipeline_id}:\n"
            f"{bad[0].detail}\n--- emitted ---\n{entry.read_text()}"
        )

    return _check


SECRET = "/services/T000/B000/sup3rs3cr3t"


class _Collector(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, dict[str, object]]]] = []
    headers_seen: ClassVar[list[dict[str, str]]] = []
    status: ClassVar[int] = 200
    reply: ClassVar[dict[str, object]] = {"ok": True, "ts": "1700000000.000100"}
    hang: ClassVar[float] = 0.0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).received.append((self.path, body))
        type(self).headers_seen.append(dict(self.headers.items()))
        if type(self).hang:
            time.sleep(type(self).hang)
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps(type(self).reply).encode())

    def log_message(self, *_: object) -> None:
        """Silence; the assertions are the output."""


@pytest.fixture
def collector() -> Iterator[tuple[str, list[tuple[str, dict[str, object]]]]]:
    _Collector.received = []
    _Collector.headers_seen = []
    _Collector.status = 200
    _Collector.reply = {"ok": True, "ts": "1700000000.000100"}
    _Collector.hang = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}{SECRET}", _Collector.received
    server.shutdown()
