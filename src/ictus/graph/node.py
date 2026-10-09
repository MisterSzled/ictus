"""Node kinds — one frozen class per Conductor agent type.

Each kind carries only the fields Conductor permits on it, so an illegal
combination is unrepresentable. Routing is not modelled here; ``Pipeline``
owns it.
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
    from collections.abc import Hashable, Iterable, Iterator, Mapping

__all__ = [
    "NODE_KINDS",
    "OUTCOME_PORT",
    "AgentNode",
    "Backoff",
    "ComputeNode",
    "ContextTier",
    "GateChoice",
    "GateNode",
    "Node",
    "NodeKind",
    "Question",
    "QuestionsNode",
    "ReasoningEffort",
    "RetryOn",
    "RetryPolicy",
    "ScopeNode",
    "ScriptNode",
    "SubGraphNode",
    "TerminateNode",
    "Validator",
    "WaitNode",
    "coerced_outcome",
    "slugify",
]


class NodeKind(StrEnum):
    """What a node does, named for the work rather than for any engine.

    ``Capabilities`` is declared in these terms.
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
    """A category of failure worth attempting again. Transient only."""

    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"


class ReasoningEffort(StrEnum):
    """How much thinking a step is allowed before it answers.

    A token budget charged per call, whether or not the step needed it. On
    Anthropic roughly 2k, 8k, 16k, 32k and 60k thinking tokens.
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

    ``attempts`` counts the first try, so 1 means no retry and is the default.
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
        if _repeated(self.on):
            raise CompositionError(f"retry policy repeats a failure category: {list(self.on)}")


def _repeated[T: Hashable](items: Iterable[T]) -> list[T]:
    """Values appearing more than once, in the order they first repeat.

    Returns rather than raises, so each caller keeps its own wording: a port
    declared twice, a gate choice reused and an answer id reused are three
    different mistakes, and in each the message is most of the value. There
    were four spellings of this one idea in this file — a ``seen`` set, two
    ``list.count`` comprehensions and a ``len(set(...))`` — and the
    ``count`` ones are quadratic besides.

    ``requirements.py`` has a fifth and deliberately keeps it: it needs the
    members back to render ``RunSignal.value``, and importing this would give
    the module that ``notify/`` and ``sources/`` both depend on a run-time edge
    into the largest module in ``graph``.
    """
    seen: set[T] = set()
    repeats: list[T] = []
    for item in items:
        if item in seen and item not in repeats:
            repeats.append(item)
        seen.add(item)
    return repeats


def _has_text(prompt: str | Template) -> bool:
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


#: Words Conductor's renderer turns into something that is not a string.
_COERCED = frozenset({"True", "False", "None", "true", "false", "null"})


def coerced_outcome(name: str) -> bool:
    """Whether Conductor would hand this outcome back as a non-string.

    A rendered output goes through ``json.loads`` (`_maybe_parse_json`), so
    ``"true"``, ``"3"`` and anything opening a JSON container arrive as a bool,
    a number or a structure — and every ``equals`` comparison against the name
    then fails without saying anything. Lives here rather than in ``scope.py``
    because ``ScopeNode`` can be constructed directly, without the builder.
    """
    stripped = name.strip()
    if stripped in _COERCED or stripped[:1] in '{["':
        return True
    try:
        float(name)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, kw_only=True, eq=False)
class Node(ABC):
    """A single step in a workflow.

    ``node_id`` is the identity routing resolves against; ``description`` is the
    human-facing string. There is no third label field.
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
        names = [p.name for p in self.inputs] + [p.name for p in self.outputs]
        if repeats := _repeated(names):
            raise CompositionError(
                f"node {self.node_id!r} declares port {repeats[0]!r} more than once"
            )

    @property
    @abstractmethod
    def kind(self) -> NodeKind:
        """What this node does, in engine-neutral terms."""

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """Values this node produces. Empty where ``output:`` is forbidden."""
        return ()

    @property
    def routes_via_options(self) -> bool:
        """Whether outgoing edges are emitted as ``options[].route``. Gates only."""
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

        Identity except on a gate, whose output shape the engine fixes.
        """
        return port_name

    def guard_depth(self, port_name: str) -> int:
        """How many trailing path segments a guard must test individually.

        Zero unless a segment can be absent even though the step ran, which is
        the case for a gate's free-text field.
        """
        _ = port_name
        return 0

    def template_strings(self) -> Iterator[str]:
        """Author-written raw strings a backend renders. Typed refs go to ``prompt_refs``."""
        return iter(())

    def prompt_refs(self) -> Iterator[Ref]:
        """Typed references this node reads, rendered in the node's own context."""
        return iter(())

    def settled_template_strings(self) -> Iterator[str]:
        """Raw strings rendered with the whole run in scope, not the node's inputs.

        A terminal's output payload is rendered this way, so the explicit-mode
        rule that governs a prompt does not apply.
        """
        return iter(())

    def settled_refs(self) -> Iterator[Ref]:
        """Typed references in the accumulate-rendered slots."""
        return iter(())

    def ref(self, port: str) -> Ref:
        """A typed reference to one of this node's outputs. Raises on an undeclared port."""
        declared = self.get_output(port)
        return Ref(
            source_id=self.node_id,
            port=declared.name,
            element=declared.element,
            port_type=declared.port_type,
            source=self,
        )

    def get_input(self, name: str) -> InputPort:
        for port in self.inputs:
            if port.name == name:
                return port
        known = ", ".join(p.name for p in self.inputs) or "(none)"
        raise UnknownPortError(
            f"node {self.node_id!r} has no input port {name!r}; declared inputs: {known}"
        )

    def get_output(self, name: str) -> OutputPort:
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

    A second model call checking content, as against ``declared_outputs``
    (shape) and ``retry`` (a call that fell over). Costs two model calls per
    step, three when the revision fires.
    """

    criteria: str
    model: str | None = None
    """A cheaper model for the grading pass. Unset reuses the step's own."""

    revise: bool = True
    """Whether a failed check re-runs the step once with the feedback attached.

    The engine caps this at one revision. Off, the check still runs and reports.
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
    """

    declared_outputs: tuple[OutputPort, ...] = ()
    max_turns: int | None = None
    """How many tool-use rounds this step may take before the engine stops it.

    Unset means the engine's default of fifty. Reaching it raises and kills the
    run; no scope can turn that into an outcome.
    """

    reasoning: ReasoningEffort | None = None
    """How much thinking this step may do before answering.

    Unset leaves the workflow's default. Not every provider accepts it;
    ``claude-agent-sdk`` does not, and ``conductor validate`` refuses it there.
    """

    timeout_seconds: float | None = None
    """Wall-clock ceiling on this step, enforced by the engine from outside.

    The call is cancelled and raises; nothing partial comes back.
    """

    max_session_seconds: float | None = None
    """How long the provider may keep this step's session open.

    The provider's own budget. Must be below ``timeout_seconds`` or it is
    unreachable, which is refused.
    """

    validator: Validator | None = None
    """A second model call that judges this step's answer before the run moves on."""

    working_dir: str | None = None
    """Where this step reads and writes, overriding the run's own directory.

    A relative path resolves against the workflow file's directory — ``build/``
    — so the conductor lint refuses one. Use absolute, ``~/...`` or a template.
    On ``claude-agent-sdk`` this moves the step's stdio MCP servers too.
    """

    skills: tuple[str, ...] | None = None
    """Which skills this step may load. Three states, like ``tools``.

    An entry is a registered name or a path; a path starts with ``~`` or ``.``
    or contains a separator, and a relative one is refused by the lint. On
    ``claude-agent-sdk`` a skill must live under a plugin root — use ``plugins``.
    """

    plugins: tuple[str, ...] | None = None
    """Whole plugins this step may use — their skills, subagents and MCP servers.

    Same three states and path rule as ``skills``.
    """

    retry: RetryPolicy | None = None
    """What to do when the call fails, as against when the answer is wrong.

    ``claude-agent-sdk`` never reads it; the conductor lint refuses it there.
    """

    context_tier: ContextTier | None = None
    """Which context window to ask for. Honoured by ``copilot`` and ``aca`` only."""

    session_key: str | None = None
    dialog_trigger: str | None = None
    """When set, the node may pause after running and converse with the person.

    An evaluator judges the output against this criterion. Model calls only.
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
    """One option offered to the human at a gate. The route comes from ``Pipeline.branch``."""

    value: str
    label: str
    prompt_for: str | None = None
    multiline: bool = False


@dataclass(frozen=True, kw_only=True, eq=False)
class GateNode(Node):
    """A human decision point. Conductor ``type: human_gate``.

    Outgoing edges are emitted as ``options[].route``; ``routes:`` is ignored here.
    """

    prompt: str | Template
    choices: tuple[GateChoice, ...]

    def __post_init__(self) -> None:
        if not _has_text(self.prompt):
            raise CompositionError(f"gate {self.node_id!r} requires a non-empty prompt")
        if not self.choices:
            raise CompositionError(f"gate {self.node_id!r} requires at least one choice")
        values = [c.value for c in self.choices]
        dupes = _repeated(values)
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

        ``selected`` carries the chosen value; each ``prompt_for`` contributes a
        port of that name, read from ``additional_input``.
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
        """``selected`` is always there; a free-text field is not."""
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
    """What to pipe to the process. A ``Template`` to pipe another step's output."""

    timeout: int | None = None
    working_dir: str | None = None
    declared_outputs: tuple[OutputPort, ...] = ()
    enforce_outputs: bool = True
    """Whether ``declared_outputs`` is also emitted as Conductor's ``output:``.

    On, the engine parses stdout as JSON and raises unless it carries these
    fields — before routes are evaluated, so a failure route can never fire.
    Off, the ports still type references at composition but nothing is checked.
    """

    uses: tuple[str, ...] = ()
    """Names of pipeline declarations this step was built from.

    What preflight and the start gate read, since the program itself is opaque
    once it is in ``args``. Names only; the lint resolves them against the
    pipeline's declarations.
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

    Exactly one of ``value`` or ``values`` is set.
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

        The ports stay known to ictus and are still typed at composition.
        """
        return False

    def output_ref(self, port_name: str) -> str:
        """A single ``value:`` is the bare scalar; only ``values:`` has sub-paths."""
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

    Gives the run an exit status, which a node simply left unrouted does not.
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

    ``id`` is the key its answer lands under. Unset for answers nothing reads.
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
    """Ask a person for values. Conductor ``type: questions``.

    A gate offers a decision; this collects values. N questions cost one
    iteration. Exactly one of ``questions`` (known when written) and ``source``
    (produced by an earlier node) is set.
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
        dupes = _repeated(ids)
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
        """The engine-fixed shape, plus one port per question that named itself."""
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

    Emitted by ``Stage``. Has its own entry point and graph, and costs the
    parent one iteration.
    """

    target: str
    declared_outputs: tuple[OutputPort, ...] = ()
    max_depth: int | None = None

    @property
    def kind(self) -> NodeKind:
        return NodeKind.SUB_GRAPH

    @property
    def outputs(self) -> tuple[OutputPort, ...]:
        """The child's exposed results. Known to ictus, never emitted."""
        return self.declared_outputs


OUTCOME_PORT = "outcome"


@dataclass(frozen=True, kw_only=True, eq=False)
class ScopeNode(SubGraphNode):
    """A scope placed in a parent, carrying its outcome vocabulary with it.

    ``branch_on_outcome`` reads the vocabulary back to check the routing.
    """

    outcomes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.outcomes:
            if coerced_outcome(name):
                raise CompositionError(
                    f"scope {self.node_id!r} cannot use the outcome {name!r}: Conductor "
                    "parses a rendered output with json.loads, so it would arrive as a "
                    "non-string and every comparison against it would silently fail"
                )
        super().__post_init__()

    @property
    def outcome(self) -> Ref:
        """The port a parent branches on."""
        return self.ref(OUTCOME_PORT)
