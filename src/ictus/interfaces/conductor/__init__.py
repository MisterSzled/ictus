"""The Conductor backend.

The only package that may know Conductor's spelling: field names, template
dialect, iteration accounting, CLI.

    emit/        a pure function of a pipeline: the workflow block, one node
                 to one ``agents:`` entry, templates, the manifest, YAML text
    control/     acts on a run that exists: launching it, its events, its log
    preflight.py asks about this machine
    lints.py     rules that are true because of how Conductor runs

This file is the backend class those three are reached through.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from ictus.errors import IctusError
from ictus.graph.node import NODE_KINDS
from ictus.interfaces import Capabilities, Document, PreflightIssue, ValidationResult
from ictus.interfaces.conductor.control.launch import binary, launch_command
from ictus.interfaces.conductor.control.signals import REPORTABLE
from ictus.interfaces.conductor.emit import manifest
from ictus.interfaces.conductor.emit.agents import agent_entry
from ictus.interfaces.conductor.emit.mapping import for_each_block
from ictus.interfaces.conductor.emit.parallel import parallel_block
from ictus.interfaces.conductor.emit.serialize import dump_yaml
from ictus.interfaces.conductor.emit.templates import output_block
from ictus.interfaces.conductor.emit.workflow import NOTHING_INHERITED, Inherited, workflow_block
from ictus.interfaces.conductor.lints import conductor_problems
from ictus.interfaces.conductor.preflight import preflight_issues
from ictus.interfaces.environment import (
    datasource_issues,
    executable_issues,
    integration_issues,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path

    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["ConductorBackend", "conductor"]


class ConductorBackend:
    """Compiles an ictus graph to Conductor workflow YAML and drives its CLI."""

    def capabilities(self) -> Capabilities:
        """Conductor expresses every node kind ictus models."""
        return Capabilities(
            name="conductor",
            kinds=frozenset(NODE_KINDS),
            # AgentDef.provider / ProviderSettings.name (config/schema.py) is a
            # closed Literal; anything else is rejected by the loader.
            providers=frozenset(
                {"copilot", "openai", "claude", "claude-agent-sdk", "hermes", "aca"}
            ),
            signals=REPORTABLE,
            # Conductor's `tools:` holds workflow tool names, which
            # claude-agent-sdk cannot translate to CLI tool ids; it raises
            # ProviderError on a non-empty list.
            tool_allowlists=False,
            # `capabilities.session_continuity` is true for this one alone;
            # config/validator.py `_check_agent_capabilities` rejects a session_key
            # on any other.
            remembering_providers=frozenset({"claude-agent-sdk"}),
            conditional_routes=True,
            cycles=True,
            sub_graphs=True,
            notes="Loops are bounded by a single global step budget, not per cycle.",
        )

    def document(self, pipeline: Pipeline, inherited: Inherited = NOTHING_INHERITED) -> YamlDict:
        """The workflow mapping for one pipeline, before serialization."""
        # A map group's body lives inline under `for_each:`, not in `agents:`.
        # An unset system prompt is an empty one to the engine, not a default,
        # so what a step inherits is decided here and passed down.
        baseline = pipeline.system_prompt or inherited.system_prompt
        agents: list[YamlValue] = [
            agent_entry(pipeline, node, baseline)
            for node in pipeline.nodes
            if pipeline.map_of(node) is None
        ]
        doc: YamlDict = {"workflow": workflow_block(pipeline, inherited), "agents": agents}
        groups = parallel_block(pipeline)
        if groups:
            doc["parallel"] = groups
        mapped = for_each_block(pipeline)
        if mapped:
            doc["for_each"] = mapped
        exposed = output_block(pipeline)
        if exposed:
            doc["output"] = exposed
        return doc

    def compile(
        self, pipeline: Pipeline, inherited: Inherited = NOTHING_INHERITED
    ) -> list[Document]:
        """Render ``pipeline`` and every stage it contains.

        The parent first, then one file per nested stage. A stage placed twice
        is two nodes over one file.
        """
        out = [
            Document(f"{pipeline.pipeline_id}.yaml", dump_yaml(self.document(pipeline, inherited)))
        ]
        # Only at top level: a stage has no run of its own to start.
        if inherited is NOTHING_INHERITED:
            listening = manifest.render(pipeline)
            if listening:
                out.append(Document(manifest.filename_for(pipeline), listening))
        seen = {out[0].filename}
        below = inherited.under(pipeline)
        for child in pipeline.children.values():
            for rendered in self.compile(child, below):
                if rendered.filename not in seen:
                    seen.add(rendered.filename)
                    out.append(rendered)
        return out

    def lint(self, pipeline: Pipeline) -> list[str]:
        """Conductor-specific problems the generic rules cannot know."""
        return conductor_problems(pipeline)

    def preflight(self, pipeline: Pipeline, *, probe: bool) -> list[PreflightIssue]:
        """Check this environment can supply what the pipeline declares.

        Whether an MCP server is installed, its token set and its endpoint
        answering, plus declared executables, datasources and integrations.
        """
        return [
            *executable_issues(pipeline, probe=probe),
            *integration_issues(pipeline),
            *datasource_issues(pipeline),
            *preflight_issues(pipeline, probe=probe),
        ]

    def validate(self, paths: Sequence[Path]) -> list[ValidationResult]:
        """Ask Conductor's own validator whether each document loads."""
        binary = self._binary()
        results: list[ValidationResult] = []
        for path in paths:
            done = subprocess.run(
                [binary, "validate", str(path)], capture_output=True, text=True, check=False
            )
            results.append(
                ValidationResult(
                    path=path,
                    ok=done.returncode == 0,
                    detail="" if done.returncode == 0 else done.stdout + done.stderr,
                )
            )
        return results

    def run(
        self,
        path: Path,
        *,
        inputs: Mapping[str, str],
        dashboard: bool,
        background: bool = False,
        workspace_instructions: bool = True,
        working_dir: Path | None = None,
        log_file: str | None = None,
        verbatim: Collection[str] = (),
    ) -> int:
        """Run a compiled workflow, serving the dashboard by default.

        A gate is only answerable from elsewhere while the dashboard port is up.

        ``working_dir`` is where the agents read and write, and where
        ``--workspace-instructions`` starts walking. The workflow path is made
        absolute, since it is rarely inside the project being worked on.

        Detaching without a dashboard is refused: ``--web-bg`` is what detaches.
        """
        if background and not dashboard:
            raise IctusError(
                "conductor detaches with --web-bg, which serves the dashboard: a "
                "background run without one cannot be reached, and there is no flag "
                "that does it. Ask for one or the other."
            )
        command = launch_command(
            self._binary(),
            path,
            inputs=inputs,
            dashboard=dashboard,
            background=background,
            workspace_instructions=workspace_instructions,
            log_file=log_file,
            verbatim=verbatim,
        )
        return subprocess.run(command, check=False, cwd=working_dir).returncode

    def plan(self, path: Path, *, working_dir: Path | None = None) -> int:
        """Print the engine's execution plan for a compiled workflow, running nothing.

        The plan is built from the workflow file alone (``cli/run.py``,
        ``build_dry_run_plan``): inputs are not substituted and nothing is spent.
        """
        command = [self._binary(), "run", str(path.resolve()), "--dry-run"]
        return subprocess.run(command, check=False, cwd=working_dir).returncode

    @staticmethod
    def _binary() -> str:
        return binary()


conductor = ConductorBackend()
