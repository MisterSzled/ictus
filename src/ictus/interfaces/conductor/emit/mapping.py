"""Lowering map groups to Conductor's top-level ``for_each:`` list.

A for-each group carries its body inline, so the body must be kept out of
``agents:`` entirely or Conductor schedules it once on its own as well.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.interfaces.conductor.emit.agents import agent_entry
from ictus.interfaces.conductor.emit.routes import route_entries
from ictus.interfaces.conductor.emit.templates import reference_path

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["for_each_block"]

# The template keeps its `name`, which Conductor requires, but it is not a
# step in the graph: no routes, and nothing to bind a parent's inputs to.
_NOT_ON_A_TEMPLATE = ("routes", "input_mapping")


def for_each_block(pipeline: Pipeline) -> list[YamlValue]:
    groups: list[YamlValue] = []
    for group in pipeline.maps:
        template = agent_entry(pipeline, group.body)
        for key in _NOT_ON_A_TEMPLATE:
            template.pop(key, None)
        entry: YamlDict = {
            "name": group.group_id,
            "type": "for_each",
            "source": reference_path(pipeline, group.source),
            "as": group.item.name,
            "agent": template,
            "max_concurrent": group.max_concurrent,
        }
        if group.description:
            entry["description"] = group.description
        if group.failure_mode is not None:
            entry["failure_mode"] = group.failure_mode.value
        if group.key_by is not None:
            entry["key_by"] = f"{group.item.name}.{group.key_by}"
        routes = route_entries(pipeline, group)
        if routes:
            entry["routes"] = routes
        groups.append(entry)
    return groups
