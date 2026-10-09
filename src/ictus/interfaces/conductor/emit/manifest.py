"""What a listener needs to start a run, rendered beside the workflow.

A pipeline's ``listen_on`` compiles to one of these: the workflow to run, what
marks a message as a request, which inputs to pass, and what the environment
must supply.

JSON rather than a key in the workflow, so reading it needs ``json`` and not
Conductor's schema.

``requires`` carries what ``ictus preflight`` would check, since preflight is
a command and not an artifact.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ictus.graph.composition import Listener
    from ictus.graph.pipeline import Pipeline

__all__ = ["SUFFIX", "VERSION", "filename_for", "render"]

#: Bumped when a reader written against the old shape would misread the new one.
VERSION = 1

#: What ``filename_for`` appends. Not ``.yaml``, so the validator's glob
#: keeps picking up workflows only.
SUFFIX = ".listen.json"


def filename_for(pipeline: Pipeline) -> str:
    """Where this pipeline's manifest goes, beside its workflow."""
    return f"{pipeline.pipeline_id}{SUFFIX}"


def render(pipeline: Pipeline) -> str:
    """The manifest for ``pipeline``, or ``""`` when nothing starts it.

    One file per pipeline, not per listener: a run is started once however
    many services could have asked for it.
    """
    if not pipeline.listeners:
        return ""
    document = {
        "manifest": VERSION,
        "pipeline": pipeline.pipeline_id,
        "workflow": f"{pipeline.pipeline_id}.yaml",
        "description": pipeline.description,
        "workspace_instructions": pipeline.workspace_instructions,
        "listeners": [_listener(one) for one in pipeline.listeners],
        "requires": _requires(pipeline),
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _listener(one: Listener) -> dict[str, object]:
    """One way in: what to match, and which inputs the match fills.

    ``service`` is where the run reports back, and ``""`` when it reports
    nowhere. No listener reads it — a prefix is what claims a message — so it
    is here to be read by a person looking at the file.
    """
    inputs = {"question": one.into.name}
    if one.thread is not None:
        inputs["thread"] = one.thread.name
    return {
        "service": one.service.name if one.service is not None else "",
        "prefix": one.prefix,
        "inputs": inputs,
    }


def _requires(pipeline: Pipeline) -> dict[str, object]:
    """What must be true of the environment before a run is worth starting.

    Deduplicated by name.
    """
    env: dict[str, str] = {}
    for service in pipeline.all_integrations():
        for variable in service.required_env:
            env.setdefault(variable.name, variable.purpose)
    for server in pipeline.all_mcp_servers():
        for variable in server.required_env:
            env.setdefault(variable.name, variable.purpose)
    for source in pipeline.all_datasources():
        for variable in source.required_env:
            env.setdefault(variable.name, variable.purpose)
    return {
        "commands": [
            {"name": tool.name, "purpose": tool.purpose}
            for tool in sorted(pipeline.all_executables(), key=lambda t: t.name)
        ],
        "env": [{"name": name, "purpose": purpose} for name, purpose in sorted(env.items())],
    }
