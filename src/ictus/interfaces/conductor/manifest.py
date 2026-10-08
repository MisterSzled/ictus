"""What a listener needs to start a run, rendered beside the workflow.

A pipeline's ``listen_on`` compiles to one of these. It names the workflow to
run, what marks a message as a request, which inputs to pass, and what the
environment has to supply before any of it is worth attempting.

JSON rather than a key in the workflow, for one reason: a process that only
starts runs should not have to parse Conductor's schema to find out it was
asked to. Reading this needs ``json`` and nothing else, so a listener can be a
small program next to ``conductor`` rather than a second copy of ictus.

``requires`` is the half that would otherwise be lost. ``require_executable``
and an integration's ``env`` are checked by ``ictus preflight``, which is a
command and not an artifact — ship only the workflow and a missing credential
stops being a refusal before launch and becomes a script node failing after a
gate, having already paid for the step in front of it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ictus.graph.pipeline import Listener, Pipeline

__all__ = ["VERSION", "filename_for", "render"]

#: Bumped when a reader written against the old shape would misread the new one.
VERSION = 1

#: What ``filename_for`` appends. Distinct from ``.yaml`` so the validator's
#: glob keeps picking up workflows and only workflows.
SUFFIX = ".listen.json"


def filename_for(pipeline: Pipeline) -> str:
    """Where this pipeline's manifest goes, beside its workflow."""
    return f"{pipeline.pipeline_id}{SUFFIX}"


def render(pipeline: Pipeline) -> str:
    """The manifest for ``pipeline``, or ``""`` when nothing starts it.

    One file per pipeline rather than one per listener: a run is started once
    however many services could have asked for it, and a directory of these is
    what a single listener reads to serve many pipelines at once.
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
    """One way in: what to match, and which inputs the match fills."""
    inputs = {"question": one.into.name}
    if one.thread is not None:
        inputs["thread"] = one.thread.name
    return {
        "service": one.service.name,
        "prefix": one.prefix,
        "inputs": inputs,
    }


def _requires(pipeline: Pipeline) -> dict[str, object]:
    """What must be true of the environment before a run is worth starting.

    Deduplicated by name: a variable named by two integrations is one variable,
    and a listener reporting it twice reads as two problems.
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
