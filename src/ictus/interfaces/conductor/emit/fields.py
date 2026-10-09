"""The per-node-kind key spelling, and the output schema.

Type-shaped rather than graph-shaped: the whole of ``AgentDef``'s spelling
lives in ``kind_fields``, which is a declarative mapping written as code —
seventeen sequential ``if node.x is not None`` assignments for an agent alone.
Everything that reasons about edges is in ``routes.py``; everything that
assembles a whole entry is in ``agents.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import EmitError
from ictus.graph.composition import ABORT_CASE
from ictus.graph.node import (
    AgentNode,
    ComputeNode,
    GateNode,
    Node,
    NodeKind,
    QuestionsNode,
    ScriptNode,
    SubGraphNode,
    TerminateNode,
    WaitNode,
)
from ictus.interfaces.conductor.emit.routes import route_target
from ictus.interfaces.conductor.emit.templates import (
    reference_path,
    render,
    render_settled,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ictus.graph.node import Question
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.ports import OutputPort
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["CONDUCTOR_TYPE", "kind_fields", "render_output_schema"]


# The one place a neutral node kind becomes a Conductor type string.
CONDUCTOR_TYPE: dict[NodeKind, str] = {
    NodeKind.LLM_CALL: "agent",
    NodeKind.HUMAN_DECISION: "human_gate",
    NodeKind.SUBPROCESS: "script",
    NodeKind.COMPUTATION: "set",
    NodeKind.DELAY: "wait",
    NodeKind.EXIT: "terminate",
    NodeKind.SUB_GRAPH: "workflow",
    NodeKind.ASK: "questions",
}


def _question(pipeline: Pipeline, node: Node, question: Question) -> YamlDict:
    """One entry of a ``questions:`` list."""
    out: YamlDict = {"text": render(pipeline, node, question.text)}
    if question.id is not None:
        out["id"] = question.id
    if question.hint is not None:
        out["hint"] = render(pipeline, node, question.hint)
    if question.choices:
        out["choices"] = list(question.choices)
    if not question.allow_free_text:
        out["allow_free_text"] = False
    if question.default is not None:
        out["default"] = question.default
    if question.required:
        out["required"] = True
    if not question.multiline:
        out["multiline"] = False
    return out


def kind_fields(pipeline: Pipeline, node: Node, system_prompt: str | None) -> YamlDict:
    """Conductor's per-type keys for one node.

    The whole of ``AgentDef``'s spelling lives in this function. The graph
    carries these values as ordinary attributes with ordinary English names;
    which key each lands under, and whether it is emitted at all, is Conductor's
    business and nobody else's.
    """
    match node:
        case AgentNode():
            fields: YamlDict = {"prompt": render(pipeline, node, node.prompt)}
            # Falling back rather than omitting: an omitted system prompt is
            # not a default one, it is an empty one. The SDK turns None into
            # `--system-prompt ""`, so a step with nothing set runs with no
            # working discipline at all.
            baseline = node.system_prompt or system_prompt
            if baseline is not None:
                fields["system_prompt"] = baseline
            if node.model is not None:
                fields["model"] = node.model
            if node.provider is not None:
                fields["provider"] = node.provider
            if node.tools is not None:
                fields["tools"] = list(node.tools)
            if node.max_turns is not None:
                fields["max_agent_iterations"] = node.max_turns
            if node.reasoning is not None:
                fields["reasoning"] = {"effort": node.reasoning.value}
            if node.timeout_seconds is not None:
                fields["timeout_seconds"] = node.timeout_seconds
            if node.max_session_seconds is not None:
                fields["max_session_seconds"] = node.max_session_seconds
            if node.validator is not None:
                # `max_retries` is emitted either way rather than left to the
                # engine's default: it is the difference between two model calls
                # and three, which is not a thing to leave implicit.
                validator: YamlDict = {
                    "criteria": node.validator.criteria,
                    "max_retries": 1 if node.validator.revise else 0,
                }
                if node.validator.model is not None:
                    validator["model"] = node.validator.model
                fields["validator"] = validator
            if node.working_dir is not None:
                fields["working_dir"] = node.working_dir
            # Emitted for an empty tuple as well: `[]` is "deny every skill",
            # which is a different instruction from the omitted key's "take the
            # workflow's default set". The same three states as `tools`.
            if node.skills is not None:
                fields["skills"] = list(node.skills)
            if node.plugins is not None:
                fields["plugins"] = list(node.plugins)
            if node.retry is not None:
                # Conductor's spelling, not ours: `attempts` counts the first
                # try the same way `max_attempts` does, and an empty `on` leaves
                # the key off so the engine keeps its own categories.
                retry: YamlDict = {
                    "max_attempts": node.retry.attempts,
                    "backoff": node.retry.backoff.value,
                }
                if node.retry.first_delay_seconds is not None:
                    retry["delay_seconds"] = node.retry.first_delay_seconds
                if node.retry.on:
                    retry["retry_on"] = [category.value for category in node.retry.on]
                fields["retry"] = retry
            if node.context_tier is not None:
                fields["context_tier"] = node.context_tier.value
            if node.session_key is not None:
                fields["session_key"] = node.session_key
            if node.dialog_trigger is not None:
                fields["dialog"] = {"trigger_prompt": node.dialog_trigger}
            return fields
        case GateNode():
            return {"prompt": render(pipeline, node, node.prompt)}
        case ScriptNode():
            fields = {"command": node.command}
            if node.args:
                fields["args"] = [render(pipeline, node, arg) for arg in node.args]
            if node.env:
                fields["env"] = dict(node.env)
            if node.stdin is not None:
                fields["stdin"] = render(pipeline, node, node.stdin)
            if node.timeout is not None:
                fields["timeout"] = node.timeout
            if node.working_dir is not None:
                fields["working_dir"] = node.working_dir
            return fields
        case ComputeNode():
            fields = {}
            if node.value is not None:
                fields["value"] = node.value
            if node.values is not None:
                fields["values"] = dict(node.values)
            if node.value_type is not None:
                fields["output_type"] = node.value_type.value
            return fields
        case WaitNode():
            fields = {"duration": node.duration}
            if node.reason is not None:
                fields["reason"] = node.reason
            return fields
        case TerminateNode():
            fields = {"status": node.status, "reason": render(pipeline, node, node.reason)}
            if node.result is not None:
                fields["output_template"] = {
                    key: render_settled(pipeline, node, value) for key, value in node.result.items()
                }
            return fields
        case QuestionsNode():
            fields = dict(node.kind_flags())
            if node.source is not None:
                # A dotted path, not an interpolation: the engine resolves it
                # itself, so wrapping it in braces would hand it a literal.
                fields["source"] = reference_path(pipeline, node.source)
            else:
                fields["questions"] = [_question(pipeline, node, q) for q in node.questions]
            abort = next((e for e in pipeline.outgoing(node) if e.case == ABORT_CASE), None)
            if abort is not None:
                fields["abort_route"] = route_target(abort)
            return fields
        case SubGraphNode():
            fields = {"workflow": node.target}
            if node.max_depth is not None:
                fields["max_depth"] = node.max_depth
            return fields
        case _:
            raise EmitError(
                f"the Conductor backend has no lowering for {type(node).__name__}; "
                "every node kind needs an entry in kind_fields()"
            )


def render_output_schema(ports: Sequence[OutputPort]) -> YamlDict:
    """Lower output ports to Conductor's ``output:`` block.

    Here rather than in ``graph`` because that is what it is: a node's declared
    ports going in, Conductor's schema shape coming out. It sat in
    ``graph/node.py`` for a long time with that same docstring, which made the
    one package forbidden to know an engine the place the engine's output schema
    was built — and its only caller was always this file.
    """
    out: YamlDict = {}
    for port in ports:
        entry: dict[str, YamlValue] = {"type": port.port_type.value}
        if port.description:
            entry["description"] = port.description
        if port.element is not None:
            # The only thing that tells the model what keys each entry needs.
            entry["items"] = {
                "type": "object",
                "properties": {
                    name: {"type": port_type.value} for name, port_type in port.element.items()
                },
            }
        out[port.name] = entry
    return out
