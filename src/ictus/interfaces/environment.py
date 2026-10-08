"""Environment checks that hold whatever executes the graph.

An MCP server is Conductor's business — it is declared in Conductor's runtime
block and reached the way Conductor reaches it. A command on ``PATH`` is not:
every engine runs its steps in some process, and a step told to check something
against a tool that is not installed behaves the same way under all of them.

It behaves badly, which is why this exists. A missing tool does not surface as a
crash. The step runs, the lookup fails, and the model reports that the thing it
was checking is absent — a confident wrong answer, indistinguishable in the
output from a considered one, produced at full price. A council once concluded
that four engine features were missing on the strength of one import that had
been run against the wrong interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from ictus.graph.signals import ANNOUNCED_BY_STEPS
from ictus.interfaces import PreflightIssue

if TYPE_CHECKING:
    from ictus.graph.pipeline import Pipeline
    from ictus.graph.requirements import Executable

__all__ = ["datasource_issues", "executable_issues", "integration_issues"]

#: Long enough for a cold `--version`, short enough not to hang a launch.
PROBE_TIMEOUT_SECONDS = 10.0


def executable_issues(pipeline: Pipeline, *, probe: bool) -> list[PreflightIssue]:
    """Every declared command this machine cannot supply."""
    issues: list[PreflightIssue] = []
    for tool in pipeline.all_executables():
        found = shutil.which(tool.name)
        if found is None:
            issues.append(
                PreflightIssue(
                    requirement=f"exe:{tool.name}",
                    problem=f"{tool.name!r} is not on PATH ({tool.purpose})",
                    remedy=tool.setup_hint or f"install {tool.name} and put it on PATH",
                )
            )
            continue
        if probe and tool.probe:
            issues.extend(_probe(tool, found))
    return issues


def _probe(tool: Executable, found: str) -> list[PreflightIssue]:
    """Run the tool's own proof that it answers.

    On PATH is not the same as working: a console script whose interpreter has
    been removed is still a file, and still executable, and still fails.
    """
    argv = [found, *tool.probe]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except OSError as exc:
        return [
            PreflightIssue(
                requirement=f"exe:{tool.name}",
                problem=f"{found} could not be run: {exc}",
                remedy=tool.setup_hint or f"reinstall {tool.name}",
            )
        ]
    except subprocess.TimeoutExpired:
        return [
            PreflightIssue(
                requirement=f"exe:{tool.name}",
                problem=(f"{' '.join(argv)} did not answer within {PROBE_TIMEOUT_SECONDS:.0f}s"),
                remedy=tool.setup_hint or f"check {tool.name} runs by hand",
            )
        ]
    if done.returncode == 0:
        return []
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return [
        PreflightIssue(
            requirement=f"exe:{tool.name}",
            problem=(
                f"{' '.join(argv)} exited {done.returncode}" + (f": {detail[-1]}" if detail else "")
            ),
            remedy=tool.setup_hint or f"reinstall {tool.name}",
        )
    ]


def integration_issues(pipeline: Pipeline) -> list[PreflightIssue]:
    """Every declared integration this machine cannot supply a credential for.

    Offline only, and deliberately: probing would mean posting something to find
    out, and an integration's endpoint is somewhere people read. A preflight that
    announced itself in a channel every time anyone checked a pipeline would be
    turned off, and then the real notification would be ignored with it.
    """
    issues: list[PreflightIssue] = []
    for service in pipeline.all_integrations():
        if shutil.which(service.command) is None:
            # A step that cannot start is the one way a report still fails the
            # run: the engine raises before the program's own guard can run.
            issues.append(
                PreflightIssue(
                    requirement=f"integrate:{service.name}",
                    problem=(
                        f"{service.command!r} is not on PATH, so {service.name!r} could never "
                        "send anything, and the step that tries would fail the run"
                    ),
                    remedy=f"install {service.command} or put it on PATH",
                )
            )
        issues.extend(
            PreflightIssue(
                requirement=f"integrate:{service.name}",
                problem=(
                    f"${var.name} is not set, so {service.name!r} cannot be reached "
                    f"({service.purpose})"
                ),
                remedy=service.setup_hint or f"export {var.name}=... before the run",
            )
            for var in service.required_env
            if not os.environ.get(var.name)
        )
        unseen = sorted(s.value for s in service.reports if s not in ANNOUNCED_BY_STEPS)
        if unseen:
            # Not blocking: the watcher is a real way to deliver these. Said out
            # loud because without it they are configured, pass preflight, and
            # never arrive.
            issues.append(
                PreflightIssue(
                    requirement=f"integrate:{service.name}",
                    problem=(
                        f"{service.name!r} asks to hear about {', '.join(unseen)}, which no "
                        "step can see; they are reported only while `ictus watch` is "
                        "attached to the run"
                    ),
                    remedy="run `ictus watch <folder> --follow` alongside the run",
                    blocking=False,
                )
            )
    return issues


def datasource_issues(pipeline: Pipeline) -> list[PreflightIssue]:
    """Every declared source this machine cannot supply a connection for.

    Offline only, like an integration's. Probing would mean opening a
    connection to a production database to find out whether a pipeline parses,
    and the one thing worse than a preflight nobody runs is one nobody dares
    to.

    The commands a source needs are folded into the pipeline's executables when
    it is declared, so they are checked by ``executable_issues`` and are not
    repeated here.
    """
    return [
        PreflightIssue(
            requirement=f"read:{source.name}",
            problem=(
                f"${var.name} is not set, so {source.name!r} cannot be reached ({source.purpose})"
            ),
            remedy=source.setup_hint or f"export {var.name}=... before the run",
        )
        for source in pipeline.all_datasources()
        for var in source.required_env
        if not os.environ.get(var.name)
    ]
