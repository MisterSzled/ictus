"""Lowering a pipeline to Conductor's ``workflow:`` block.

Every default Conductor would otherwise apply silently is written out here:
the provider (which defaults to copilot), the iteration bound (which defaults to
10 total steps and would stop a loop midway), the context mode (which defaults
to ``accumulate``, under which the declared ``input:`` graph is parsed and never
consulted), and checkpointing, which the engine otherwise does only when a step
raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict

from ictus.errors import EmitError
from ictus.interfaces.conductor.mcp import mcp_servers_block

__all__ = ["NOTHING_INHERITED", "Inherited", "workflow_block"]

# Conductor's own default when the workflow omits a provider.
DEFAULT_PROVIDER = "copilot"

# Conductor counts every step execution against one global budget, and caps it.
ITERATION_CEILING = 500


def max_iterations(pipeline: Pipeline) -> int:
    """Price the graph in the unit Conductor charges: total step executions.

    A loop costs its body length on every pass, so the bound has to be derived
    from the shape of the graph. Conductor's default is 10, which stops a
    six-node pipeline with one review loop midway through its second pass — at a
    number nobody chose.
    """
    if pipeline.max_iterations is not None:
        return _within_ceiling(pipeline, pipeline.max_iterations)
    pipeline.require_loop_bound()
    # Conductor charges a group's whole fan-out against this budget and records
    # a map group's cost as the number of items it actually spawned, which is
    # what `budget_cost` prices. Derived there rather than here so the start
    # gate quotes the same number this compiles in.
    return _within_ceiling(pipeline, pipeline.budget_cost())


def _within_ceiling(pipeline: Pipeline, wanted: int) -> int:
    """Refuse a budget Conductor cannot hold, rather than quietly truncating it.

    Clamping looks harmless and is not: the group cost is charged after the work
    runs, so a truncated budget fails on the *next* node, having already paid for
    everything before it.
    """
    if wanted > ITERATION_CEILING:
        raise EmitError(
            f"pipeline {pipeline.pipeline_id!r} needs {wanted} steps but Conductor caps "
            f"max_iterations at {ITERATION_CEILING}. Reduce loop_passes, split the graph "
            "into stages (a stage costs its caller one step), or set max_iterations "
            "explicitly and accept the truncation."
        )
    return wanted


@dataclass(frozen=True, slots=True)
class Inherited:
    """Runtime settings a nested workflow takes from the pipeline that hosts it.

    Conductor loads each ``type: workflow`` file on its own, so a child that
    names no provider gets Conductor's default rather than its parent's. That is
    how four of six emitted files ended up on ``copilot`` while their parents ran
    on ``claude-agent-sdk`` — a setting the author made once, silently ignored by
    everything nested inside it.

    Resolved when the file is written rather than when the stage is placed,
    because a parent's provider is often chosen after the stage is instantiated.
    """

    provider: str | None = None
    default_model: str | None = None
    system_prompt: str | None = None

    def under(self, parent: Pipeline) -> Inherited:
        """What a child of ``parent`` should inherit, parent's own choice first."""
        return Inherited(
            provider=parent.provider or self.provider,
            default_model=parent.default_model or self.default_model,
            system_prompt=parent.system_prompt or self.system_prompt,
        )


NOTHING_INHERITED = Inherited()


def workflow_block(pipeline: Pipeline, inherited: Inherited = NOTHING_INHERITED) -> YamlDict:
    block: YamlDict = {"name": pipeline.pipeline_id}
    if pipeline.description:
        block["description"] = pipeline.description
    block["version"] = pipeline.version
    if pipeline.instructions:
        block["instructions"] = list(pipeline.instructions)
    block["entry_point"] = pipeline.entry().node_id

    provider: YamlDict = {"name": pipeline.provider or inherited.provider or DEFAULT_PROVIDER}
    # Only when asked for. The engine refuses the field on any other provider,
    # and its own default is the same "none" — so saying it unprompted would be
    # noise in the diff and a validation error the moment somebody switched.
    if pipeline.native_tools == "claude_code":
        provider["native_tools"] = "claude_code"
    elif isinstance(pipeline.native_tools, tuple):
        # A list, so the step is given these and nothing else. Emitted as a
        # list because that is what the schema takes; a tuple is ictus's way of
        # keeping the pipeline hashable, not the engine's spelling.
        provider["native_tools"] = list(pipeline.native_tools)
    runtime: YamlDict = {"provider": provider}
    model = pipeline.default_model or inherited.default_model
    if model is not None:
        runtime["default_model"] = model
    servers = mcp_servers_block(pipeline)
    if servers:
        runtime["mcp_servers"] = servers
    # Unconditional. Conductor already checkpoints when a step raises, so what
    # this adds is the crash that raises nothing — a hung provider, a killed
    # process, a closed laptop — after which a run has no resume point at all.
    # `every_seconds` is deliberately not set alongside: the schema ignores it
    # whenever `every_agent` is true, and `keep_last` rotates the saves anyway.
    runtime["checkpoint"] = {"every_agent": True}
    block["runtime"] = runtime

    if pipeline.workflow_inputs:
        params: YamlDict = {}
        for param in pipeline.workflow_inputs:
            entry: YamlDict = {"type": param.port_type.value, "required": param.required}
            if param.default is not None:
                entry["default"] = param.default
            if param.description:
                entry["description"] = param.description
            params[param.name] = entry
        block["input"] = params

    context: YamlDict = {"mode": pipeline.context_mode}
    if pipeline.context_max_tokens is not None:
        context["max_tokens"] = pipeline.context_max_tokens
    if pipeline.context_trim is not None:
        context["trim_strategy"] = pipeline.context_trim.value
    block["context"] = context

    limits: YamlDict = {"max_iterations": max_iterations(pipeline)}
    if pipeline.timeout_seconds is not None:
        limits["timeout_seconds"] = pipeline.timeout_seconds
    if pipeline.budget_usd is not None:
        limits["budget_usd"] = pipeline.budget_usd
        limits["budget_mode"] = pipeline.budget_mode
    block["limits"] = limits

    if pipeline.metadata:
        block["metadata"] = dict(pipeline.metadata)
    return block
