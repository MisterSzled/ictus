"""Rules that are true because of how Conductor runs, not how graphs are shaped.

Each is a claim about Conductor's runtime — its template dialect, its
strict-undefined rendering, its output wrapper — and none duplicates
``conductor validate``.

A registry, and deliberately one file: every cut you could draw through it
needs a third module holding ``OUTPUT_REF`` and ``GROUP_REF``, which are shared
across all of them. **The predicates are defined in the order
``conductor_problems`` calls them**, in three bands, so the file reads the way
the dispatcher runs — and each small helper sits under its one caller rather
than in a pile at the end.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ictus.graph.node import AgentNode, ComputeNode, GateNode, Node, TerminateNode
from ictus.graph.ref import Origin
from ictus.graph.traversal import has_cycle, may_be_unresolved
from ictus.interfaces.conductor.emit.templates import output_path
from ictus.interfaces.conductor.emit.workflow import DEFAULT_PROVIDER
from ictus.lint.rules import describe

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.ref import Ref

REMEMBERING_PROVIDERS = frozenset({"claude-agent-sdk"})

__all__ = ["conductor_problems"]

# Conductor's validator checks the agent segment of a reference and stops, so a
# typo in the field name survives it. ``\.output`` must not swallow a parallel
# group's ``\.outputs``.
OUTPUT_REF = re.compile(
    r"\b([a-z_][a-z0-9_]*)\.output(?!s)(?:\.([a-z_][a-z0-9_.]*))?", re.IGNORECASE
)

# A group's results are addressed through the group: ``group.outputs.member.field``.
GROUP_REF = re.compile(
    r"\b([a-z_][a-z0-9_]*)\.(?:outputs|errors)(?:\.([a-z_][a-z0-9_]*))?", re.IGNORECASE
)
INPUT_REF = re.compile(r"\bworkflow\.input\.([a-z_][a-z0-9_]*)", re.IGNORECASE)

# Conductor wraps a step's result under ``.output.``; a field with one of these
# names collides with the wrapper and reads back empty.
RESERVED_OUTPUT_NAMES = frozenset({"outputs", "errors", "output"})


def conductor_problems(pipeline: Pipeline) -> list[str]:
    """Every Conductor-specific violation in one pipeline (not its children)."""
    where = pipeline.pipeline_id
    by_id = {n.node_id: n for n in pipeline.nodes}
    declared_inputs = {p.name for p in pipeline.workflow_inputs}
    problems: list[str] = []

    problems.extend(_instruction_problems(pipeline, where))
    problems.extend(_context_trim_problems(pipeline, where))
    for node in pipeline.nodes:
        problems.extend(_deferred_reference_problems(pipeline, node, where))
        problems.extend(_tool_allowlist_problems(node, where))
        problems.extend(_ignored_field_problems(pipeline, node, where))
        problems.extend(_relative_path_problems(node, where))
        problems.extend(_session_problems(pipeline, node, where))
        problems.extend(_retyped_value_problems(node, where))
        problems.extend(_env_reference_problems(node, where))
        problems.extend(_template_problems(node, by_id, declared_inputs, where))
        problems.extend(_group_reference_problems(pipeline, node, where))
        problems.extend(
            f"{where}: {describe(node)} declares output {port.name!r}, which collides "
            "with Conductor's output wrapper and reads back empty"
            for port in node.outputs
            if port.name in RESERVED_OUTPUT_NAMES
        )

    if pipeline.context_mode == "explicit":
        for node in pipeline.nodes:
            problems.extend(_undeclared_reference_problems(pipeline, node, where))

    for host_id, child in pipeline.children.items():
        problems.extend(
            f"{where}: stage {host_id!r} can exit through {node.node_id!r}, a failed terminal. "
            "A child engine converts that into SubworkflowTerminatedError before the parent's "
            "routes are evaluated (engine/workflow.py, `_run_child_engine`), so it "
            "kills the caller instead of "
            "routing. End with a success terminal carrying the outcome as a value."
            for node in child.nodes
            if isinstance(node, TerminateNode) and node.status == "failed"
        )

    problems.extend(
        f"{where}: exposed output {name!r} collides with Conductor's output wrapper and will "
        "read back empty; rename it"
        for name in pipeline.exposed_outputs
        if name in RESERVED_OUTPUT_NAMES
    )
    return problems


#: Fields Conductor's schema accepts on any agent that only some providers act
#: on, mapped to the providers that read them. Nothing upstream checks these.
#:
#: Entry criterion: the failure is silent. A field ``conductor validate``
#: refuses belongs in :data:`VALIDATED_UPSTREAM` instead. The lint reads the
#: node with ``getattr``, so an entry for a field ictus does not expose is inert.
HONOURED_BY: dict[str, frozenset[str]] = {
    # claude-agent-sdk's own matches are all `is_retryable=False`.
    "retry": frozenset({"claude", "openai", "hermes", "aca", "copilot"}),
    # The schema says Copilot-only and is stale; aca forwards it (providers/aca.py,
    # in `_build_request`).
    "context_tier": frozenset({"copilot", "aca"}),
    # CAPABILITIES.working_dir.
    "working_dir": frozenset({"claude-agent-sdk", "claude", "openai", "copilot"}),
    # CAPABILITIES.skills.
    "skills": frozenset({"claude-agent-sdk", "claude", "openai", "hermes", "copilot"}),
    # CAPABILITIES.plugins. A plugin ships skills, subagents and MCP servers
    # together, and only these two host all three.
    "plugins": frozenset({"claude-agent-sdk", "copilot"}),
    # No capability flag. Read only via `AgentDef.effective_output_schema()`,
    # whose sole caller is providers/copilot.py.
    "output_mode": frozenset({"copilot"}),
}

#: Fields only some providers act on that ``conductor validate`` already
#: refuses, naming the provider and the levels it would take. Recorded here and
#: deliberately not linted.
VALIDATED_UPSTREAM: frozenset[str] = frozenset(
    {
        # capabilities.reasoning_effort, a tuple of levels: claude-agent-sdk
        # None, openai low/medium/high, hermes adds xhigh, the rest all five.
        "reasoning",
        # capabilities-gated to aca.
        "sandbox",
    }
)


#: Fields every provider honours. Recorded so they are not re-derived.
HONOURED_EVERYWHERE: frozenset[str] = frozenset(
    {
        # engine/workflow.py, `_execute_with_agent_timeout` — `asyncio.wait_for`
        # around the whole call.
        "timeout_seconds",
        # engine/validator.py — a second model call the engine makes itself.
        "validator",
        # CAPABILITIES.max_session_seconds, true on all six.
        "max_session_seconds",
    }
)


#: Strategies that make room by deleting a step's output rather than shortening
#: it (`engine/context.py`: both `del self.agent_outputs[agent_name]`).
_DELETING_STRATEGIES = frozenset({"drop_oldest", "summarize"})

# Conductor's loader expands `${VAR}` in every string it reads
# (config/loader.py resolve_env_vars).
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


# --- the pipeline's own settings -------------------------------------------------


def _instruction_problems(pipeline: Pipeline, where: str) -> list[str]:
    """The same, for workspace instructions — which every step's prompt carries."""
    return [
        f"{where}: workspace instructions contain ${{{match.group(1)}}}. Conductor expands "
        "it at load and prepends the result to every prompt, so a set variable's value goes "
        "to the provider with every step."
        for text in pipeline.instructions
        for match in _ENV_REF.finditer(text)
    ]


def _context_trim_problems(pipeline: Pipeline, where: str) -> list[str]:
    """A context ceiling that makes room by deleting what a loop reads.

    A deleted output is indistinguishable from one that has not run, so the
    loop keeps rendering nothing. ``truncate`` shortens in place instead.
    """
    cap = pipeline.context_max_tokens
    if cap is None:
        if pipeline.context_trim is not None:
            return [
                f"{where}: context_trim is set but context_max_tokens is not, so nothing "
                "ever trims and the strategy is never reached. Set a ceiling, or drop the "
                "strategy."
            ]
        return []
    if pipeline.context_trim is None:
        return [
            f"{where}: context_max_tokens is {cap} with no context_trim. The engine does "
            "not leave that unset — it uses drop_oldest, which deletes whole step outputs "
            "oldest first. Name the strategy you want rather than inheriting the most "
            "destructive one by omission."
        ]
    if pipeline.context_trim.value in _DELETING_STRATEGIES and has_cycle(pipeline):
        return [
            f"{where}: context_max_tokens is set with trim_strategy "
            f"{pipeline.context_trim.value!r} on a graph that loops. That strategy makes "
            "room by deleting whole step outputs, and a loop reads the previous pass "
            "through exactly those — once one is deleted the reference renders empty and "
            "is indistinguishable from a first pass, so the loop keeps running and stops "
            "deliberating. Use TrimStrategy.TRUNCATE, which shortens fields in place and "
            "leaves every reference resolvable."
        ]
    return []


# --- what the provider actually reads, field by field ----------------------------


def _deferred_reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A reference to a node that may not have run yet must be guarded.

    The ``?`` suffix makes the dependency optional, not the template variable,
    and Conductor renders with strict undefined.
    """
    problems: list[str] = []
    for dep in pipeline.deps_into(node):
        deferred = dep.connection.target.optional or may_be_unresolved(pipeline, dep.source, node)
        if not deferred:
            continue
        source_id = dep.source.node_id
        for template in node.template_strings():
            if f"{source_id}." not in template or f"{source_id} is defined" in template:
                continue
            problems.append(
                f"{where}: {describe(node)} references {source_id!r} in a template, but "
                f"{source_id!r} may not have run yet (the dependency is optional). Guard it "
                f"with `{{% if {source_id} is defined %}}` or the first pass fails with "
                f"\"'{source_id}' is undefined\"."
            )
            break
    return problems


def _tool_allowlist_problems(node: Node, where: str) -> list[str]:
    """Naming individual tools raises ``ProviderError`` mid-run. Unset and empty both work."""
    tools = getattr(node, "tools", None)
    if not tools:
        return []
    return [
        f"{where}: agent {node.node_id!r} names the tools {sorted(tools)}, which conductor "
        "cannot translate — its `tools:` are workflow tool names, not the CLI's, and the "
        "provider raises rather than grant the wrong ones. Use tools=() for none, or leave "
        "it unset, which grants nothing unless the pipeline asks for native_tools."
    ]


# Conductor's loader expands `${VAR}` in every string it reads
# (config/loader.py resolve_env_vars).


def _ignored_field_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Refuse a field the chosen provider will read straight past."""
    provider = pipeline.provider or DEFAULT_PROVIDER
    problems: list[str] = []
    for field, honoured in HONOURED_BY.items():
        if getattr(node, field, None) is None or provider in honoured:
            continue
        problems.append(
            f"{where}: agent {node.node_id!r} sets {field}, which provider {provider!r} "
            f"ignores — Conductor accepts the field on any agent and only "
            f"{sorted(honoured)} act on it, so the workflow would load, validate and run "
            f"with the setting doing nothing. Drop it, or choose a provider that reads it."
        )
    return problems


# Conductor's rule for when a `skills:`/`plugins:` entry is a path rather than a
# registered name (skills/registry.py). Purely syntactic.


def _relative_path_problems(node: Node, where: str) -> list[str]:
    """Refuse a relative path on an agent, which resolves somewhere useless.

    ``working_dir``, ``skills`` and ``plugins`` resolve a relative entry against
    the workflow file's directory (engine/workflow.py,
    ``_resolve_agent_working_dir``) — the pipeline's
    ``build/``, which ``ictus emit`` rewrites and prunes.

    ``AgentNode`` only: a script's ``working_dir`` goes straight to the
    subprocess (executor/script.py, ``cwd=``), where relative is correct.
    """
    if not isinstance(node, AgentNode):
        return []
    problems: list[str] = []
    raw = getattr(node, "working_dir", None)
    if (
        isinstance(raw, str)
        and not _is_deferred(raw)
        and not raw.startswith("~")
        and not PurePosixPath(raw).is_absolute()
    ):
        problems.append(
            f"{where}: agent {node.node_id!r} sets working_dir to the relative path "
            f"{raw!r}, which the engine resolves against the emitted workflow's own "
            f"directory — the pipeline's build/, which ictus rewrites and prunes. "
            f"Use an absolute path, a ~/ one, or a template resolved at run time."
        )
    for field in ("skills", "plugins"):
        for entry in getattr(node, field, None) or ():
            if not isinstance(entry, str) or _is_deferred(entry) or not _is_path_entry(entry):
                continue
            if entry.startswith("~") or PurePosixPath(entry).is_absolute():
                continue
            problems.append(
                f"{where}: agent {node.node_id!r} names {field} entry {entry!r}, a relative "
                f"path the engine resolves against the emitted workflow's directory — the "
                f"pipeline's build/, which ictus rewrites and prunes. Use an absolute path "
                f"or a ~/ one, or name a registered entry instead of a path."
            )
    return problems


#: Strategies that make room by deleting a step's output rather than shortening
#: it (`engine/context.py`: both `del self.agent_outputs[agent_name]`).


def _is_path_entry(entry: str) -> bool:
    return entry.startswith(("~", ".")) or "/" in entry or "\\" in entry


# Rendered at run time, so ictus cannot know what it resolves to.


def _is_deferred(value: str) -> bool:
    return "{{" in value or "${" in value


def _session_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A step asks to remember, on a provider that cannot."""
    key = getattr(node, "session_key", None)
    if key is None:
        return []
    provider = pipeline.provider or DEFAULT_PROVIDER
    if provider in REMEMBERING_PROVIDERS:
        return []
    return [
        f"{where}: agent {node.node_id!r} asks to resume session {key!r}, which provider "
        f"{provider!r} cannot do — it would start cold every time and Conductor refuses "
        f"the workflow. Use one of {sorted(REMEMBERING_PROVIDERS)}, or turn remembering off."
    ]


def _retyped_value_problems(node: Node, where: str) -> list[str]:
    """A ``set`` step whose value Conductor's YAML loader will hand back retyped.

    Conductor runs a bare ``value:`` through a YAML load, so ``"no"`` becomes
    ``False`` and ``"3"`` an integer — a silent type change at the point a route
    condition is about to test it. ``output_type`` is what pins it, and the node
    declares ``string`` without it, so the graph and the run disagree and nothing
    says so.

    Only literals: a value carrying ``{{`` is rendered at run time and what it
    becomes is not knowable here.
    """
    if not isinstance(node, ComputeNode) or node.value is None or node.value_type is not None:
        return []
    if "{{" in node.value:
        return []
    loaded = _as_yaml(node.value)
    if isinstance(loaded, str):
        return []
    return [
        f"{where}: step {node.node_id!r} sets {node.value!r} with no output_type, and "
        f"Conductor loads that as {type(loaded).__name__} {loaded!r} — the node declares "
        "string, so a route testing it is comparing two different types and is never true. "
        "Pass output_type=PortType.<the type you mean>, or quote it into something YAML "
        "reads as text."
    ]


def _as_yaml(text: str) -> object:
    """What Conductor's loader makes of this scalar. Text, if it will not parse.

    ``typ="safe", pure=True`` because that is the loader ``executor/set_step.py``
    builds, and the answer depends on it: that is YAML 1.2, where ``no``, ``yes``,
    ``on`` and ``off`` stay strings. Only ``true``/``false``, integers, floats and
    ``null`` retype. Guessing a YAML 1.1 loader here would report four spellings
    that are in fact safe.
    """
    try:
        return YAML(typ="safe", pure=True).load(text)
    except YAMLError:
        return text


def _env_reference_problems(node: Node, where: str) -> list[str]:
    """A `${VAR}` in text a model reads is either a crash or a leak.

    Expansion happens at load: unset refuses the workflow, set puts the value
    in the prompt.
    """
    problems: list[str] = []
    for text in node.template_strings():
        for match in _ENV_REF.finditer(text):
            problems.append(
                f"{where}: agent {node.node_id!r} has ${{{match.group(1)}}} in text a model "
                "reads. Conductor expands it at load: unset it refuses the workflow, and set "
                "it puts the value in the prompt. Escape it, or say the variable's name "
                "without the ${...} syntax."
            )
    return problems


# --- references and templates, which resolve or silently do not ------------------


def _template_problems(
    node: Node, by_id: dict[str, Node], declared_inputs: set[str], where: str
) -> list[str]:
    """Check the field segment of every reference, which Conductor never does."""
    problems: list[str] = []
    for template in (*node.template_strings(), *node.settled_template_strings()):
        for ref_node, ref_field in OUTPUT_REF.findall(template):
            target = by_id.get(ref_node)
            if target is None:
                problems.append(f"{where}: {describe(node)} references unknown node {ref_node!r}")
                continue
            if not ref_field:
                continue
            head = ref_field.split(".")[0]
            if isinstance(target, GateNode) and head == "additional_input":
                continue
            if head not in {p.name for p in target.outputs}:
                known = ", ".join(p.name for p in target.outputs) or "(none declared)"
                problems.append(
                    f"{where}: {describe(node)} references {ref_node}.output.{head}, "
                    f"which {ref_node!r} does not declare; declared outputs: {known}"
                )
        problems.extend(
            f"{where}: {describe(node)} references workflow input {name!r}, which is not "
            f"declared; declared inputs: {', '.join(sorted(declared_inputs)) or '(none declared)'}"
            for name in INPUT_REF.findall(template)
            if name not in declared_inputs
        )
    return problems


def _group_reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Check references addressed through a parallel group."""
    groups = {g.group_id: g for g in pipeline.groups}
    problems: list[str] = []
    for template in node.template_strings():
        for group_name, member in GROUP_REF.findall(template):
            group = groups.get(group_name)
            if group is None:
                known = ", ".join(sorted(groups)) or "(none)"
                problems.append(
                    f"{where}: {describe(node)} reads {group_name}.outputs, but "
                    f"{group_name!r} is not a parallel group; groups here: {known}"
                )
                continue
            if member and member not in {m.node_id for m in group.members}:
                known = ", ".join(sorted(m.node_id for m in group.members))
                problems.append(
                    f"{where}: {describe(node)} reads {group_name}.outputs.{member}, "
                    f"but {member!r} is not in that group; members: {known}"
                )
    return problems


def _undeclared_reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Under ``context.mode: explicit`` a node sees only what its ``input:`` names.

    Anything else is an undefined variable at render time.
    """
    referenced: set[str] = set()
    for ref in node.prompt_refs():
        if not ref.from_input:
            referenced.add(ref.source_id)
    for edge in pipeline.outgoing(node):
        for ref in edge.condition_refs():
            if not ref.from_input:
                referenced.add(ref.source_id)
    # Explicit-rendered slots only; a terminal's payload sees the whole run.
    for template in node.template_strings():
        referenced.update(name for name, _ in OUTPUT_REF.findall(template))
        referenced.update(name for name, _ in GROUP_REF.findall(template))

    declared: set[str] = {node.node_id}
    for dep in pipeline.deps_into(node):
        declared.add(dep.source.node_id)
        if isinstance(dep.source, Node):
            group = pipeline.group_of(dep.source)
            if group is not None:
                # A member is addressed through its group, so either name resolves.
                declared.add(group.group_id)
    known = (
        {n.node_id for n in pipeline.nodes}
        | {g.group_id for g in pipeline.groups}
        | {m.group_id for m in pipeline.maps}
    )

    problems = [
        f"{where}: {describe(node)} references {name!r} but does not declare it as an "
        f"input. Under context.mode 'explicit' it will not be in scope, and the template "
        f"fails at run time with \"'{name}' is undefined\". Wire it with feed() or connect()."
        for name in sorted(referenced - declared)
        if name in known
    ]
    return (
        problems
        + _undeclared_member_field_problems(pipeline, node, where)
        + _undeclared_port_problems(pipeline, node, where)
    )


def _undeclared_port_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A step is in scope port by port, not whole.

    Each ``input:`` entry carries one output, so reading a second port of the
    same source fails at render time even though the source is declared.
    """
    carried: dict[str, set[str]] = {}
    for dep in pipeline.deps_into(node):
        carried.setdefault(dep.source.node_id, set()).add(dep.connection.source.name)
    if not carried:
        return []
    # Prompts only. A route condition renders against the whole context
    # (`engine/router.py`), so it may read a port this node was not wired for.
    referenced: list[Ref] = list(node.prompt_refs())
    groups = {g.group_id for g in pipeline.groups} | {m.group_id for m in pipeline.maps}
    return [
        f"{where}: {describe(node)} reads {ref.source_id}.{ref.port}, but is wired to "
        f"{ref.source_id!r} for {_listed(carried[ref.source_id])} only. Under "
        f"context.mode 'explicit' one input entry carries one port, so this renders "
        f"against a value without it and fails at run time. Add a feed() for it."
        for ref in referenced
        # Its own ports are always in scope, and a group is projected whole.
        if ref.source_id in carried
        and ref.source_id != node.node_id
        and ref.source_id not in groups
        and not ref.from_input
        and ref.port not in carried[ref.source_id]
    ]


def _listed(ports: set[str]) -> str:
    return ", ".join(sorted(repr(name) for name in ports))


def _undeclared_member_field_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A parallel group's fields are projected one at a time, not as a whole object.

    ``_add_parallel_group_input`` (engine/context.py) copies exactly the
    field each ``input:`` entry names.
    """
    wanted: dict[str, Ref] = {}
    for ref in node.prompt_refs():
        _collect_member_path(pipeline, ref, wanted)
    for edge in pipeline.outgoing(node):
        for ref in edge.condition_refs():
            _collect_member_path(pipeline, ref, wanted)

    declared = {
        output_path(pipeline, dep.source, dep.connection.source.name)
        for dep in pipeline.deps_into(node)
    }
    return [
        f"{where}: {describe(node)} reads {path!r}, but only the exact fields it "
        f"declares are put in scope — a sibling field of the same group member is not. "
        f"Wire {ref.source_id}.{ref.port} into {node.node_id!r} with feed()."
        for path, ref in sorted(wanted.items())
        if path not in declared
    ]


def _collect_member_path(pipeline: Pipeline, ref: Ref, into: dict[str, Ref]) -> None:
    """Record ``ref`` if it reads a field of a parallel group member."""
    if ref.origin is not Origin.NODE:
        return
    source = next((n for n in pipeline.nodes if n.node_id == ref.source_id), None)
    if source is None or pipeline.group_of(source) is None:
        return
    into[output_path(pipeline, source, ref.port)] = ref
