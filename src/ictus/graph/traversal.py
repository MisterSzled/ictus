"""Walking a finished graph: what reaches what, where it loops, what it costs.

Every function here is read-only. Nothing takes a lock on a ``Pipeline``'s
private state and nothing may: the rule is that these reach a graph only
through its public accessors, and ``tests/test_boundaries.py`` asserts it. That
is the whole reason they are functions in their own module rather than methods
— analysis that cannot mutate is analysis you can read without checking.

**There are three successor relations and they differ on purpose.** Merging any
two double-counts a parallel group or loses a cycle:

* ``_successors`` descends into a group's members *and* ascends to the
  enclosing group, because reachability has to see both.
* ``_route_successors`` only ascends, because ``step_cost`` has already priced
  every member of the group it would otherwise descend into.
* ``back_edges`` uses raw ``Pipeline.outgoing`` and does neither, because a
  cycle is closed by a control edge and a group is one routing endpoint.

**The accessors copy.** ``pipeline.edges`` and ``pipeline.nodes`` build a fresh
tuple on every access — ``p.edges is p.edges`` is ``False`` — so every walk
below reads them once at the top and threads the result down. ``_cycle_span``
can make ``PATH_BUDGET`` visits, and re-reading inside that loop would rebuild
the edge list twenty thousand times.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.composition import Edge, ParallelGroup, RouteEnd, _End
from ictus.graph.mapping import MapGroup
from ictus.graph.node import Node

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ictus.graph.pipeline import Pipeline

__all__ = [
    "PATH_BUDGET",
    "back_edges",
    "budget_cost",
    "has_cycle",
    "longest_cycle_length",
    "loop_cost",
    "may_be_unresolved",
    "reachable_from_entry",
    "reaches",
    "require_loop_bound",
    "step_cost",
    "total_cost",
]

#: Past this many visits ``_cycle_span`` falls back to the whole graph's cost,
#: which over-prices rather than under-prices.
PATH_BUDGET = 20_000


def _successors(pipeline: Pipeline, end: RouteEnd, edges: Sequence[Edge]) -> Iterable[RouteEnd]:
    """What runs after ``end``. Entering a group reaches its members."""
    if isinstance(end, ParallelGroup):
        yield from end.members
    if isinstance(end, MapGroup):
        yield end.body
    for edge in edges:
        if edge.source is end and not isinstance(edge.target, _End):
            yield edge.target
    if isinstance(end, Node):
        enclosing: RouteEnd | None = pipeline.group_of(end) or pipeline.map_of(end)
        if enclosing is not None:
            # A member continues wherever the group routes.
            for edge in edges:
                if edge.source is enclosing and not isinstance(edge.target, _End):
                    yield edge.target


def _route_successors(
    pipeline: Pipeline, end: RouteEnd, edges: Sequence[Edge]
) -> Iterable[RouteEnd]:
    """What runs after ``end``, following routes only.

    Does not descend into a group's members; the group's own cost covers them.
    """
    source = pipeline.group_of(end) or pipeline.map_of(end) if isinstance(end, Node) else None
    for edge in edges:
        if edge.source is (source or end) and not isinstance(edge.target, _End):
            yield edge.target


def reaches(pipeline: Pipeline, start: RouteEnd, goal: RouteEnd) -> bool:
    """Whether ``goal`` is reachable from ``start``."""
    edges = pipeline.edges
    seen: set[str] = set()
    stack: list[RouteEnd] = [start]
    while stack:
        current = stack.pop()
        if current.node_id in seen:
            continue
        seen.add(current.node_id)
        for nxt in _successors(pipeline, current, edges):
            if nxt is goal:
                return True
            stack.append(nxt)
    return False


def reachable_from_entry(pipeline: Pipeline) -> set[str]:
    """The id of everything the run can get to, starting where it starts."""
    edges = pipeline.edges
    start = pipeline.entry()
    seen: set[str] = {start.node_id}
    stack: list[RouteEnd] = [start]
    while stack:
        for nxt in _successors(pipeline, stack.pop(), edges):
            if nxt.node_id not in seen:
                seen.add(nxt.node_id)
                stack.append(nxt)
    return seen


def back_edges(pipeline: Pipeline) -> list[Edge]:
    """Control edges that close a cycle: edges into a node still on the DFS stack."""
    state: dict[str, int] = {}
    found: list[Edge] = []

    def visit(node: RouteEnd) -> None:
        state[node.node_id] = 1
        for edge in pipeline.outgoing(node):
            # A group is a routing endpoint like a node.
            if isinstance(edge.target, _End):
                continue
            mark = state.get(edge.target.node_id, 0)
            if mark == 1:
                found.append(edge)
            elif mark == 0:
                visit(edge.target)
        state[node.node_id] = 2

    nodes = pipeline.nodes
    roots: list[RouteEnd] = [pipeline.entry(), *nodes] if nodes else []
    for node in roots:
        if state.get(node.node_id, 0) == 0:
            visit(node)
    return found


def may_be_unresolved(pipeline: Pipeline, source: RouteEnd, target: RouteEnd) -> bool:
    """Whether ``target`` can run before ``source`` has produced anything.

    True when ``target`` is reachable from the entry without passing through
    ``source``, which is when a reference to it must be emitted optional.
    """
    start = pipeline.entry()
    if start is source:
        return False
    if start is target:
        return True

    # A group does not route onward until its members finish, so both the
    # member and its group are barriers.
    barriers = {source.node_id}
    if isinstance(source, Node):
        group = pipeline.group_of(source)
        if group is not None:
            barriers.add(group.group_id)

    # The entry can itself be the barrier; expanding it would walk past it.
    if start.node_id in barriers:
        return False

    edges = pipeline.edges
    seen: set[str] = {start.node_id}
    stack: list[RouteEnd] = [start]
    while stack:
        for nxt in _successors(pipeline, stack.pop(), edges):
            if nxt.node_id in barriers or nxt.node_id in seen:
                continue
            if nxt is target:
                return True
            seen.add(nxt.node_id)
            stack.append(nxt)
    return False


def has_cycle(pipeline: Pipeline) -> bool:
    """Whether any control edge closes a loop."""
    return bool(back_edges(pipeline))


def require_loop_bound(pipeline: Pipeline) -> None:
    """Refuse a cyclic graph that never says how many times it may go round."""
    if has_cycle(pipeline) and pipeline.loop_passes is None and pipeline.max_iterations is None:
        raise CompositionError(
            f"pipeline {pipeline.pipeline_id!r} contains a loop but sets no loop_passes; "
            "an unbounded loop has no safe default. Pass loop_passes=N (or an "
            "explicit max_iterations) so the bound is a decision, not an accident."
        )


def step_cost(end: RouteEnd) -> int:
    """How many step executions reaching ``end`` costs."""
    if isinstance(end, ParallelGroup):
        return len(end.members)
    if isinstance(end, MapGroup):
        return end.expect_items
    return 1


def total_cost(pipeline: Pipeline) -> int:
    """Every step in this graph, run once.

    Executions, not nodes: a parallel group costs one per member, a map
    group one per item.
    """
    groups, maps = pipeline.groups, pipeline.maps
    grouped = {m.node_id for g in groups for m in g.members}
    grouped |= {m.body.node_id for m in maps}
    loose = sum(1 for n in pipeline.nodes if n.node_id not in grouped)
    collections: tuple[RouteEnd, ...] = (*groups, *maps)
    return loose + sum(step_cost(g) for g in collections)


def budget_cost(pipeline: Pipeline) -> int:
    """Step executions this graph can reach, loops included.

    Treats an unbounded loop as a single pass; call ``require_loop_bound``
    first to make the bound a decision.
    """
    if not has_cycle(pipeline):
        return max(1, total_cost(pipeline))
    return max(1, total_cost(pipeline) + loop_cost(pipeline, pipeline.loop_passes or 1))


def loop_cost(pipeline: Pipeline, passes: int) -> int:
    """Extra step executions the graph's loops buy beyond one pass each.

    Every cycle is priced. Nesting multiplies: a cycle inside ``d`` others
    costs ``passes ** (d + 1)``. Over-prices rather than under-prices.
    """
    loops = _loops(pipeline)
    extra = 0
    for header, (cost, _span) in loops.items():
        # Nesting is by header, not span: a loop is inside another only
        # when its header sits within the other's body.
        depth = sum(1 for h, (_, span) in loops.items() if h != header and header in span)
        extra += cost * (passes ** (depth + 1) - 1)
    return extra


def _loops(pipeline: Pipeline) -> dict[str, tuple[int, frozenset[str]]]:
    """One entry per loop header: its costliest pass, and every step on it.

    Back edges are grouped by target, which is the loop header.
    """
    loops: dict[str, tuple[int, frozenset[str]]] = {}
    for edge in back_edges(pipeline):
        cost, span = _cycle_span(pipeline, edge)
        header = edge.describe_target
        known = loops.get(header)
        if known is None:
            loops[header] = (cost, span)
        else:
            loops[header] = (max(known[0], cost), known[1] | span)
    return loops


def longest_cycle_length(pipeline: Pipeline) -> int:
    """Step executions on the costliest cycle, or 0 when the graph is acyclic."""
    return max((_cycle_span(pipeline, e)[0] for e in back_edges(pipeline)), default=0)


def _cycle_span(pipeline: Pipeline, back_edge: Edge) -> tuple[int, frozenset[str]]:
    """The costliest simple cycle closed by ``back_edge``: its cost and its steps."""
    if isinstance(back_edge.target, _End):
        return step_cost(back_edge.source), frozenset({back_edge.source.node_id})
    start, goal = back_edge.target, back_edge.source
    if start is goal:
        return step_cost(start), frozenset({start.node_id})

    edges = pipeline.edges
    best = 0
    span: frozenset[str] = frozenset({start.node_id, goal.node_id})
    visits = 0
    stack: list[tuple[RouteEnd, int, frozenset[str]]] = [
        (start, step_cost(start), frozenset({start.node_id}))
    ]
    while stack:
        current, cost, seen = stack.pop()
        visits += 1
        if visits > PATH_BUDGET:
            return total_cost(pipeline), frozenset(n.node_id for n in pipeline.nodes)
        if current is goal:
            if cost > best:
                best, span = cost, seen
            continue
        for nxt in _route_successors(pipeline, current, edges):
            if nxt.node_id in seen:
                continue
            stack.append((nxt, cost + step_cost(nxt), seen | {nxt.node_id}))
    return (best or step_cost(goal)), span
