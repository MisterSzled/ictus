"""Finding a run that is happening, and getting permission to talk to it.

Named ``live`` rather than ``runs`` because ``ictus.runs`` is a different thing
one layer up — *acting* on a run, in no engine's vocabulary. This is Conductor's
own record of what is running on this machine, and reading it needs to know
where Conductor puts its files.

Conductor writes one record per run under ``~/.conductor/runs/`` and moves it
into ``terminal/`` on a graceful exit only, so the pid is checked rather than
the directory trusted. Nothing filters on age.

Two directories: run records come from ``fleet.records.run_records_dir``,
which honours ``$CONDUCTOR_HOME``, and a dashboard's token from
``rundir.runs_dir``, which does not.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LOOPBACK",
    "TOKENS_DIR",
    "TOKEN_ENV",
    "LiveRun",
    "alive",
    "live_runs",
    "records_dir",
    "token_for",
]

#: Where a dashboard's token is written. Not configurable in the engine.
TOKENS_DIR = Path.home() / ".conductor" / "runs"


def records_dir() -> Path:
    """Where the engine writes one record per run: ``$CONDUCTOR_HOME/runs``, else home's."""
    home = os.environ.get("CONDUCTOR_HOME")
    return (Path(home) if home else Path.home() / ".conductor") / "runs"


#: Overrides the per-run minted token, and is what the engine checks first.
TOKEN_ENV = "CONDUCTOR_GATE_TOKEN"

#: How anything here reaches a dashboard: directly, never through a proxy.
#: urllib's default opener honours ``$http_proxy`` for 127.0.0.1 unless
#: ``no_proxy`` lists that literal address.
LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True, slots=True)
class LiveRun:
    """A run serving a dashboard, as its own record describes it."""

    run_id: str
    workflow: str
    port: int
    pid: int
    started_at: str
    event_log: Path | None = None
    """Where the engine is writing this run's JSONL, read from the record.

    The filename's timestamp differs from ``started_at``, so it cannot be
    reconstructed.
    """

    @property
    def dashboard(self) -> str:
        """The dashboard's base URL. Loopback — the engine binds 127.0.0.1."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def socket_url(self) -> str:
        """Where the live event stream is served."""
        return f"ws://127.0.0.1:{self.port}/ws"


def alive(pid: int) -> bool:
    """Whether a process is still there. Signal 0 checks without delivering one."""
    if pid <= 0:
        return True  # nothing recorded, so nothing to disprove
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but a process
    return True


def live_runs(*, runs_dir: Path | None = None) -> list[LiveRun]:
    """Every run currently serving a dashboard, oldest first.

    A record with no port is a foreground run: no socket to attach to and no
    way to answer its gates. The pid is checked, since a killed or crashed run
    leaves its record behind.
    """
    found: list[LiveRun] = []
    for path in sorted((runs_dir or records_dir()).glob("*.json")):
        record = _read(path)
        if record is None:
            continue
        port = record.get("port")
        run_id = record.get("run_id")
        if not isinstance(port, int) or not isinstance(run_id, str):
            continue
        log = record.get("event_log_path")
        pid = record.get("pid")
        if not alive(pid if isinstance(pid, int) else 0):
            continue
        found.append(
            LiveRun(
                run_id=run_id,
                workflow=str(record.get("workflow_name", "")),
                port=port,
                pid=pid if isinstance(pid, int) else 0,
                started_at=str(record.get("started_at", "")),
                event_log=Path(log) if isinstance(log, str) and log else None,
            )
        )
    return sorted(found, key=lambda run: run.started_at)


def token_for(port: int, *, runs_dir: Path | None = None) -> str | None:
    """The token a dashboard will accept, or ``None`` if none can be found.

    Needed for the socket handshake and for anything that changes the run;
    reading state needs none, so this returns rather than raises.
    """
    override = os.environ.get(TOKEN_ENV)
    if override:
        return override
    path = (runs_dir or TOKENS_DIR) / f"dashboard-{port}.token"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read(path: Path) -> dict[str, object] | None:
    """One run record, or ``None`` if it cannot be read.

    A record half-written while this reads it is normal.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None
