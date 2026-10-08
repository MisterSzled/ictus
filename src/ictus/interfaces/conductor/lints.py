"""Rules that are true because of how Conductor runs, not how graphs are shaped.

Each was checked against the installed validator and confirmed to pass it, so
none of them duplicates ``conductor validate``. They live here rather than in
``ictus.lint`` because every one of them is a claim about Conductor's runtime:
its template dialect, its strict-undefined rendering, its output wrapper.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ictus.graph.node import AgentNode, GateNode, Node, TerminateNode
from ictus.graph.ref import Origin
from ictus.interfaces.conductor.templates import output_path
from ictus.interfaces.conductor.workflow import DEFAULT_PROVIDER
from ictus.lint.rules import describe

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.ref import Ref

REMEMBERING_PROVIDERS = frozenset({"claude-agent-sdk"})

__all__ = ["conductor_problems"]

# Conductor's template namespace. Its own validator checks the agent segment of
# a reference and stops there, so a typo in the field name survives validation.
# ``\.output`` must not swallow the ``\.outputs`` of a parallel group — doing so
# reports the group as an unknown agent and hides whatever the reference meant.
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
            "routes are evaluated (engine/workflow.py:2131), so it kills the caller instead of "
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


#: Fields Conductor's schema accepts on *any* agent that only some providers act
#: on, mapped to the providers that actually read them.
#:
#: Every entry was read out of Conductor rather than out of its schema, because
#: the schema is where these look universal. Two sources, both authoritative:
#: each provider's ``CAPABILITIES`` declaration (``providers/capabilities.py``
#: names the flags), and, for the fields with no flag, the code that consumes
#: the value.
#:
#: The failure this exists to stop is quiet, and *quiet* is the entry criterion:
#: nothing upstream checks these, so a workflow declaring one on a provider that
#: ignores it loads clean, validates clean, runs, and does nothing. A retry
#: policy that never retries is worse than none, because it was written by
#: someone who then stopped worrying about the failure it does not handle.
#:
#: A field Conductor checks for itself does **not** belong here — see
#: :data:`VALIDATED_UPSTREAM`. Adding one would duplicate ``conductor validate``
#: with a worse message, which is the thing this module promises not to do.
#:
#: A field ictus does not expose yet still belongs here: the lint reads the node
#: with ``getattr``, so an entry is inert until the field is wired and correct
#: from the moment it is. Wired today: ``working_dir``, ``skills``, ``plugins``,
#: ``retry``, ``context_tier`` — the first three because ``claude-agent-sdk``
#: honours them, the last two so that writing one against that provider is
#: refused rather than quietly ignored.
HONOURED_BY: dict[str, frozenset[str]] = {
    # Every provider but this one reads `agent.retry`; claude-agent-sdk's own
    # matches are all `is_retryable=False` on errors it raises.
    "retry": frozenset({"claude", "openai", "hermes", "aca", "copilot"}),
    # The schema documents this as Copilot-only and is stale — aca forwards it
    # too (providers/aca.py:769).
    "context_tier": frozenset({"copilot", "aca"}),
    # CAPABILITIES.working_dir.
    "working_dir": frozenset({"claude-agent-sdk", "claude", "openai", "copilot"}),
    # CAPABILITIES.skills.
    "skills": frozenset({"claude-agent-sdk", "claude", "openai", "hermes", "copilot"}),
    # CAPABILITIES.plugins. Narrow by design: a plugin ships skills, subagents
    # and MCP servers together, and only these two can host all three.
    "plugins": frozenset({"claude-agent-sdk", "copilot"}),
    # No capability flag. Consumed only through `AgentDef.effective_output_schema()`,
    # whose sole caller is providers/copilot.py:1194 — every other provider reads
    # `agent.output` directly and never sees the mode.
    "output_mode": frozenset({"copilot"}),
}

#: Fields only some providers act on that Conductor refuses for itself.
#:
#: The distinction from :data:`HONOURED_BY` is not which providers honour them —
#: it is whether anything upstream notices. ``conductor validate`` rejects both
#: of these with a message naming the provider and, for ``reasoning``, the exact
#: levels it would take:
#:
#:     Agent 'work' resolves to reasoning.effort='max' but provider 'openai'
#:     supports only ['low', 'medium', 'high'].
#:
#: ictus cannot better that, and repeating it would mean maintaining a table of
#: per-provider *levels* that drifts against ``CAPABILITIES.reasoning_effort``.
#: So these are recorded and deliberately not linted. Checked by running
#: ``conductor validate`` over an emitted workflow for each, not inferred.
VALIDATED_UPSTREAM: frozenset[str] = frozenset(
    {
        # capabilities.reasoning_effort, a tuple of levels rather than a bool:
        # claude-agent-sdk None, openai low/medium/high, hermes adds xhigh,
        # claude/aca/copilot all five.
        "reasoning",
        # capabilities-gated to aca. The two other providers mentioning
        # "sandbox" do so only in comments.
        "sandbox",
    }
)


#: The same question asked of the rest, and answered "everywhere".
#:
#: Recorded rather than omitted so the next person does not re-derive it. Two of
#: these are engine-level, which is why they hold across providers that share
#: nothing else; the third is declared by every provider individually. All three
#: are wired on ``AgentNode`` — having no provider to refuse them on is what made
#: them the cheap ones to expose.
HONOURED_EVERYWHERE: frozenset[str] = frozenset(
    {
        # engine/workflow.py:1516 — `asyncio.wait_for` around the whole call.
        "timeout_seconds",
        # engine/validator.py — a second model call the engine makes itself.
        "validator",
        # CAPABILITIES.max_session_seconds is true on all six.
        "max_session_seconds",
    }
)


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


# Conductor's own rule for when a `skills:`/`plugins:` entry is a path rather
# than a registered name (skills/registry.py:226). Purely syntactic, so a bare
# name can never be shadowed by a same-named directory.
def _is_path_entry(entry: str) -> bool:
    return entry.startswith(("~", ".")) or "/" in entry or "\\" in entry


# Rendered at run time, so ictus cannot know what they resolve to and does not
# get to refuse them.
def _is_deferred(value: str) -> bool:
    return "{{" in value or "${" in value


def _relative_path_problems(node: Node, where: str) -> list[str]:
    """Refuse a relative path on an agent, which resolves somewhere useless.

    ``working_dir``, ``skills`` and ``plugins`` all resolve a relative entry
    against the *workflow file's* directory (engine/workflow.py:620-622 for the
    first, and the schema says the other two follow it). For ictus that
    directory is the pipeline's ``build/`` — compiled output that ``ictus emit``
    rewrites and prunes. Nobody means that, and the failure is quiet in the two
    ways that matter: a missing ``working_dir`` raises mid-run, and a skill path
    that does not resolve is a step that runs without the instructions it was
    supposed to have.

    ``AgentNode`` only. ``working_dir`` is one Conductor field serving two step
    kinds and they do not resolve it the same way: a script's goes straight to
    the subprocess (executor/script.py, ``cwd=``), so it resolves against the
    directory the run was launched from — the project — where a relative path is
    both correct and the obvious thing to write. Refusing it there broke every
    demo that runs a script, which is how this distinction earned its own rule.
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


#: The strategies that make room by deleting a step's output rather than
#: shortening it. Read from `engine/context.py`: `_trim_drop_oldest` and
#: `_trim_summarize` both `del self.agent_outputs[agent_name]`; `_trim_truncate`
#: shortens fields in place and deletes nothing.
_DELETING_STRATEGIES = frozenset({"drop_oldest", "summarize"})


def _context_trim_problems(pipeline: Pipeline, where: str) -> list[str]:
    """A context ceiling that makes room by deleting what a loop reads.

    Trimming is the only thing in the engine that removes a step's output from
    the run (`engine/context.py`, two `del agent_outputs[...]` sites). A loop
    reads the pass before through exactly those entries, and the guard that lets
    a first pass render nothing cannot tell "not run yet" from "deleted a moment
    ago" — both are an absent key. So a deliberation whose outputs get trimmed
    does not fail: it goes quiet and reads like a first round, every round,
    for the rest of the run.

    ``truncate`` is the one strategy that does not do this. It shortens fields
    in place, so every reference still resolves — to less text, which is what a
    ceiling is supposed to cost.
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
    if pipeline.context_trim.value in _DELETING_STRATEGIES and pipeline.has_cycle():
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


def _tool_allowlist_problems(node: Node, where: str) -> list[str]:
    """Naming individual tools is a run-time failure this backend cannot avoid.

    Omitting the list and emptying it both work — "the engine's default" and
    "none at all". Naming them raises ``ProviderError`` partway through the run,
    on a list that emitted and validated cleanly.
    """
    tools = getattr(node, "tools", None)
    if not tools:
        return []
    return [
        f"{where}: agent {node.node_id!r} names the tools {sorted(tools)}, which conductor "
        "cannot translate — its `tools:` are workflow tool names, not the CLI's, and the "
        "provider raises rather than grant the wrong ones. Use tools=() for none, or leave "
        "it unset for the default set (filesystem, bash, web)."
    ]


# Conductor's loader expands `${VAR}` in every string it reads, before anything
# else happens (config/loader.py resolve_env_vars).
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


def _env_reference_problems(node: Node, where: str) -> list[str]:
    """A `${VAR}` in text a model reads is either a crash or a leak.

    Expansion happens at load, so with the variable unset the workflow refuses
    to load at all, and with it set the *value* is substituted into the prompt
    and sent to the provider. A token belongs in an MCP header, which is
    expanded for exactly that reason; it does not belong in something a model
    is asked to read.
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


def _instruction_problems(pipeline: Pipeline, where: str) -> list[str]:
    """The same, for workspace instructions — which every step's prompt carries."""
    return [
        f"{where}: workspace instructions contain ${{{match.group(1)}}}. Conductor expands "
        "it at load and prepends the result to every prompt, so a set variable's value goes "
        "to the provider with every step."
        for text in pipeline.instructions
        for match in _ENV_REF.finditer(text)
    ]


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


def _undeclared_reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """Under ``context.mode: explicit`` a node sees only what its ``input:`` names.

    Referencing anything else is an undefined variable at render time. That is
    late: a gate's terminal step failed this way *after* the human had answered,
    losing the run. Conductor cannot catch it — the reference is well-formed and
    the agent exists — so it has to be caught here.
    """
    referenced: set[str] = set()
    for ref in node.prompt_refs():
        if not ref.from_input:
            referenced.add(ref.source_id)
    for edge in pipeline.outgoing(node):
        for ref in edge.condition_refs():
            if not ref.from_input:
                referenced.add(ref.source_id)
    # Only the explicit-rendered slots. A terminal's payload is rendered with the
    # whole run in scope, so requiring it to be declared would be wrong.
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

    Each ``input:`` entry names one output. A node wired for one of another
    node's ports has *that port* in scope and nothing else, so reading a second
    one renders against a dict holding only the first and fails with "'dict
    object' has no attribute 'basis'" — well after the step that produced it
    succeeded, and on a graph the node-level check calls wired.

    The node-level check above cannot see this: the source is declared, so it is
    satisfied. Both have been wrong in the same session, which is what this is
    for.
    """
    carried: dict[str, set[str]] = {}
    for dep in pipeline.deps_into(node):
        carried.setdefault(dep.source.node_id, set()).add(dep.connection.source.name)
    if not carried:
        return []
    # Prompts only. A route condition is rendered against the whole context
    # (`engine/router.py`: `eval_context = {**context, "output": ...}`), so a
    # condition may read a port this node was never wired for and is right to.
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
    """Port names as prose, for a message somebody has to act on."""
    return ", ".join(sorted(repr(name) for name in ports))


def _undeclared_member_field_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A parallel group's fields are projected one at a time, not as a whole object.

    ``_add_parallel_group_input`` (engine/context.py:99-101) copies exactly the
    field each ``input:`` entry names, so declaring ``g.outputs.a.position``
    puts *only* that key under ``a``. Reading ``g.outputs.a.satisfied`` next to
    it then fails with "'dict object' has no attribute 'satisfied'" — and the
    group name being declared is what makes that look wired.
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


def _deferred_reference_problems(pipeline: Pipeline, node: Node, where: str) -> list[str]:
    """A reference to a node that may not have run yet must be guarded.

    The ``?`` suffix makes the *dependency* optional. It does not make the
    template variable defined, and Conductor renders with strict undefined, so
    the first pass through a loop dies on ``'<node>' is undefined``.
    """
    problems: list[str] = []
    for dep in pipeline.deps_into(node):
        deferred = dep.connection.target.optional or pipeline.may_be_unresolved(dep.source, node)
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
