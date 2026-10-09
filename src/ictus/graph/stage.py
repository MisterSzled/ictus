"""Stages — reusable collections of nodes.

A stage is a whole workflow: its own entry point, graph, loops and gates.
Placing one emits a second YAML file plus a ``type: workflow`` agent that
references it, which is Conductor's only nesting construct.

The body's ``declare_input`` calls become the child's ``workflow.input`` and
its ``expose_output`` calls the child's ``output:``. ictus keeps the port
types on both sides; Conductor's ``output:`` map is ``dict[str, str]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.node import SubGraphNode
from ictus.graph.pipeline import Pipeline

if TYPE_CHECKING:
    from ictus.graph.ports import InputPort, OutputPort

__all__ = [
    "Stage",
]


class Stage:
    """A named, reusable sub-graph that compiles to its own workflow file."""

    def __init__(
        self,
        *,
        stage_id: str,
        description: str = "",
        loop_passes: int | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.body = Pipeline(
            pipeline_id=stage_id,
            description=description,
            loop_passes=loop_passes,
            max_iterations=max_iterations,
        )

    @property
    def stage_id(self) -> str:
        """The stage's identifier, and the basename of its emitted file."""
        return self.body.pipeline_id

    @property
    def description(self) -> str:
        """Human description, forwarded to the emitted workflow."""
        return self.body.description

    @property
    def input_ports(self) -> tuple[InputPort, ...]:
        """The stage's parameters, as ports a parent can wire into."""
        return self.body.declared_input_ports

    @property
    def output_ports(self) -> tuple[OutputPort, ...]:
        """The stage's results, as ports a parent can wire from."""
        return self.body.exposed_output_ports

    def instantiate(
        self,
        parent: Pipeline,
        *,
        node_id: str | None = None,
        description: str = "",
        max_depth: int | None = None,
    ) -> SubGraphNode:
        """Place this stage into ``parent`` and return the node standing for it.

        The node carries the stage's contract as ordinary ports.
        """
        if not self.output_ports and not self.input_ports:
            raise CompositionError(
                f"stage {self.stage_id!r} exposes no inputs and no outputs; "
                "declare_input()/expose_output() on its body give it a contract, "
                "without which it cannot be wired to anything"
            )
        node = SubGraphNode(
            node_id=node_id or self.stage_id,
            description=description or self.description,
            inputs=self.input_ports,
            declared_outputs=self.output_ports,
            target=f"./{self.stage_id}.yaml",
            max_depth=max_depth,
        )
        parent.add_subgraph(node, self.body)
        return node
