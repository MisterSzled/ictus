"""Composition lints.

Two tiers:

* ``rules.py`` holds what is true of any graph — an unreachable node, a
  required input nothing feeds, a stage whose contract has drifted;
* engine-specific rules come from the backend.

Neither is visible to the executor's own validator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError, LintError
from ictus.graph.node import SubGraphNode
from ictus.graph.traversal import reachable_from_entry
from ictus.lint.rules import (
    capability_problems,
    describe,
    group_routing_problems,
    node_problems,
    placeholder_problems,
    previous_pass_problems,
    stage_contract_problems,
    undeclared_use_problems,
)

if TYPE_CHECKING:
    from ictus.graph.composition import RouteEnd
    from ictus.graph.pipeline import Pipeline
    from ictus.interfaces import Backend

__all__ = ["check", "lint_pipeline"]


def lint_pipeline(
    pipeline: Pipeline,
    *,
    backend: Backend | None = None,
    _seen: set[str] | None = None,
    _declared: frozenset[str] = frozenset(),
) -> list[str]:
    """Every violation in ``pipeline`` and its nested stages.

    Pass ``backend`` to add that engine's own rules; without one, graph-level
    rules only.
    """
    seen = _seen if _seen is not None else set()
    if pipeline.pipeline_id in seen:
        return []
    seen.add(pipeline.pipeline_id)

    where = pipeline.pipeline_id
    if not pipeline.nodes:
        return [f"{where}: pipeline has no nodes"]

    try:
        entry = pipeline.entry()
    except CompositionError as exc:
        return [f"{where}: {exc}"]

    problems: list[str] = placeholder_problems(pipeline, where)
    problems.extend(previous_pass_problems(pipeline, where))
    problems.extend(undeclared_use_problems(pipeline, where, _declared))
    reachable = reachable_from_entry(pipeline)
    problems.extend(
        f"{where}: {describe(node)} is unreachable from entry point "
        f"{entry.node_id!r} and will never run"
        for node in pipeline.nodes
        if node.node_id not in reachable
    )
    for node in pipeline.nodes:
        problems.extend(node_problems(pipeline, node, where))
    collections: tuple[RouteEnd, ...] = (*pipeline.groups, *pipeline.maps)
    for group in collections:
        problems.extend(group_routing_problems(pipeline, group, where))

    # A stage is part of its caller's run, so what the caller announced is
    # announced for it too.
    visible = _declared | {service.name for service in pipeline.integrations}
    visible |= {source.name for source in pipeline.datasources}
    by_id = {n.node_id: n for n in pipeline.nodes}
    for host_id, child in pipeline.children.items():
        host = by_id.get(host_id)
        if isinstance(host, SubGraphNode):
            problems.extend(stage_contract_problems(where, host, child))
        problems.extend(lint_pipeline(child, backend=backend, _seen=seen, _declared=visible))

    if backend is not None:
        problems.extend(capability_problems(pipeline, backend.capabilities(), where))
        problems.extend(backend.lint(pipeline))
    return problems


def check(pipeline: Pipeline, *, backend: Backend | None = None) -> None:
    """Raise ``LintError`` if ``pipeline`` violates any composition rule."""
    problems = lint_pipeline(pipeline, backend=backend)
    if problems:
        raise LintError(problems)
