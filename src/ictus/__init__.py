"""Typed composition for Conductor workflows."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from ictus.errors import CompositionError, EmitError, LintError, PortTypeError, UnknownPortError
from ictus.graph.composition import END, Edge, WorkflowInput
from ictus.graph.node import (
    AgentNode,
    Backoff,
    ComputeNode,
    ContextTier,
    GateChoice,
    GateNode,
    Node,
    Question,
    QuestionsNode,
    ReasoningEffort,
    RetryOn,
    RetryPolicy,
    ScriptNode,
    SubGraphNode,
    TerminateNode,
    Validator,
    WaitNode,
    slugify,
)
from ictus.graph.pipeline import Pipeline
from ictus.graph.ports import InputPort, OutputPort, PortConnection, PortType
from ictus.graph.ref import (
    Ref,
    Template,
    at_least,
    equals,
    every,
    not_equals,
    not_every,
    optional,
    ref_to,
    tpl,
)
from ictus.graph.requirements import (
    Datasource,
    EnvVar,
    Executable,
    Integration,
    McpServer,
    McpTransport,
)
from ictus.graph.scope import Scope, ScopeNode, outcome_scope
from ictus.graph.signals import RunSignal
from ictus.graph.stage import Stage

try:
    __version__ = version("ictus")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "END",
    "AgentNode",
    "Backoff",
    "CompositionError",
    "ComputeNode",
    "ContextTier",
    "Datasource",
    "Edge",
    "EmitError",
    "EnvVar",
    "Executable",
    "GateChoice",
    "GateNode",
    "InputPort",
    "Integration",
    "LintError",
    "McpServer",
    "McpTransport",
    "Node",
    "OutputPort",
    "Pipeline",
    "PortConnection",
    "PortType",
    "PortTypeError",
    "Question",
    "QuestionsNode",
    "ReasoningEffort",
    "Ref",
    "RetryOn",
    "RetryPolicy",
    "RunSignal",
    "Scope",
    "ScopeNode",
    "ScriptNode",
    "Stage",
    "SubGraphNode",
    "Template",
    "TerminateNode",
    "UnknownPortError",
    "Validator",
    "WaitNode",
    "WorkflowInput",
    "__version__",
    "at_least",
    "equals",
    "every",
    "not_equals",
    "not_every",
    "optional",
    "outcome_scope",
    "ref_to",
    "slugify",
    "tpl",
]
