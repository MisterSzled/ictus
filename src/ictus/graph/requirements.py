"""What a pipeline needs from its environment before it can run.

Declared where the pipeline is written, checked before it launches. The point is
that "this needs a GitHub token" stops being knowledge in someone's head and
becomes a thing the tooling can refuse on.

Secrets are deliberately *not* modelled as values. An ``EnvVar`` names a
variable that must be present in the environment at run time; ictus checks it is
set and never reads or emits it. A committed workflow file with a token in it is
a worse problem than an unconfigured one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ictus.errors import CompositionError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ictus.graph.signals import RunSignal

__all__ = ["Datasource", "EnvVar", "Executable", "Integration", "McpServer", "McpTransport"]


class McpTransport(StrEnum):
    """How a client reaches an MCP server."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class EnvVar:
    """An environment variable that must be set before the pipeline runs.

    ``purpose`` is shown to whoever has to go and set it, so write it for them:
    "a GitHub token with repo:read" beats "the token".
    """

    name: str
    purpose: str = ""
    secret: bool = True


@dataclass(frozen=True, slots=True)
class Executable:
    """A command that must be on ``PATH`` before the pipeline runs.

    Declared for the same reason an ``EnvVar`` is: so "this pipeline reads
    Conductor's own source" stops being knowledge in the author's head and
    becomes something preflight can refuse on.

    The failure this exists to prevent is not a crash. A step told to go and
    check something against a tool that is not reachable does not fail — it
    reports that the thing it was checking does not exist, which is a confident
    wrong answer that costs a whole run to produce and looks exactly like a
    considered one. Better to refuse at the launch, for free.

    ``probe`` are arguments that prove the command actually answers, run only
    when preflight is probing. ``--version`` is the usual one. Left empty, the
    check is presence on ``PATH`` and nothing more.
    """

    name: str
    purpose: str
    probe: tuple[str, ...] = ()
    setup_hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise CompositionError("an executable requirement needs a command name")
        if not self.purpose:
            raise CompositionError(
                f"executable {self.name!r} needs a purpose; it is what the person "
                "being asked to install it will read"
            )


@dataclass(frozen=True, slots=True)
class McpServer:
    """An MCP server a pipeline needs access to.

    Transport-specific fields are checked here rather than at emission, so an
    ``http`` server with no URL is refused while the pipeline is being written.
    """

    name: str
    purpose: str
    transport: McpTransport = McpTransport.STDIO
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    env: tuple[EnvVar, ...] = ()
    tools: tuple[str, ...] = ()
    timeout_ms: int | None = None
    setup_hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise CompositionError("an MCP server needs a name")
        if not self.purpose:
            raise CompositionError(
                f"MCP server {self.name!r} needs a purpose; it is what the person "
                "being asked to configure it will read"
            )
        if self.transport is McpTransport.STDIO:
            if not self.command:
                raise CompositionError(
                    f"MCP server {self.name!r} uses stdio and needs a command to run"
                )
            if self.url is not None:
                raise CompositionError(
                    f"MCP server {self.name!r} uses stdio; a url belongs to http or sse"
                )
        else:
            if not self.url:
                raise CompositionError(
                    f"MCP server {self.name!r} uses {self.transport.value} and needs a url"
                )
            if self.command is not None:
                raise CompositionError(
                    f"MCP server {self.name!r} uses {self.transport.value}; "
                    "a command belongs to stdio"
                )

    @property
    def required_env(self) -> tuple[EnvVar, ...]:
        """Environment variables that must be set for this server to work."""
        return self.env


@dataclass(frozen=True, slots=True)
class Integration:
    """A third-party service a pipeline talks to, declared where it is written.

    The air gap. Nothing in the composition model knows Slack, or any other
    service, exists — the same way nothing here knows what an MCP server is for.
    This holds the *shape* of an integration: what it is called, why it is there,
    what the environment must supply, and an opaque program that sends one
    report. Who fills that in lives behind ``ictus.notify``, and a second service
    is a new module there rather than a new branch anywhere else.

    Declared at the top of a pipeline on purpose. A reader should see what a run
    will talk to before they read what it does, and whoever approves the run is
    shown the same list at the start gate — a pipeline that reaches outside the
    machine should say so where it cannot be missed.

    ``env`` names variables, never values. A bot token or a webhook URL is the
    whole authorisation to act as somebody, so it is read at run time and never
    written into a pipeline or an emitted workflow.
    """

    name: str
    purpose: str
    env: tuple[EnvVar, ...] = ()
    reports: tuple[RunSignal, ...] = ()
    """Which moments this integration is told about, when it is attached.

    Empty means it is attached by hand — a pipeline that announces at points of
    its own choosing rather than at every gate.
    """

    command: str = "python3"
    program: str = ""
    """How one report is sent. Opaque here, and never read above ``notify``."""

    threads: bool = False
    """Whether a report can be hung under an earlier one.

    Not every destination has threads, and one that does not gets a flat
    sequence rather than a broken reference to a parent that never existed.
    """

    announces: bool = True
    """Whether its program sends a *report* — free text, optionally under a
    parent, optionally carrying buttons."""

    comments: bool = False
    """Whether its program adds a *remark to a named item* — a ticket, an issue,
    a pull request — addressed by something the graph carries.

    Separate from ``announces`` because the two programs are handed different
    things, and an integration built for one driven by a step built for the
    other would be passed arguments in positions that mean something else. The
    flags make that a refusal while the pipeline is being written; without them
    it is a report that silently goes nowhere."""

    listens: bool = False
    """Whether a run can be *started* from this destination as well as reported
    to it.

    Not the same capability: sending a report needs somewhere to post, and
    waiting for one needs a connection held open and a credential that permits
    it. A destination that can only be written to is refused by ``listen_on``
    while the pipeline is being written, rather than by a listener that starts,
    reports itself as listening, and never fires.
    """

    setup_hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise CompositionError("an integration needs a name")
        if not self.purpose:
            raise CompositionError(
                f"integration {self.name!r} needs a purpose; it is what the person "
                "being asked to configure it will read"
            )
        if not self.program:
            raise CompositionError(
                f"integration {self.name!r} has no program, so it could never send "
                "anything. Build it with one of the constructors in ictus.notify."
            )
        duplicated = sorted({s for s in self.reports if self.reports.count(s) > 1})
        if duplicated:
            raise CompositionError(
                f"integration {self.name!r} names {[s.value for s in duplicated]} more "
                "than once; a signal is reported once or not at all"
            )

    @property
    def required_env(self) -> tuple[EnvVar, ...]:
        """Environment variables that must be set for this to work."""
        return self.env

    def wants(self, signal: RunSignal) -> bool:
        """Whether this integration asked to hear about ``signal``."""
        return signal in self.reports


@dataclass(frozen=True, slots=True)
class Datasource:
    """Somewhere a pipeline reads data *from*, declared where it is written.

    The same air gap as ``Integration``, one axis over. Nothing in the
    composition model knows Postgres, or any other engine, exists: this holds
    the shape — what it is called, why it is there, what the environment must
    supply, and an opaque program that runs one statement and prints what came
    back. Which engine that is lives behind ``ictus.sources``, and a second one
    is a new module there rather than a new branch anywhere else.

    Separate from ``Integration`` because it is the other direction. An
    integration is an audience and is told things; a datasource is asked things
    and answers. Conflating them would put ``reports`` and ``threads`` on a
    database, and a query on a channel.

    ``read_only`` is a claim about the *connection*, not about the statement.
    It travels with the declaration so a step that must not write can refuse a
    connection that could, at the point the pipeline is written rather than at
    the point somebody's generated SQL reaches a production table.
    """

    name: str
    purpose: str
    env: tuple[EnvVar, ...] = ()
    read_only: bool = False
    command: str = "python3"
    program: str = ""
    """How one statement is run. Opaque here, and never read above ``sources``."""

    needs: tuple[Executable, ...] = ()
    """Commands the program shells out to, so preflight can refuse a machine
    without them instead of a step discovering it mid-run."""

    setup_hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise CompositionError("a datasource needs a name")
        if not self.purpose:
            raise CompositionError(
                f"datasource {self.name!r} needs a purpose; it is what the person "
                "being asked to configure it will read"
            )
        if not self.program:
            raise CompositionError(
                f"datasource {self.name!r} has no program, so it could never read "
                "anything. Build it with one of the constructors in ictus.sources."
            )

    @property
    def required_env(self) -> tuple[EnvVar, ...]:
        """Environment variables that must be set for this to work."""
        return self.env
