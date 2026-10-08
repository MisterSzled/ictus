"""The Conductor backend.

Everything Conductor-shaped lives under this package: its field names, its
template dialect, its iteration accounting, its terminal marker, its CLI. If a
Conductor spelling appears anywhere above ``ictus.interfaces``, that is a defect
with a name.

    workflow.py   the ``workflow:`` block and the defaults worth stating
    agents.py     one node to one ``agents:`` entry
    serialize.py  YAML text, without altering any value
    lints.py      rules that are true because of how Conductor runs
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from ictus.errors import IctusError
from ictus.graph.node import NODE_KINDS
from ictus.interfaces import Capabilities, Document, PreflightIssue, ValidationResult
from ictus.interfaces.conductor import manifest
from ictus.interfaces.conductor.agents import agent_entry
from ictus.interfaces.conductor.lints import conductor_problems
from ictus.interfaces.conductor.mapping import for_each_block
from ictus.interfaces.conductor.mcp import preflight_issues
from ictus.interfaces.conductor.parallel import parallel_block
from ictus.interfaces.conductor.serialize import dump_yaml
from ictus.interfaces.conductor.signals import REPORTABLE
from ictus.interfaces.conductor.templates import output_block
from ictus.interfaces.conductor.workflow import NOTHING_INHERITED, Inherited, workflow_block
from ictus.interfaces.environment import (
    datasource_issues,
    executable_issues,
    integration_issues,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from ictus.graph.pipeline import Pipeline
    from ictus.graph.values import YamlDict, YamlValue

__all__ = ["ConductorBackend", "binary", "conductor", "launch_command", "launch_env"]

BINARY = "conductor"


def binary() -> str:
    """The Conductor executable, or a ``FileNotFoundError`` naming what is missing."""
    found = shutil.which(BINARY)
    if found is None:
        raise FileNotFoundError(
            f"{BINARY!r} is not on PATH; the Conductor backend cannot check or run "
            "what it compiles without it"
        )
    return found


#: Variables about the *machine*, which a run cannot work without and which are
#: nobody's pipeline secret: where to find commands, where the home directory
#: is, where temporary files go, how to decode bytes, which certificates to
#: trust.
MACHINE_ENV: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
    }
)

#: Prefixes for the engine's own settings and for model-provider credentials.
#: Matched by prefix rather than listed, because a provider added upstream
#: brings its own variable names and a run that cannot authenticate is a
#: confusing failure, not a safe one. These are machine credentials — the right
#: to call a model — and not the pipeline secrets this filtering is about.
MACHINE_PREFIXES: tuple[str, ...] = (
    "CONDUCTOR_",
    "CLAUDE_",
    "ANTHROPIC_",
    "OPENAI_",
    "AZURE_",
    "COPILOT_",
    "GITHUB_",
    "ACA_",
    "OTEL_",
)


def launch_env(declared: Iterable[str], source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a run should receive: what it declared, and nothing else.

    A workflow is spawned as its own process, which is the one place an
    environment can actually be cut — a stage is a file, not a process, and the
    provider hands a model session a copy of whatever the run inherited. So a
    step with a shell sees every variable the run was given, and the only way to
    keep a credential away from it is not to give the *run* that credential.

    ``declared`` is what the pipeline announced: its integrations' variables,
    its datasources', its MCP servers'. Everything outside that and the machine
    baseline is dropped, which is what turns the declaration from something a
    reviewer reads into something the run is actually bounded by. The listener's
    own app-level token is the clearest case — no pipeline declares it, so no
    run receives it, and a run cannot open a socket as the app that started it.

    Missing variables are simply absent rather than empty: a program testing
    ``os.environ.get(NAME)`` should see the same nothing it would see on a
    machine where nobody set it.
    """
    present = os.environ if source is None else source
    wanted = set(declared) | MACHINE_ENV
    return {
        name: value
        for name, value in present.items()
        if name in wanted or name.startswith(MACHINE_PREFIXES)
    }


def launch_command(
    executable: str,
    path: Path,
    *,
    inputs: Mapping[str, str],
    dashboard: bool,
    background: bool = False,
    workspace_instructions: bool = True,
    log_file: str | None = None,
) -> list[str]:
    """The argv that runs one compiled workflow.

    Built here rather than at each call site so that everything which starts a
    run — the CLI, and a listener acting on a message — spells the flags the
    same way. The difference between ``--web`` and ``--web-bg`` decides whether
    a gate can be answered from outside the process, which is not a detail to
    get independently right in two places.
    """
    command = [executable, "run", str(path.resolve())]
    for name, value in inputs.items():
        command += ["-i", f"{name}={value}"]
    if log_file is not None:
        # Passed through verbatim: `auto` is Conductor's own spelling for a
        # generated temp path, and anything else is taken as a file path.
        command += ["--log-file", log_file]
    if workspace_instructions:
        # The provider runs every step with `setting_sources=[]` — no
        # CLAUDE.md, no settings, no ambient skills — so a step arrives
        # knowing nothing the project says about itself. This flag is the
        # engine's own opt-in: it walks from the working directory up to the
        # git root and prepends AGENTS.md, .github/copilot-instructions.md,
        # CLAUDE.md and .github/instructions/*.instructions.md to every
        # prompt. Nothing else ictus can emit reaches those files.
        command.append("--workspace-instructions")
    if background:
        command.append("--web-bg")
    elif dashboard:
        command.append("--web")
    return command


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
            # Conductor's `tools:` holds *workflow* tool names, which the
            # claude-agent-sdk provider cannot translate to CLI tool ids — it
            # raises ProviderError on a non-empty list rather than silently
            # granting the wrong ones (providers/claude_agent_sdk.py).
            tool_allowlists=False,
            # `capabilities.session_continuity` is true for this one alone;
            # config/validator.py:2458 rejects a session_key on any other.
            remembering_providers=frozenset({"claude-agent-sdk"}),
            conditional_routes=True,
            cycles=True,
            sub_graphs=True,
            notes="Loops are bounded by a single global step budget, not per cycle.",
        )

    def document(self, pipeline: Pipeline, inherited: Inherited = NOTHING_INHERITED) -> YamlDict:
        """The workflow mapping for one pipeline, before serialization.

        Public because the backend's own tests assert on the structure; the
        ``Backend`` protocol only promises rendered text.
        """
        # A map group's body lives inline under `for_each:`; emitting it here as
        # well would leave a step Conductor schedules once on its own.
        # An unset system prompt is an *empty* one to the engine, not a default
        # one, so what a step inherits has to be decided here and passed down.
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

        The parent comes first; each nested stage follows as its own file,
        because ``type: workflow`` references a sibling rather than inlining a
        graph. A stage placed twice in one parent is two nodes over one file.
        """
        out = [
            Document(f"{pipeline.pipeline_id}.yaml", dump_yaml(self.document(pipeline, inherited)))
        ]
        # Only here, never for a stage: a stage is reached through its caller
        # and has no run of its own for a message to start.
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

        Conductor validates that a provider *can* honour MCP. Whether the server
        is installed, the token is set and the endpoint answers is checked here,
        because nothing else checks it and the failure otherwise lands mid-run.

        Declared executables are checked here too. They are not Conductor's
        concern — a command on PATH means the same thing under any engine — but
        preflight is one question, and asking it in two places would let one of
        them be forgotten.
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
    ) -> int:
        """Run a compiled workflow, serving the dashboard by default.

        A gate is only answerable from elsewhere while the run has a dashboard
        port, and mid-run guidance needs one too.

        ``working_dir`` is the directory the agents read and write in: Conductor
        resolves script paths and the model's own tools against the process's
        cwd, so this is what "run it on that project" means. It is also where
        ``--workspace-instructions`` starts walking, which is why the two belong
        to the same call. The workflow path
        is made absolute first, because it is almost never inside the project
        being worked on.

        Detaching without a dashboard is refused rather than honoured on one
        side: Conductor's ``--web-bg`` is what detaches, so a caller asking for
        both got a served port anyway and no signal that its choice was dropped.
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
        )
        return subprocess.run(command, check=False, cwd=working_dir).returncode

    def plan(self, path: Path, *, working_dir: Path | None = None) -> int:
        """Print the engine's execution plan for a compiled workflow, running nothing.

        Separate from ``run`` rather than a flag on it, because it is not a run:
        ``conductor run --dry-run`` builds its plan from the workflow file alone
        (``cli/run.py``, ``build_dry_run_plan``), so inputs are not substituted,
        no provider is constructed and nothing is spent. Passing the run-shape
        flags would mean accepting a dashboard port and a detach for something
        that prints and exits.
        """
        command = [self._binary(), "run", str(path.resolve()), "--dry-run"]
        return subprocess.run(command, check=False, cwd=working_dir).returncode

    @staticmethod
    def _binary() -> str:
        return binary()


conductor = ConductorBackend()
