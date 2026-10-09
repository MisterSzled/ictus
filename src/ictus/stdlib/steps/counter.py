"""A pass counter — the only thing that makes a loop's give-up routable.

``_run_child_engine`` catches an explicit termination and nothing else, so a
``MaxIterationsError`` escapes past every route its caller declared. A loop
that reports running out has to count its own passes and exit first.

Costs one iteration and no provider call. Three engine behaviours:

* The guard is on the node, not the value: on the first pass the whole step is
  absent, so ``| default(0)`` raises on the attribute access before the filter.
* A single ``value:`` is the bare scalar, addressed as ``n.output``.
* A single ``value:`` step must declare no ``output:`` schema.

``ComputeNode`` handles the last two; the first is here.
"""

from __future__ import annotations

from ictus.graph.node import ComputeNode
from ictus.graph.ports import InputPort, OutputPort, PortType

__all__ = ["COUNT", "counter"]

COUNT = "value"


def counter(*, node_id: str, description: str = "") -> ComputeNode:
    """Count how many times the run has reached this point, starting at one.

    Read with ``node.ref("value")`` and test with ``at_least``. Wire it to
    itself with ``feed(node, "value", node, node_id)``, which is what puts the
    previous value in scope under ``context.mode: explicit``.
    """
    return ComputeNode(
        node_id=node_id,
        description=description or "Which pass this is",
        value=(
            "{% if " + node_id + " is defined %}"
            "{{ (" + node_id + ".output | int) + 1 }}"
            "{% else %}1{% endif %}"
        ),
        value_type=PortType.NUMBER,
        inputs=(InputPort(node_id, PortType.NUMBER, "The previous pass", optional=True),),
        declared_outputs=(OutputPort(COUNT, PortType.NUMBER, "Pass number"),),
    )
