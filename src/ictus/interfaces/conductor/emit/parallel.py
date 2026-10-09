"""Lowering parallel groups to Conductor's top-level ``parallel:`` list.

Members stay ordinary ``agents:`` entries and are listed by name here. They
carry no routes of their own; Conductor rejects a member that does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.interfaces.conductor.emit.routes import route_entries

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["parallel_block"]


def parallel_block(pipeline: Pipeline) -> list[YamlValue]:
    groups: list[YamlValue] = []
    for group in pipeline.groups:
        entry: YamlDict = {
            "name": group.group_id,
            "agents": [member.node_id for member in group.members],
            "failure_mode": group.failure_mode.value,
        }
        if group.description:
            entry["description"] = group.description
        routes = route_entries(pipeline, group)
        if routes:
            entry["routes"] = routes
        groups.append(entry)
    return groups
