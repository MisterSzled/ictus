"""Whether this machine can actually reach the MCP servers a pipeline declares.

The other half of what ``mcp.py`` used to be. This one opens sockets, runs
subprocesses and takes network timeouts; the rendering half is a pure function
and lives with the rest of emission in ``emit/mcp.py``. Two phases, two files.

An offline pass first — a missing variable or a command not on ``PATH`` needs no
network and is the commonest answer. The probe is behind a flag because it costs
a connection per server, and it buys the case the offline pass cannot see: an
endpoint that is reachable and refuses the credential.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from ictus.graph.requirements import McpTransport
from ictus.interfaces import PreflightIssue

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.requirements import McpServer

__all__ = ["preflight_issues"]

PROBE_TIMEOUT_SECONDS = 5.0

# The opening message of the MCP handshake. A 401 to it is a credential
# problem, which an offline check cannot see.
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "ictus-preflight", "version": "0"},
    },
}


def preflight_issues(pipeline: Pipeline, *, probe: bool) -> list[PreflightIssue]:
    """Everything about this environment that would stop the pipeline running."""
    issues: list[PreflightIssue] = []
    for server in pipeline.all_mcp_servers():
        issues.extend(_offline(server))
        if probe:
            issues.extend(_probe(server))
    return issues


def _offline(server: McpServer) -> list[PreflightIssue]:
    """Checks that need no network and no subprocess."""
    issues: list[PreflightIssue] = []
    for var in server.required_env:
        if not os.environ.get(var.name):
            purpose = f" ({var.purpose})" if var.purpose else ""
            issues.append(
                PreflightIssue(
                    requirement=f"mcp:{server.name}",
                    problem=f"environment variable {var.name} is not set{purpose}",
                    remedy=server.setup_hint or f"export {var.name}=... before launching",
                )
            )
    missing_command = (
        server.transport is McpTransport.STDIO
        and bool(server.command)
        and shutil.which(server.command or "") is None
        and not Path(server.command or "").exists()
    )
    if missing_command:
        issues.append(
            PreflightIssue(
                requirement=f"mcp:{server.name}",
                problem=f"command {server.command!r} is not on PATH",
                remedy=server.setup_hint or f"install {server.command!r} or fix PATH",
            )
        )
    return issues


def _probe(server: McpServer) -> list[PreflightIssue]:
    """Open the connection and speak the first word of the protocol."""
    if server.transport is McpTransport.STDIO:
        return _probe_stdio(server)
    return _probe_http(server)


def _probe_stdio(server: McpServer) -> list[PreflightIssue]:
    if not server.command or shutil.which(server.command) is None:
        return []  # already reported by the offline check
    command = [server.command, *server.args]
    try:
        done = subprocess.run(
            command,
            input=json.dumps(_INITIALIZE) + "\n",
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A server that stays open waiting for more input is behaving correctly.
        return []
    except OSError as exc:
        return [_issue(server, f"could not start {server.command!r}: {exc}")]
    if done.returncode != 0 and not done.stdout.strip():
        detail = (done.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {done.returncode}"
        return [_issue(server, f"{server.command!r} exited without answering: {tail}")]
    return []


def _probe_http(server: McpServer) -> list[PreflightIssue]:
    if not server.url:
        return []
    request = urllib.request.Request(
        server.url,
        data=json.dumps(_INITIALIZE).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **{k: os.path.expandvars(v) for k, v in server.headers.items()},
        },
        method="POST" if server.transport is McpTransport.HTTP else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS):
            return []
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return [
                _issue(
                    server,
                    f"{server.url} refused the handshake with HTTP {exc.code}; "
                    "the endpoint is reachable but the credential was rejected",
                )
            ]
        if exc.code < 500:
            return []  # answered, just not to this shape — it is alive
        return [_issue(server, f"{server.url} returned HTTP {exc.code}")]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [_issue(server, f"{server.url} is unreachable: {exc}")]


def _issue(server: McpServer, problem: str) -> PreflightIssue:
    return PreflightIssue(
        requirement=f"mcp:{server.name}",
        problem=problem,
        remedy=server.setup_hint or f"check the configuration for {server.name!r}",
    )
