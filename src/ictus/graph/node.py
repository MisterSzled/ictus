"""Node kinds — one frozen class per Conductor agent type.

Conductor's ``AgentDef`` is a single wide model whose ``validate_agent_type``
validator enforces a different required/forbidden field set for each of its
eight ``type`` values, twenty-plus fields deep. Mirroring that as one class with
a free-form config dict reproduces the trap: every illegal combination stays
representable and is only caught downstream, if at all.

Instead each kind is its own class carrying only the fields Conductor permits on
it. ``TerminateNode`` has no ``outputs`` field to set, so the "terminate agents
cannot have 'output'" rule cannot be violated. ``routes`` is not modelled here
at all — routing belongs to the graph, so ``Pipeline`` owns it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from ictus.errors import CompositionError, UnknownPortError
from ictus.graph.ports import InputPort, OutputPort, PortType
from ictus.graph.ref import Ref, Template

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from ictus.graph.values import YamlDict, YamlValue


class NodeKind(StrEnum):
    """What a node does, named for the work rather than for any engine.

    A backend maps these onto whatever it calls them. ``Capabilities`` is
    declared in these terms, so a graph using a kind the target cannot express
    is refused while it is being composed.
    """

    LLM_CALL = "llm_call"
    HUMAN_DECISION = "human_decision"
    SUBPROCESS = "subprocess"
    COMPUTATION = "computation"
    DELAY = "delay"
    EXIT = "exit"
    SUB_GRAPH = "sub_graph"
    ASK = "ask"


NODE_KINDS = frozenset(NodeKind)


class Backoff(StrEnum):
    """How the wait between attempts grows."""

    FIXED = "fixed"
    EXPONENTIAL = "exponential"


class RetryOn(StrEnum):
    """A category of failure worth attempting again.

    Deliberately narrow. A wrong answer is not a transient failure and retrying
    it just buys the same answer twice; these are the two the engine can tell
    apart from outside the model.
    """

    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"


class ReasoningEffort(StrEnum):
    """How much thinking a step is allowed before it answers.

    The levels are a budget, not a dial on quality: on Anthropic each maps to a
    number of thinking tokens the model may spend — roughly 2k, 8k, 16k, 32k and
    60k — which is charged whether or not the step needed them. That is per
    call, so a council of four voices over three rounds at ``MAX`` is a
    different order of spend from the same council at ``LOW``.

    Worth setting per node rather than per workflow for exactly that reason: the
    step that synthesises is usually the one that needs it, and the steps either
    side of it usually do not.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ContextTier(StrEnum):
    """Which context window a step asks for, on models that offer a choice."""

    DEFAULT = "default"
    LONG = "long_context"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """What a step does about a transient failure.

    Transient means the provider fell over or the call timed out — not that the
    answer was wrong. Re-running a step that produced a bad answer produces
    another bad answer at full price, which is what ``converge`` is for.

    ``attempts`` counts the first try, so 1 means no retry at all and is the
    engine's default.
    """

    attempts: int = 3
    backoff: Backoff = Backoff.EXPONENTIAL
    first_delay_seconds: float | None = None
    on: tuple[RetryOn, ...] = ()
    """Which failures to attempt again. Empty leaves the engine's own set."""

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 10:
            raise CompositionError(
                f"a retry policy allows 1 to 10 attempts, got {self.attempts}. One means "
                "no retry; past ten you are waiting on something that is not coming back."
            )
        if self.first_delay_seconds is not None and self.first_delay_seconds <= 0:
            raise CompositionError(
                f"retry first_delay_seconds must be positive, got {self.first_delay_seconds}"
            )
        if len(set(self.on)) != len(self.on):
            raise CompositionError(f"retry policy repeats a failure category: {list(self.on)}")


def _has_text(prompt: str | Template) -> bool:
    """Whether a prompt says anything at all."""
    if isinstance(prompt, Template):
        return bool(prompt.parts)
    return bool(prompt.strip())


_IDENT = "abcdefghijklmnopqrstuvwxyz0123456789_"


def slugify(label: str) -> str:
    """Derive a routing identifier from a human label."""
    out = "".join(c if c in _IDENT else "_" for c in label.strip().lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


@dataclass(frozen=True, kw_only=True, eq=False)
class Node(ABC):
    """A single step in a workflow.

    ``node_id`` is the identity Conductor routes on — it is emitted as
    ``AgentDef.name``; ``description`` is the human-facing string and is emitted
    as ``AgentDef.description``. Collapsing these two into one field is what made
    the previous emitter unroutable: ``entry_point`` was written from the slug
    while agent identity was the display string, so nothing resolved.

    There is deliberately no third "label" field: Conductor has one human-text
    slot, and a field that is stored, type-checked and then discarded is how the
    previous port graph came to mean nothing.
    """

    node_id: str
    description: str = ""
    inputs: tuple[InputPort, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id:
            raise CompositionError("node_id cannot be empty")
        if slugify(self.node_id) != self.node_id:
            raise CompositionError(
                f"node_id {self.node_id!r} is not a routing identifier; "
                f"use {slugify(self.node_id)!r} (lowercase, digits and underscore only)"
            )
        seen: set[str] = set()
        names = [p.name for p in self.inputs] + [p.name for p in self.outputs]
        for name in names:
            if name in seen:
                raise CompositionError(
                    f"node {self.node_id!r} declares port {name!r} more than once"
                )
            seen.add(name)

    @property
    @abstractmethod
    def kind(self) -> NodeKind:
        """What this node does, in engine-neutral terms."""

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """Values this node produces.

        Empty for the kinds on which Conductor forbids ``output:``.
        """
        return ()

    @property
    def routes_via_options(self) -> bool:
        """Whether outgoing edges are emitted as ``options[].route``.

        True only for ``human_gate``, where ``routes:`` is accepted by the
        schema and then ignored by the engine.
        """
        return False

    @property
    def accepts_routes(self) -> bool:
        """Whether this kind may have outgoing edges at all."""
        return True

    @property
    def emits_output_schema(self) -> bool:
        """Whether Conductor permits an ``output:`` block on this kind."""
        return False

    def output_ref(self, port_name: str) -> str:
        """The path under ``<node>.output.`` that reads this port.

        Identity for most kinds. A gate overrides it because Conductor fixes the
        shape of a gate's output rather than taking a declared schema.
        """
        return port_name

    def guard_depth(self, port_name: str) -> int:
        """How many trailing path segments a guard must test individually.

        Zero for almost everything: if the step ran, its declared outputs are
        there, so testing the node's own name is enough. A gate is the
        exception — its free-text field exists only on the branch that asked
        for one, and both ``additional_input`` and the field itself can be
        missing on any other branch. Reading either under strict undefined is a
        hard template error, and it lands a round later than the mistake.
        """
        _ = port_name
        return 0

    def template_strings(self) -> Iterator[str]:
        """Author-written *raw* strings a backend renders.

        Typed references are reported by ``prompt_refs`` instead; anything left
        here is literal text that should contain no template syntax at all.
        """
        return iter(())

    def prompt_refs(self) -> Iterator[Ref]:
        """Typed references this node reads, rendered in the node's own context."""
        return iter(())

    def settled_template_strings(self) -> Iterator[str]:
        """Raw strings rendered with the whole run in scope, not the node's inputs.

        Conductor renders a terminal's output payload against the accumulated
        context rather than the step's declared inputs, so the explicit-mode rule
        that governs a prompt does not govern these. Keeping them apart is what
        stops a correct payload being reported as an undeclared reference.
        """
        return iter(())

    def settled_refs(self) -> Iterator[Ref]:
        """Typed references in the accumulate-rendered slots."""
        return iter(())

    def ref(self, port: str) -> Ref:
        """A typed reference to one of this node's outputs.

        Checked here: an undeclared port raises now, rather than surviving as
        text until a lint parses it back out or a run fails on it.
        """
        declared = self.get_output(port)
        return Ref(
            source_id=self.node_id,
            port=declared.name,
            element=declared.element,
            port_type=declared.port_type,
            source=self,
        )

    def get_input(self, name: str) -> InputPort:
        """Look up a declared input port by name."""
        for port in self.inputs:
            if port.name == name:
                return port
        known = ", ".join(p.name for p in self.inputs) or "(none)"
        raise UnknownPortError(
            f"node {self.node_id!r} has no input port {name!r}; declared inputs: {known}"
        )

    def get_output(self, name: str) -> OutputPort:
        """Look up a declared output port by name."""
        for port in self.outputs:
            if port.name == name:
                return port
        known = ", ".join(p.name for p in self.outputs) or "(none)"
        raise UnknownPortError(
            f"node {self.node_id!r} has no output port {name!r}; declared outputs: {known}"
        )


@dataclass(frozen=True, slots=True)
class Validator:
    """Judge a step's output against a rubric, and revise it once if it fails.

    A second model call, after the step, asking whether the output meets
    ``criteria``. It is about *content*, which is what makes it different from
    the two checks that already exist: ``declared_outputs`` fixes the shape, and
    ``retry`` covers a call that fell over. This one catches an answer that is
    well-formed, delivered successfully, and wrong.

    It is not free. Budget two model calls per step where you set it, and three
    where the revision fires.
    """

    criteria: str
    model: str | None = None
    """A cheaper model for the grading pass. Unset reuses the step's own."""

    revise: bool = True
    """Whether a failed check re-runs the step once with the feedback attached.

    A bool rather than a count because the engine hard-caps it at one: past a
    single feedback-driven attempt you are fighting the prompt, not noise. Off,
    the check still runs and still reports — it just does not act.
    """

    def __post_init__(self) -> None:
        if not self.criteria.strip():
            raise CompositionError(
                "a validator needs criteria; an empty rubric is a second billable call "
                "that asks the model whether it is happy with itself"
            )


@dataclass(frozen=True, kw_only=True, eq=False)
class AgentNode(Node):
    """An LLM step. Conductor ``type: agent`` (the default)."""

    prompt: str | Template
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    tools: tuple[str, ...] | None = None
    """Which tools this step may call.

    ``None`` leaves it to the workflow; an empty tuple denies tools outright.
    Two different things, and collapsing them is why a step could not be built
    that is *denied* tools — Conductor reads ``None`` as all and ``[]`` as none,
    and an omitted key is the former.
    """

    declared_outputs: tuple[OutputPort, ...] = ()
    max_turns: int | None = None
    """How many tool-use rounds this step may take before the engine stops it.

    Unset means the engine's default, which is fifty. A step that reaches it is
    not throttled — it is killed: the provider raises rather than returning what
    it had, and the error is *not* one a scope can turn into an outcome, so it
    detonates the whole run. Raise it for a step whose job is to go and check
    things, and expect to.
    """

    reasoning: ReasoningEffort | None = None
    """How much thinking this step may do before answering.

    Unset leaves the workflow's own default, which is usually none at all.

    Not every provider takes it, and the one these pipelines use is currently
    among those that do not: ``claude-agent-sdk`` declares
    ``capabilities.reasoning_effort=None``, so ``conductor validate`` refuses a
    workflow that sets this against it. That refusal is the whole feedback loop
    — ictus does not repeat the check, because Conductor's message names the
    provider and the levels it would accept, which is more than ictus knows.

    The gap is one assignment upstream rather than a missing capability: the CLI
    takes ``--effort``, the SDK exposes ``ClaudeAgentOptions.effort`` and maps it
    straight to that flag, and Conductor's provider reads neither. When that
    lands this field starts working with no change here.
    """

    timeout_seconds: float | None = None
    """Wall-clock ceiling on this step, enforced by the engine from outside.

    The engine cancels the call and raises; nothing partial comes back, and the
    error is not one a scope can turn into an outcome. Distinct from
    ``max_turns``, which counts tool-use rounds rather than time, and from
    ``max_session_seconds``, which asks the *provider* to bound its own session
    rather than being cut off from outside.
    """

    max_session_seconds: float | None = None
    """How long the provider may keep this step's session open.

    The provider's own budget, as against ``timeout_seconds``, which is the
    engine cancelling from outside. Setting it at or above ``timeout_seconds``
    makes it unreachable — the engine gets there first — which is refused rather
    than left as a number that reads like a limit and is not.
    """

    validator: Validator | None = None
    """A second model call that judges this step's answer before the run moves on.

    Off by default because it costs a second call every time and a third when it
    revises. Worth it on a step whose output later steps cannot sanity-check.
    """

    working_dir: str | None = None
    """Where this step reads and writes, overriding the run's own directory.

    A relative path is resolved against the *workflow file's* directory, which
    for ictus is the pipeline's ``build/`` — emitted output that ``ictus emit``
    prunes. That is never what an author means, so the conductor lint refuses
    it: use an absolute path, ``~/...``, or a template resolved at run time.

    On ``claude-agent-sdk`` this moves the step's stdio MCP servers with it.
    They inherit the cwd from the session subprocess rather than being
    configured individually, so there is no way to move one and not the other.
    """

    skills: tuple[str, ...] | None = None
    """Which skills this step may load. Three states, like ``tools``.

    ``None`` takes the workflow's default set, an empty tuple denies every
    skill, and a non-empty one names exactly what to load. Entries are either a
    registered built-in name or a path — Conductor treats an entry as a path
    when it starts with ``~`` or ``.``, or contains a separator — and a relative
    path resolves against ``build/``, so the lint refuses it the same way it
    refuses a relative ``working_dir``.

    Not every provider can load one, and on ``claude-agent-sdk`` a skill must
    live under a plugin root: naming a bare ``.claude/skills/x`` raises at run
    time rather than loading. Reach for ``plugins`` there instead.
    """

    plugins: tuple[str, ...] | None = None
    """Whole plugins this step may use — their skills, subagents and MCP servers.

    Same three states and the same path rule as ``skills``. A plugin is the unit
    a person installs, and enabling one brings all three of the things it ships;
    that is why it is the route to a skill on a provider that will not load a
    loose one.
    """

    retry: RetryPolicy | None = None
    """What to do when the *call* fails, as against when the answer is wrong.

    Not every provider acts on it. Conductor's schema accepts it on any agent,
    and ``claude-agent-sdk`` — the provider these pipelines use — never reads it,
    so the conductor lint refuses the combination rather than letting a workflow
    carry a policy that silently does nothing.
    """

    context_tier: ContextTier | None = None
    """Which context window to ask for, where the model offers a choice.

    Honoured by ``copilot`` and ``aca`` only. Refused on the rest by the
    conductor lint, for the same reason as ``retry``.
    """

    session_key: str | None = None
    dialog_trigger: str | None = None
    """When set, the node may pause after running and converse with the person.

    An evaluator judges the node's output against this criterion and decides
    whether to open a multi-turn conversation. Only a model call can do this —
    Conductor rejects it on gates, scripts, waits and terminals, which is why
    this field exists on this class and nowhere else.
    """

    def __post_init__(self) -> None:
        if not _has_text(self.prompt):
            raise CompositionError(
                f"agent node {self.node_id!r} requires a non-empty prompt; "
                "an agent with no prompt is a billable call that says nothing"
            )
        for field_name in ("timeout_seconds", "max_session_seconds"):
            seconds = getattr(self, field_name)
            if seconds is not None and seconds < 1:
                raise CompositionError(
                    f"agent node {self.node_id!r} sets {field_name}={seconds}; the engine "
                    "requires at least one second, and a sub-second ceiling on a model "
                    "call is a step that never gets to start"
                )
        if (
            self.max_session_seconds is not None
            and self.timeout_seconds is not None
            and self.max_session_seconds >= self.timeout_seconds
        ):
            raise CompositionError(
                f"agent node {self.node_id!r} sets max_session_seconds="
                f"{self.max_session_seconds} at or above timeout_seconds="
                f"{self.timeout_seconds}, so the engine cancels the call before the "
                "provider's own budget can ever fire. Lower one, or drop the other."
            )
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.LLM_CALL

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        return self.declared_outputs

    @property
    def emits_output_schema(self) -> bool:
        return True

    def template_strings(self) -> Iterator[str]:
        if isinstance(self.prompt, str):
            yield self.prompt
        if self.system_prompt is not None:
            yield self.system_prompt
        if self.dialog_trigger is not None:
            yield self.dialog_trigger

    def prompt_refs(self) -> Iterator[Ref]:
        if isinstance(self.prompt, Template):
            yield from self.prompt.refs()


@dataclass(frozen=True, slots=True)
class GateChoice:
    """One option offered to the human at a gate.

    Carries no route: the target belongs to the graph, so ``Pipeline.branch``
    supplies it. That keeps a gate reusable across pipelines and keeps every
    edge in one place.
    """

    value: str
    label: str
    prompt_for: str | None = None
    multiline: bool = False


@dataclass(frozen=True, kw_only=True, eq=False)
class GateNode(Node):
    """A human decision point. Conductor ``type: human_gate``.

    Outgoing edges are emitted as ``options[].route``; a ``routes:`` block on a
    gate validates and is then ignored by the engine.
    """

    prompt: str | Template
    choices: tuple[GateChoice, ...]

    def __post_init__(self) -> None:
        if not _has_text(self.prompt):
            raise CompositionError(f"gate {self.node_id!r} requires a non-empty prompt")
        if not self.choices:
            raise CompositionError(f"gate {self.node_id!r} requires at least one choice")
        values = [c.value for c in self.choices]
        dupes = {v for v in values if values.count(v) > 1}
        if dupes:
            raise CompositionError(
                f"gate {self.node_id!r} has duplicate choice values: {sorted(dupes)}"
            )
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.HUMAN_DECISION

    @property
    def routes_via_options(self) -> bool:
        return True

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """The gate's output shape, which Conductor fixes rather than declares.

        ``selected`` carries the chosen value. Each choice with a ``prompt_for``
        contributes a port of that name, read from ``additional_input``. Exposing
        them as ports is what lets a rejection note be wired back into the loop
        with the same type checking as any other edge.
        """
        ports = [OutputPort("selected", PortType.STRING, "The chosen option value")]
        seen: set[str] = set()
        for choice in self.choices:
            if choice.prompt_for is not None and choice.prompt_for not in seen:
                seen.add(choice.prompt_for)
                ports.append(
                    OutputPort(choice.prompt_for, PortType.STRING, "Free text from the human")
                )
        return tuple(ports)

    def output_ref(self, port_name: str) -> str:
        return port_name if port_name == "selected" else f"additional_input.{port_name}"

    def guard_depth(self, port_name: str) -> int:
        """``selected`` is always there; a free-text field is not.

        Verified against the engine's own Jinja settings: with only the node
        name guarded, reading a field the chosen option never asked for raises
        "'dict object' has no attribute 'notes'", and with ``additional_input``
        absent entirely it raises one segment earlier.
        """
        return 0 if port_name == "selected" else 2

    def template_strings(self) -> Iterator[str]:
        if isinstance(self.prompt, str):
            yield self.prompt

    def prompt_refs(self) -> Iterator[Ref]:
        if isinstance(self.prompt, Template):
            yield from self.prompt.refs()


@dataclass(frozen=True, kw_only=True, eq=False)
class ScriptNode(Node):
    """A subprocess step. Conductor ``type: script`` — no model in the loop."""

    command: str
    args: tuple[str | Template, ...] = ()
    env: Mapping[str, str] | None = None
    stdin: str | Template | None = None
    """What to pipe to the process. A ``Template`` so a step can be handed a
    value another step produced — writing a report to a file is a script whose
    whole payload is somebody else's output."""

    timeout: int | None = None
    working_dir: str | None = None
    declared_outputs: tuple[OutputPort, ...] = ()
    enforce_outputs: bool = True
    """Whether ``declared_outputs`` is also emitted as Conductor's ``output:``.

    On, stdout is a contract: the engine parses it as JSON and raises unless it
    is an object carrying these fields. Off, the ports still type every
    reference at composition and still name what a route may read — Conductor
    merges parsed stdout over ``{stdout, stderr, exit_code}`` either way — but
    nothing is checked once the command has run.

    Off exists for one reason. The validation raise happens *before* routes are
    evaluated, so a command that dies takes the workflow with it and a route
    written for its failure can never fire. Giving the enforcement up is the
    price of the branch, and ``stdlib.try_shell`` is where that trade is made
    deliberately rather than by hand.
    """

    uses: tuple[str, ...] = ()
    """Names of pipeline declarations this step was built from.

    A step built from an ``Integration`` or a ``Datasource`` carries that
    thing's program in ``args``, and from there it is opaque — which means a
    pipeline can reach a production database, or post as somebody, through a
    step whose requirement nothing ever recorded. Preflight would report no
    requirements and pass, the start gate would list nothing, and the first
    anyone knew of it would be a credential missing mid-run.

    Names rather than the objects: this layer knows what a declaration is
    called and never what it is for. The lint resolves them against the
    pipeline's own declarations and refuses one that is not there.
    """

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise CompositionError(f"script node {self.node_id!r} requires a command")
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.SUBPROCESS

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        return self.declared_outputs

    @property
    def emits_output_schema(self) -> bool:
        return self.enforce_outputs

    def template_strings(self) -> Iterator[str]:
        yield from (arg for arg in self.args if isinstance(arg, str))
        if isinstance(self.stdin, str):
            yield self.stdin

    def prompt_refs(self) -> Iterator[Ref]:
        for arg in self.args:
            if isinstance(arg, Template):
                yield from arg.refs()
        if isinstance(self.stdin, Template):
            yield from self.stdin.refs()


@dataclass(frozen=True, kw_only=True, eq=False)
class ComputeNode(Node):
    """A zero-cost computation. Conductor ``type: set`` — no provider call.

    Exactly one of ``value`` or ``values`` is set, which is Conductor's own
    requirement; the constructor rejects both and neither.
    """

    value: str | None = None
    values: Mapping[str, str] | None = None
    value_type: PortType | None = None
    declared_outputs: tuple[OutputPort, ...] = ()

    def __post_init__(self) -> None:
        if (self.value is None) == (self.values is None):
            raise CompositionError(
                f"compute node {self.node_id!r} requires exactly one of 'value' or 'values'"
            )
        if self.values is not None and self.value_type is not None:
            raise CompositionError(
                f"compute node {self.node_id!r} cannot combine 'values' with 'output_type'; "
                "output_type coerces a single 'value'"
            )
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.COMPUTATION

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        return self.declared_outputs

    @property
    def emits_output_schema(self) -> bool:
        """Never. A ``set`` step's shape is decided by the engine, not declared.

        Both forms fail if a schema is written, and both fail *after* the step
        has run. A single ``value:`` produces a scalar, and Conductor refuses a
        schema on one: "declares an output schema but its rendered value is a
        int, not a dict". A ``values:`` block produces a dict, but each binding's
        type comes from a YAML load of its rendered text (executor/set_step.py),
        with no per-key override — so a binding that renders as ``no`` arrives as
        a boolean and a declared ``string`` fails validation.

        The ports stay known to ictus, so references to them are still typed and
        checked at composition. They are simply not written to the file.
        """
        return False

    def output_ref(self, port_name: str) -> str:
        """A single ``value:`` is stored as the bare scalar, not wrapped in a key.

        Verified on a live run: reading ``tally.output.value`` off a
        ``value:``/``output_type: number`` step raises "'int object' has no
        attribute 'value'". Only the ``values:`` form produces a mapping, so
        only it has sub-paths.
        """
        return port_name if self.values is not None else ""

    def template_strings(self) -> Iterator[str]:
        if self.value is not None:
            yield self.value
        if self.values is not None:
            yield from self.values.values()


@dataclass(frozen=True, kw_only=True, eq=False)
class WaitNode(Node):
    """A timed pause. Conductor ``type: wait`` — costs one iteration, no model."""

    duration: float
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise CompositionError(
                f"wait node {self.node_id!r} requires a positive duration, got {self.duration}"
            )
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.DELAY

    def template_strings(self) -> Iterator[str]:
        if self.reason is not None:
            yield self.reason


@dataclass(frozen=True, kw_only=True, eq=False)
class TerminateNode(Node):
    """An explicit exit. Conductor ``type: terminate``.

    A node with no outgoing edge implicitly means ``$end``, which makes a
    forgotten edge indistinguishable from an intended finish. Terminating
    explicitly gives the run a distinguishable exit status and marks the stop
    as deliberate in the event log.
    """

    status: Literal["success", "failed"]
    reason: str | Template
    result: Mapping[str, str | Template] | None = None

    def __post_init__(self) -> None:
        if not _has_text(self.reason):
            raise CompositionError(f"terminate node {self.node_id!r} requires a non-empty reason")
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.EXIT

    @property
    def accepts_routes(self) -> bool:
        return False

    def template_strings(self) -> Iterator[str]:
        if isinstance(self.reason, str):
            yield self.reason

    def settled_template_strings(self) -> Iterator[str]:
        for value in (self.result or {}).values():
            if isinstance(value, str):
                yield value

    def prompt_refs(self) -> Iterator[Ref]:
        if isinstance(self.reason, Template):
            yield from self.reason.refs()

    def settled_refs(self) -> Iterator[Ref]:
        for value in (self.result or {}).values():
            if isinstance(value, Template):
                yield from value.refs()


@dataclass(frozen=True, slots=True)
class Question:
    """One thing to ask a person.

    ``id`` is the key its answer lands under, so downstream references survive
    a question being inserted above them. Leave it unset only for questions
    nothing reads by name.
    """

    text: str
    id: str | None = None
    hint: str | None = None
    choices: tuple[str, ...] = ()
    allow_free_text: bool = True
    default: str | None = None
    required: bool = False
    multiline: bool = True

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise CompositionError("a question needs text")
        if not self.choices and not self.allow_free_text:
            raise CompositionError(
                f"question {self.id or self.text!r} is unanswerable: no choices and no free "
                "text, so there is nothing the person can do with it"
            )
        if self.id is not None and slugify(self.id) != self.id:
            raise CompositionError(
                f"question id {self.id!r} is not an identifier; use {slugify(self.id)!r}"
            )


@dataclass(frozen=True, kw_only=True, eq=False)
class QuestionsNode(Node):
    """Ask a person for things the run could not work out. ``type: questions``.

    Distinct from a gate: a gate offers a decision among known options, this
    collects *values*. All of them in one engine step — N questions cost one
    iteration, not N.

    Either the questions are known when the pipeline is written, or an upstream
    node produces them: a ticket touching an unknown number of repositories
    cannot have its questions written in advance. Exactly one of ``questions``
    and ``source`` is set, which is Conductor's rule and this constructor's.
    """

    questions: tuple[Question, ...] = ()
    source: Ref | None = None
    allow_back: bool | None = None
    allow_skip: bool | None = None
    allow_skip_all: bool | None = None
    allow_abort: bool | None = None

    def __post_init__(self) -> None:
        if bool(self.questions) == (self.source is not None):
            raise CompositionError(
                f"questions node {self.node_id!r} needs exactly one of 'questions' "
                "(known when written) or 'source' (produced by an earlier node)"
            )
        ids = [q.id for q in self.questions if q.id]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise CompositionError(
                f"questions node {self.node_id!r} reuses answer id(s) {sorted(dupes)}"
            )
        super().__post_init__()

    @property
    def kind(self) -> NodeKind:
        return NodeKind.ASK

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """The fixed shape, plus one port per question that named itself.

        Conductor fixes the output of this kind, so nothing is declared by the
        author — but an inline question with an ``id`` is known here, and giving
        it a port is what lets ``node.ref("repo_path")`` be checked.
        """
        ports = [
            OutputPort("answers", PortType.OBJECT, "Every answer, keyed by question id"),
            OutputPort("transcript", PortType.STRING, "The exchange, as text"),
            OutputPort("answered_count", PortType.NUMBER, "How many were answered"),
            OutputPort("outcome", PortType.STRING, "How the exchange ended"),
        ]
        reserved = {p.name for p in ports}
        ports += [
            OutputPort(q.id, PortType.STRING, q.text)
            for q in self.questions
            if q.id and q.id not in reserved
        ]
        return tuple(ports)

    def output_ref(self, port_name: str) -> str:
        fixed = {"answers", "transcript", "answered_count", "outcome"}
        return port_name if port_name in fixed else f"answers.{port_name}"

    def template_strings(self) -> Iterator[str]:
        for question in self.questions:
            yield question.text
            if question.hint is not None:
                yield question.hint

    def kind_flags(self) -> dict[str, bool]:
        """Navigation flags the author set explicitly."""
        named = {
            "allow_back": self.allow_back,
            "allow_skip": self.allow_skip,
            "allow_skip_all": self.allow_skip_all,
            "allow_abort": self.allow_abort,
        }
        return {k: v for k, v in named.items() if v is not None}


@dataclass(frozen=True, kw_only=True, eq=False)
class SubGraphNode(Node):
    """A nested workflow. Conductor ``type: workflow``.

    Emitted by ``Stage``; not usually constructed directly. This is Conductor's
    only nesting construct — it has its own entry point, its own graph, and
    costs the parent exactly one iteration.
    """

    target: str
    declared_outputs: tuple[OutputPort, ...] = ()
    max_depth: int | None = None

    @property
    def kind(self) -> NodeKind:
        return NodeKind.SUB_GRAPH

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """The child's exposed results.

        Known to ictus but never emitted: the child declares its own ``output:``
        map, and re-declaring a schema on the parent node would be a second
        source of truth for the same values.
        """
        return self.declared_outputs


OUTCOME_PORT = "outcome"


@dataclass(frozen=True, kw_only=True, eq=False)
class ScopeNode(SubGraphNode):
    """A scope placed in a parent, carrying its outcome vocabulary with it.

    The vocabulary travels on the node rather than living in the parent's head:
    ``branch_on_outcome`` reads it back to check that every exit is routed and
    that no route names an outcome the scope cannot produce.
    """

    outcomes: tuple[str, ...] = ()

    @property
    def outcome(self) -> Ref:
        """The port a parent branches on."""
        return self.ref(OUTCOME_PORT)


def render_output_schema(ports: Sequence[OutputPort]) -> YamlDict:
    """Lower output ports to Conductor's ``output:`` block."""
    out: YamlDict = {}
    for port in ports:
        entry: dict[str, YamlValue] = {"type": port.port_type.value}
        if port.description:
            entry["description"] = port.description
        if port.element is not None:
            # The shape each entry must have. This is the only thing that tells
            # the model what keys to emit; a fan-out over the array reads them.
            entry["items"] = {
                "type": "object",
                "properties": {
                    name: {"type": port_type.value} for name, port_type in port.element.items()
                },
            }
        out[port.name] = entry
    return out
