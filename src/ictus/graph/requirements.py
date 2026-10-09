"""What a pipeline needs from its environment before it can run.

Declared where the pipeline is written, checked before it launches.

Secrets are names, never values. An ``EnvVar`` names a variable that must be
set at run time; ictus checks it is and never reads or emits it.
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

    ``purpose`` is shown to whoever has to go and set it.
    """

    name: str
    purpose: str = ""
    secret: bool = True


@dataclass(frozen=True, slots=True)
class Executable:
    """A command that must be on ``PATH`` before the pipeline runs.

    ``probe`` are arguments that prove the command answers, run only when
    preflight is probing; ``--version`` is the usual one. Left empty, the check
    is presence on ``PATH``.
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

    Transport-specific fields are checked here, not at emission.
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
        return self.env


@dataclass(frozen=True, slots=True)
class Integration:
    """A third-party service a pipeline talks to, declared where it is written.

    The shape only: a name, a purpose, what the environment must supply, and an
    opaque program that sends one report. The concrete service lives behind
    ``ictus.notify``; nothing here knows one exists.

    ``env`` names variables, never values.
    """

    name: str
    purpose: str
    env: tuple[EnvVar, ...] = ()
    reports: tuple[RunSignal, ...] = ()
    """Which moments this integration is told about when it is attached.

    Empty means it is attached by hand.
    """

    command: str = "python3"
    program: str = ""
    """How one report is sent. Opaque here, and never read above ``notify``."""

    threads: bool = False
    """Whether a report can be hung under an earlier one.

    A destination without threads gets a flat sequence.
    """

    announces: bool = True
    """Whether its program sends a *report* — free text, optionally under a
    parent, optionally carrying buttons."""

    comments: bool = False
    """Whether its program adds a remark to a named item — a ticket, an issue,
    a pull request — addressed by something the graph carries.

    Separate from ``announces``: the two programs take different arguments."""

    listens: bool = False
    """Whether a run can be started from this destination as well as reported to.

    A different capability: it needs a held connection and a credential that
    permits one. ``listen_on`` refuses a destination without it.
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
    """Somewhere a pipeline reads data from, declared where it is written.

    The shape only: a name, a purpose, what the environment must supply, and an
    opaque program that runs one statement and prints what came back. The
    concrete engine lives behind ``ictus.sources``.

    ``read_only`` is a claim about the connection, not about the statement.
    """

    name: str
    purpose: str
    env: tuple[EnvVar, ...] = ()
    read_only: bool = False
    command: str = "python3"
    program: str = ""
    """How one statement is run. Opaque here, and never read above ``sources``."""

    needs: tuple[Executable, ...] = ()
    """Commands the program shells out to, for preflight to check."""

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
