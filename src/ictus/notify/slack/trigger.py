"""Starting a run because somebody said so in a channel.

The last link. A message matching a prefix starts a pipeline, the message's own
timestamp becomes the conversation the run reports into, and whatever followed
the prefix becomes its input — so the answer arrives under the question rather
than beside it.

What a trigger *is* comes from a manifest, compiled from the pipeline's own
``listen_on`` and written beside its workflow. That matters more than it looks:
a listener runs the built artifact, not the source, so it needs neither the
pipeline's Python, nor its config, nor the compiler that produced them. The
contract is a JSON file and ``conductor run <workflow> -i name=value``, small
enough to implement again elsewhere if ictus is not what gets deployed.

Two guards that are not optional:

* **The bot's own messages are ignored.** It reports into the channel it
  watches, so without this its first announcement starts a second run, which
  announces, which starts a third.
* **A message is acted on once.** Slack redelivers what it believes was not
  acknowledged, and a redelivery is indistinguishable from somebody saying the
  same thing twice.

Spawning is a subprocess, not an import: a run outlives the listener that
started it, and Conductor's ``--web-bg`` already knows how to detach one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.interfaces.conductor import binary, launch_command, launch_env
from ictus.interfaces.conductor.manifest import SUFFIX, VERSION
from ictus.interfaces.conductor.runs import live_runs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PREFIX",
    "Asked",
    "Need",
    "Started",
    "Trigger",
    "asked",
    "start",
    "triggers_in",
]

#: What somebody types to start one, when no manifest says otherwise.
DEFAULT_PREFIX = "Start test run:"

#: How long to wait for Conductor to detach before giving up on it.
LAUNCH_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True, slots=True)
class Asked:
    """Somebody asked for a run, and what they asked about."""

    question: str
    thread: str
    """The message's own timestamp. Replies to it open its thread."""

    channel: str
    who: str
    trigger: Trigger | None = None
    """Which pipeline was asked for. One listener serves many, so an ask that
    did not say would have to be matched against them all a second time."""


@dataclass(frozen=True, slots=True)
class Started:
    """What became of a request to start a run."""

    why: str = ""
    """Empty when it started. Otherwise what to tell the person who asked."""

    dashboard: str = ""
    """Where to watch it, from the run's own record."""

    run_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.why


@dataclass(frozen=True, slots=True)
class Need:
    """Something the environment has to supply, and why it is wanted."""

    name: str
    purpose: str = ""


@dataclass(frozen=True, slots=True)
class Trigger:
    """What starts a run, and what to start.

    ``workflow`` is the compiled YAML, not a pipeline folder. A listener
    pointed at source would have to load the author's module, read its config
    and re-run the compiler before it could start anything — which puts the
    whole toolchain, and the author's repository, on every machine that
    listens.
    """

    workflow: Path
    prefix: str = DEFAULT_PREFIX
    question_input: str = "question"
    thread_input: str = "reply_to"
    """Not ``thread``: that name collides with what an announcement publishes,
    so no pipeline can declare an input called it."""

    commands: tuple[Need, ...] = ()
    env: tuple[Need, ...] = ()
    """What ``ictus preflight`` would have checked. Carried here because
    preflight is a command and not an artifact: without it a missing credential
    stops being a refusal before launch and becomes a script node failing after
    a gate, having already paid for the step in front of it."""

    pipeline: str = ""
    description: str = ""
    workspace_instructions: bool = True
    """Whether to hand the run what its working directory says about itself.

    Off for a pipeline whose work is not about that directory: a tracker ticket
    does not want a contributor guide prepended to every prompt, and on a
    reasoning step that is both a cost and a distraction."""

    @property
    def pattern(self) -> re.Pattern[str]:
        """The prefix, matched at the start and case-insensitively.

        Loose on purpose: somebody typing this into a channel is not writing a
        command line, and a trigger that fails silently on capitalisation reads
        as a broken bot rather than a near miss.

        Every run of whitespace matches any other, in the prefix as much as in
        the message. That covers the person who double-spaces, and the one who
        copied a long prefix out of a wrapped terminal and carried the line
        break into it — which escapes to a literal newline that no single-line
        message can ever match, while the listener starts cleanly and reports
        itself as listening.

        Slack's own emphasis is whitespace too. A message typed in bold arrives
        as ``*New DB ticket raised:*``, and the leading marker alone is enough
        to miss a prefix anchored at the start — so ``*``, ``_``, ``~`` and
        backticks are skipped wherever a space would be allowed. The text a
        person sees is not the text an app receives, and the difference is
        invisible from the channel.
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

    A directory is searched rather than each pipeline being named, because one
    listener serving many is not a convenience: Slack delivers an event to
    exactly one of an app's open sockets, so a process per pipeline would leave
    each seeing a share of the messages and none seeing all of them.
    """
    if where.is_dir():
        found = sorted(where.rglob(f"*{SUFFIX}"))
    elif where.name.endswith(SUFFIX):
        found = [where]
    else:
        found = []
    return [trigger for path in found for trigger in _read(path)]


def _read(path: Path) -> Iterator[Trigger]:
    """The triggers in one manifest. Skips what it cannot read, saying so.

    A listener serves many pipelines, so one unreadable manifest is a reason to
    name that pipeline and go on serving the rest — not to refuse to start.
    """
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
            thread_input=str(inputs.get("thread", "reply_to")),
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


# --- recognising the ask ------------------------------------------------------


def asked(envelope: dict[str, object], trigger: Trigger) -> Iterator[Asked]:
    """The request in one envelope, if it holds one."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return
    # Anything the app itself said, and anything that is not somebody typing:
    # edits, deletions, joins, and the thread-broadcast copies of all three.
    if event.get("bot_id") or event.get("subtype"):
        return
    text = event.get("text")
    if not isinstance(text, str):
        return
    found = trigger.pattern.match(text)
    if found is None:
        return
    # The message's own ts, never its thread_ts: a request made inside somebody
    # else's thread is answered in that thread, not alongside it.
    yield Asked(
        question=found.group("question").strip(),
        thread=str(event.get("ts", "")),
        channel=str(event.get("channel", "")),
        who=str(event.get("user", "")),
        trigger=trigger,
    )


# --- launching ----------------------------------------------------------------


def start(request: Asked, trigger: Trigger) -> Started:
    """Launch a run for ``request``, and say where to watch it.

    Never raises. This is driven by somebody typing in a channel, and every way
    it can fail is something to tell them rather than a traceback in a log.
    """
    # Before the subprocess, not after: this is the check that used to come
    # from `ictus preflight`, and its whole worth is being free.
    gaps = trigger.missing()
    if gaps:
        return Started("; ".join(gaps))
    inputs = {trigger.question_input: request.question, trigger.thread_input: request.thread}
    try:
        command = launch_command(
            binary(),
            trigger.workflow,
            inputs=inputs,
            dashboard=True,
            background=True,
            workspace_instructions=trigger.workspace_instructions,
        )
    except FileNotFoundError as exc:
        return Started(str(exc))
    # Which runs were already going. Conductor prints the dashboard only to a
    # terminal, so a subprocess sees nothing of it — the run's own record is
    # where the port actually lives, and the new id is whatever was not there a
    # moment ago.
    # What the pipeline announced, and nothing else. A run is its own process,
    # which is the only place an environment can be cut: a stage is a file, and
    # the provider hands a model session a copy of whatever the run inherited.
    # This listener's own app-level token is declared by no pipeline, so no run
    # it starts can open a socket as the app that started it.
    passing = launch_env(need.name for need in trigger.env)
    held_back = len(os.environ) - len(passing)
    if held_back > 0:
        logger.debug(
            "%s: passing %d variables, holding back %d", trigger.pipeline, len(passing), held_back
        )
    before = {run.run_id for run in live_runs()}
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=LAUNCH_TIMEOUT_SECONDS,
            env=passing,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Started("the run did not finish starting in time")
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return Started(said[-1] if said else "it refused to start")
    return _launched(before)


def _launched(before: set[str]) -> Started:
    """The run that was not there before, and where to watch it.

    Returns a bare success if it cannot be told apart — two launches at once, or
    a record not yet written. Reporting no address is a smaller failure than
    reporting somebody else's.
    """
    fresh = [run for run in live_runs() if run.run_id not in before]
    if len(fresh) != 1:
        return Started()
    return Started(dashboard=fresh[0].dashboard, run_id=fresh[0].run_id)
