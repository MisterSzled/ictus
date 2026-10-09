"""What can be started, read from the manifests the compiler wrote.

A pipeline's ``listen_on`` compiles to a JSON manifest beside its workflow,
which is the whole contract: a listener runs the built artifact, not the
source, so it needs no Python, no config and no compiler.

Nothing here knows which service asked.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.interfaces.conductor.emit.manifest import SUFFIX, VERSION
from ictus.runspec.inputs import BUILD_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_PREFIX", "Need", "Trigger", "triggers_in"]

#: What somebody types to start one, when no manifest says otherwise.
DEFAULT_PREFIX = "Start test run:"


@dataclass(frozen=True, slots=True)
class Need:
    """Something the environment has to supply, and why it is wanted."""

    name: str
    purpose: str = ""


@dataclass(frozen=True, slots=True)
class Trigger:
    """What starts a run, and what to start.

    ``workflow`` is the compiled YAML, not a pipeline folder.
    """

    workflow: Path
    prefix: str = DEFAULT_PREFIX
    question_input: str = "question"
    thread_input: str = ""
    """Which input the conversation arrives in, and ``""`` when the pipeline
    declared none — a run that reports nowhere has nothing to answer under.

    Never ``thread``: that name collides with what an announcement publishes,
    so no pipeline can declare an input called it."""

    commands: tuple[Need, ...] = ()
    env: tuple[Need, ...] = ()
    """What ``ictus preflight`` would have checked, carried here so a listener
    can refuse before launch rather than failing a step mid-run."""

    pipeline: str = ""
    description: str = ""
    workspace_instructions: bool = True
    """Whether to hand the run what its working directory says about itself.

    Off for a pipeline whose work is not about that directory."""

    @property
    def folder(self) -> Path:
        """Where a run started by this works: the pipeline's own folder.

        Read from where the workflow sits, so it moves with a deployment rather
        than following whoever started the listener — a run's relative paths
        landed in the listener's working directory, one level above anything
        that was deployed. The folder holding ``build/``, or the workflow's own
        directory when it was built somewhere else.
        """
        built = self.workflow.resolve().parent
        return built.parent if built.name == BUILD_DIR else built

    @property
    def pattern(self) -> re.Pattern[str]:
        """The prefix, matched at the start and case-insensitively.

        Loose on purpose: somebody typing into a channel is not writing a
        command line. Every run of whitespace matches any other, and ``*``,
        ``_``, ``~`` and backticks are skipped wherever a space would be —
        chat services send markup, not what the person saw.
        """
        skip = r"[\s*_~`]"
        body = f"{skip}+".join(re.escape(word) for word in self.prefix.split())
        return re.compile(rf"^{skip}*{body}{skip}*(?P<question>.+)", re.I | re.S)

    def missing(self) -> list[str]:
        """What this environment cannot supply, in the manifest's own words."""
        gaps = [
            f"{need.name} is not on PATH" + (f" — {need.purpose}" if need.purpose else "")
            for need in self.commands
            if shutil.which(need.name) is None
        ]
        gaps += [
            f"${need.name} is not set" + (f" — {need.purpose}" if need.purpose else "")
            for need in self.env
            if not os.environ.get(need.name)
        ]
        return gaps


# --- reading what the compiler wrote -----------------------------------------


def triggers_in(where: Path) -> list[Trigger]:
    """Every trigger declared by a manifest at or under ``where``.

    A directory, because one listener has to serve many: a chat service
    delivers an event to exactly one of an app's open connections.
    """
    if where.is_dir():
        found = sorted(where.rglob(f"*{SUFFIX}"))
    elif where.name.endswith(SUFFIX):
        found = [where]
    else:
        found = []
    return [trigger for path in found for trigger in _read(path)]


def _read(path: Path) -> Iterator[Trigger]:
    """The triggers in one manifest. Skips what it cannot read, saying so."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring %s: %s", path, exc)
        return
    if not isinstance(document, dict):
        logger.warning("ignoring %s: not a manifest", path)
        return
    version = document.get("manifest")
    if version != VERSION:
        logger.warning(
            "ignoring %s: manifest version %r, this reads %r — re-emit it", path, version, VERSION
        )
        return
    workflow = path.parent / str(document.get("workflow", ""))
    if not workflow.is_file():
        logger.warning("ignoring %s: its workflow %s is not beside it", path, workflow.name)
        return
    requires = document.get("requires")
    requires = requires if isinstance(requires, dict) else {}
    listeners = document.get("listeners")
    for one in listeners if isinstance(listeners, list) else []:
        if not isinstance(one, dict):
            continue
        inputs = one.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        yield Trigger(
            workflow=workflow,
            prefix=str(one.get("prefix", DEFAULT_PREFIX)),
            question_input=str(inputs.get("question", "question")),
            # No default. The manifest names this exactly when there is one, so
            # guessing a name here would hand every run an input it never
            # declared, under a name it never chose.
            thread_input=str(inputs.get("thread", "")),
            commands=_needs(requires.get("commands")),
            env=_needs(requires.get("env")),
            pipeline=str(document.get("pipeline", "")),
            description=str(document.get("description", "")),
            workspace_instructions=document.get("workspace_instructions") is not False,
        )


def _needs(raw: object) -> tuple[Need, ...]:
    """A manifest's requirement list, ignoring entries that name nothing."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        Need(name=str(entry["name"]), purpose=str(entry.get("purpose", "")))
        for entry in raw
        if isinstance(entry, dict) and entry.get("name")
    )
