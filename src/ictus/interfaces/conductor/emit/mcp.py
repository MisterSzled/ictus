"""MCP servers, as Conductor spells them in a workflow.

Only the rendering. Whether this machine can actually reach any of them is
``../preflight.py``: that half opens sockets and runs subprocesses, this half is
a pure function of the pipeline, and they were one file whose own docstring had
to say "emitting them, and checking the environment can supply them".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.graph.requirements import McpTransport

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict

__all__ = ["mcp_servers_block"]


def mcp_servers_block(pipeline: Pipeline) -> YamlDict:
    """Render the declared servers into Conductor's ``mcp_servers`` mapping."""
    block: YamlDict = {}
    for server in pipeline.mcp_servers:
        entry: YamlDict = {"type": server.transport.value}
        if server.transport is McpTransport.STDIO:
            entry["command"] = server.command
            if server.args:
                entry["args"] = list(server.args)
            if server.env:
                # The reference, never the value, with an empty default:
                # Conductor's loader treats an unexpandable bare ${VAR} as a
                # hard error, so the artifact would not validate offline.
                entry["env"] = {var.name: "${" + var.name + ":-}" for var in server.env}
        else:
            entry["url"] = server.url
            if server.headers:
                entry["headers"] = dict(server.headers)
        if server.timeout_ms is not None:
            entry["timeout"] = server.timeout_ms
        if server.tools:
            entry["tools"] = list(server.tools)
        block[server.name] = entry
    return block
