"""Lowering a pipeline to Conductor's ``workflow:`` block.

Every default Conductor would apply silently is written out: the provider
(else copilot), the iteration bound (else 10 total steps), the context mode
(else ``accumulate``, which ignores the declared ``input:`` graph), and
checkpointing (else only when a step raises).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict

from ictus.errors import EmitError
from ictus.graph.traversal import budget_cost, require_loop_bound
from ictus.interfaces.conductor.emit.mcp import mcp_servers_block

__all__ = ["DEFAULT_PROVIDER", "NOTHING_INHERITED", "Inherited", "workflow_block"]

# Conductor's own default when the workflow omits a provider.
DEFAULT_PROVIDER = "copilot"

# Conductor counts every step execution against one global budget, and caps it.
ITERATION_CEILING = 500


def max_iterations(pipeline: Pipeline) -> int:
    """Price the graph in the unit Conductor charges: total step executions.

    A loop costs its body length on every pass, so the bound is derived from
    the shape of the graph. Conductor's own default is 10.
    """
    if pipeline.max_iterations is not None:
        return _within_ceiling(pipeline, pipeline.max_iterations)
    require_loop_bound(pipeline)
    # Priced by `budget_cost` rather than here, so the start gate quotes the
    # same number this compiles in.
    return _within_ceiling(pipeline, budget_cost(pipeline))


def _within_ceiling(pipeline: Pipeline, wanted: int) -> int:
    """Refuse a budget Conductor cannot hold, rather than quietly truncating it.

    A group's cost is charged after the work runs, so a truncated budget fails
    on the next node having already paid for everything before it.
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

    Conductor loads each ``type: workflow`` file on its own, so a child naming
    no provider would get Conductor's default rather than its parent's.
    Resolved when the file is written, not when the stage is placed.
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
    # Only when asked for: the engine refuses the field on any other provider,
    # and its own default is the same "none".
    if pipeline.native_tools == "claude_code":
        provider["native_tools"] = "claude_code"
    elif isinstance(pipeline.native_tools, tuple):
        # These and nothing else. A list because that is what the schema takes.
        provider["native_tools"] = list(pipeline.native_tools)
    runtime: YamlDict = {"provider": provider}
    model = pipeline.default_model or inherited.default_model
    if model is not None:
        runtime["default_model"] = model
    servers = mcp_servers_block(pipeline)
    if servers:
        runtime["mcp_servers"] = servers
    # Unconditional: the engine otherwise checkpoints only when a step raises,
    # which leaves no resume point after a crash that raises nothing.
    # `every_seconds` is ignored whenever `every_agent` is true.
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
