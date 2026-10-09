"""Environment checks that hold whatever executes the graph.

A missing command does not surface as a crash: the step runs, the lookup
fails, and the model reports the thing it was checking as absent.

An MCP server is the engine's business and is checked in
``interfaces.conductor.mcp``.
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
    gone is still an executable file.
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

    Offline only: probing would mean posting something somewhere people read.
    """
    issues: list[PreflightIssue] = []
    for service in pipeline.all_integrations():
        if shutil.which(service.command) is None:
            # A step that cannot start is the one way a report fails the run:
            # the engine raises before the program's own guard can run.
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
            # Not blocking: the watcher is a real way to deliver these.
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

    Offline only. A source's own commands are folded into the pipeline's
    executables when it is declared, so ``executable_issues`` covers them.
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
