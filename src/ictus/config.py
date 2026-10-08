"""``config.yaml`` — how a pipeline runs, as against what it is.

Three files, three questions, and keeping them apart is the point::

    pipeline.py     what the graph is          (composition)
    config.yaml     how it runs                (policy)
    input.md        what to run it on          (this run's values)

Policy is what you change without touching the graph: which provider answers the
model calls, what it may spend, whether a person confirms before anything starts.
Putting it in Python meant editing a composition to move a pipeline between
providers, and meant the answer was invisible unless you read the code.

``provider`` is required and has no default. Conductor's own default is
``copilot``, and a pipeline that silently took it is how four emitted workflows
once ran on the wrong provider for a whole session — the sort of thing that is
obvious in a config file and invisible in an omission.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ictus.baseline import AGENT_BASELINE, NO_BASELINE
from ictus.errors import IctusError

if TYPE_CHECKING:
    from ictus.graph.pipeline import NativeTools, Pipeline

__all__ = ["CONFIG_FILE", "MINIMAL", "ConfigError", "PipelineConfig", "read_config"]

CONFIG_FILE = "config.yaml"

MINIMAL = "provider: claude-agent-sdk\n"

_BUDGET_MODES = frozenset({"audit", "enforce"})

_KNOWN = frozenset(
    {
        "provider",
        "default_model",
        "start_gate",
        "budget_usd",
        "budget_mode",
        "max_iterations",
        "timeout_seconds",
        "dashboard",
        "instructions",
        "workspace_instructions",
        "system_prompt",
        "native_tools",
    }
)


class ConfigError(IctusError):
    """``config.yaml`` is missing, malformed, or says something impossible."""


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """A pipeline's run policy."""

    provider: str
    default_model: str | None = None
    start_gate: bool = True
    """Whether a person confirms before anything runs. See ``ictus.gate``."""

    budget_usd: float | None = None
    budget_mode: str | None = None
    """Whether the budget stops a run or only records it.

    ``None`` means this file did not say, and the pipeline's own value stands.
    Defaulting it to ``"audit"`` here instead made every pipeline that asked for
    ``enforce`` in composition emit ``audit``, silently — the one policy field
    that overwrote rather than conflicted, so nothing ever reported it."""
    max_iterations: int | None = None
    timeout_seconds: int | None = None
    """A wall-clock ceiling on the whole run, in seconds.

    Policy rather than composition for the same reason ``budget_usd`` is: how
    long this run may take is a property of where it is being run, not of what
    the graph does. Nothing else bounds elapsed time — a budget bounds spend and
    ``max_iterations`` bounds step count, and a run can sit for hours moving
    neither. Distinct from a step's own ``timeout_seconds``, which cancels one
    model call and leaves the workflow to carry on."""

    dashboard: bool = True
    """Whether ``ictus run`` serves the dashboard. A gated run needs one."""

    native_tools: NativeTools = "none"
    """Whether a step that names no tools may read files, run commands, or fetch.

    Off, which is the engine's own default and was not always: a step with no
    ``tools`` used to be handed the filesystem, a shell and the web without any
    pipeline saying so. Closing that was right and it is not free — a step that
    was reading the repository now answers from memory and sounds no different
    doing it, because nothing fails. Turn it on where a step is meant to go and
    look, and the diff shows which pipelines can touch a disk.
    """

    system_prompt: str | None = AGENT_BASELINE
    """What every model call is told about how to work, unless it sets its own.

    Defaulted rather than omitted because omitting it does not mean "the
    provider's default" — Conductor forwards ``None`` and the SDK turns that into
    an *empty* system prompt. A step then has the model and the tools and none of
    the discipline, which is how a council once agreed on a report whose claims
    nobody had checked.

    ``none`` in the file switches it off; a path or literal text replaces it."""

    workspace_instructions: bool = True
    """Whether the run picks up the target project's own instruction files.

    A Claude Code session opened in a repository reads its ``CLAUDE.md`` before
    it does anything. A Conductor step does not: the provider pins
    ``setting_sources=[]``, so no ``CLAUDE.md``, no settings, no ambient skills
    reach it. The engine's own opt-in for this is the ``--workspace-instructions``
    flag, which walks up from the run's working directory to the git root and
    prepends what it finds — ``AGENTS.md``, ``.github/copilot-instructions.md``,
    ``CLAUDE.md``, ``.github/instructions/*.instructions.md``.

    On by default, because the alternative is a step reasoning about a project
    whose stated conventions it has never seen, and because the thing being
    picked up is committed to the repository being worked on rather than
    ambient to the machine. Set false for a run that must behave identically
    against any checkout.

    This is the *project's* instructions, not the operator's: a personal
    ``~/.claude/CLAUDE.md`` is a user setting and stays out either way.
    """

    instructions: tuple[str, ...] = ()
    """Text prepended to every step's prompt: what the project is, and how to
    work on it.

    Conductor runs its agents with no settings sources — no CLAUDE.md, no
    ambient skills, no hooks — so a step arrives knowing nothing about the
    project beyond its own prompt. That is most of the difference between a
    step's output and what the same model produces in a session that has been
    reading the repository for an hour. Paths are read relative to the pipeline
    folder; anything that is not a readable path is used as literal text."""

    def apply(self, pipeline: Pipeline, *, where: str) -> None:
        """Put this policy on ``pipeline`` and everything nested inside it.

        Descendants too, because a stage is its own workflow file and a lint
        reading it in isolation would otherwise see no provider and assume the
        engine's default — which is how a check meant to catch "this provider
        cannot do that" reported the wrong provider.

        A value set in both places and set *differently* is refused rather than
        silently resolved: two sources of truth that disagree is exactly the
        state where whichever one you read is the wrong one.
        """
        for child in pipeline.children.values():
            self.apply(child, where=where)
        for field, value in (
            ("provider", self.provider),
            ("default_model", self.default_model),
            ("budget_usd", self.budget_usd),
            ("max_iterations", self.max_iterations),
            ("timeout_seconds", self.timeout_seconds),
        ):
            if value is None:
                continue
            current = getattr(pipeline, field)
            if current is not None and current != value:
                raise ConfigError(
                    f"{where}: {field} is {value!r} here but {current!r} in the pipeline. "
                    "Policy belongs in config.yaml; take it out of the composition."
                )
            setattr(pipeline, field, value)
        # Apart from the loop, because this is the one policy field whose
        # composition-side default is a value rather than `None`: a pipeline
        # reading `audit` has not chosen it, so taking that for disagreement
        # would refuse every config that set a mode. Overwriting unconditionally
        # is what this used to do, and it meant every pipeline asking for
        # `enforce` emitted `audit` with nothing said.
        if self.budget_mode is not None:
            if pipeline.budget_mode not in ("audit", self.budget_mode):
                raise ConfigError(
                    f"{where}: budget_mode is {self.budget_mode!r} here but "
                    f"{pipeline.budget_mode!r} in the pipeline. Policy belongs in "
                    "config.yaml; take it out of the composition."
                )
            pipeline.budget_mode = self.budget_mode  # type: ignore[assignment]
        pipeline.workspace_instructions = self.workspace_instructions
        if self.instructions and not pipeline.instructions:
            pipeline.instructions = list(self.instructions)
        if pipeline.system_prompt is None:
            pipeline.system_prompt = self.system_prompt
        if pipeline.native_tools is None:
            pipeline.native_tools = self.native_tools


def _native_tools(loaded: dict[str, object], where: str) -> NativeTools:
    """``native_tools``: nothing, everything, or exactly what is listed.

    The list is the one worth reaching for. ``claude_code`` grants a shell
    along with the reading, and a step with a shell is bounded by its own
    judgement rather than by anything here — one started a database container
    to try its own SQL against, which was a reasonable thing to do and not a
    thing anybody had agreed to. ``[Read, Grep, Glob]`` is a step that may look
    at a repository and may not run it.
    """
    value = loaded.get("native_tools", "none")
    if value == "claude_code":
        return "claude_code"
    if isinstance(value, list):
        named = [str(item).strip() for item in value]
        if not named or not all(named):
            raise ConfigError(
                f"{where}: native_tools is an empty list, or has an entry with no "
                "name. Write 'none' to grant nothing — an empty list reads as "
                "something half-written rather than a decision."
            )
        return tuple(named)
    if value != "none":
        raise ConfigError(
            f"{where}: native_tools is {value!r}; it is 'none', 'claude_code', or a "
            "list of tool ids such as [Read, Grep, Glob]. 'claude_code' lets a step "
            "that names no tools read files, run commands and fetch, which is what "
            "the bare `claude` CLI gives you."
        )
    return "none"


def read_config(path: Path) -> PipelineConfig:
    """Read a pipeline folder's ``config.yaml``."""
    where = str(path)
    if not path.is_file():
        raise ConfigError(
            f"{path} does not exist. Every pipeline folder needs one; the minimal "
            f"version is a single line:\n\n    {MINIMAL.strip()}\n\n"
            "Or run `ictus init` on the folder."
        )
    yaml = YAML(typ="safe")
    try:
        loaded = yaml.load(io.StringIO(path.read_text(encoding="utf-8")))
    except YAMLError as exc:
        raise ConfigError(f"{where}: not valid YAML: {exc}") from exc
    if loaded is None:
        raise ConfigError(f"{where}: is empty; it needs at least `{MINIMAL.strip()}`")
    if not isinstance(loaded, dict):
        raise ConfigError(f"{where}: must be a mapping, got {type(loaded).__name__}")

    unknown = sorted(set(loaded) - _KNOWN)
    if unknown:
        raise ConfigError(
            f"{where}: {unknown} are not settings; known are {sorted(_KNOWN)}. "
            "A key that does nothing quietly is worse than one that fails."
        )
    provider = loaded.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ConfigError(
            f"{where}: needs a `provider`. There is no default on purpose — Conductor's "
            "own is copilot, and inheriting it silently is how a pipeline ends up running "
            "somewhere nobody chose."
        )
    mode = loaded.get("budget_mode")
    if mode is not None and mode not in _BUDGET_MODES:
        raise ConfigError(f"{where}: budget_mode must be one of {sorted(_BUDGET_MODES)}")
    timeout_seconds = _optional_int(loaded, "timeout_seconds", where)
    if timeout_seconds is not None and timeout_seconds < 1:
        raise ConfigError(
            f"{where}: timeout_seconds must be at least 1, got {timeout_seconds}. "
            "Conductor's own bound; leave it out for no ceiling rather than "
            "writing one it will refuse."
        )

    return PipelineConfig(
        provider=provider,
        default_model=_optional_str(loaded, "default_model", where),
        start_gate=_flag(loaded, "start_gate", where, default=True),
        budget_usd=_optional_number(loaded, "budget_usd", where),
        budget_mode=mode,
        max_iterations=_optional_int(loaded, "max_iterations", where),
        timeout_seconds=timeout_seconds,
        dashboard=_flag(loaded, "dashboard", where, default=True),
        instructions=_instructions(loaded, where, beside=path.parent),
        workspace_instructions=_flag(loaded, "workspace_instructions", where, default=True),
        system_prompt=_system_prompt(loaded, where, beside=path.parent),
        native_tools=_native_tools(loaded, where),
    )


def _system_prompt(data: dict[str, object], where: str, *, beside: Path) -> str | None:
    """The baseline, a replacement for it, or nothing at all."""
    raw = data.get("system_prompt")
    if raw is None:
        return AGENT_BASELINE
    if not isinstance(raw, str):
        raise ConfigError(f"{where}: system_prompt must be text, a path, or {NO_BASELINE!r}")
    if raw.strip() == NO_BASELINE:
        return None
    if raw.endswith((".md", ".txt")) or raw.startswith(("./", "../", "~")):
        target = Path(raw).expanduser() if raw.startswith("~") else (beside / raw).expanduser()
        if not target.is_file():
            raise ConfigError(f"{where}: system_prompt names {raw!r}, which is not a file")
        return target.read_text(encoding="utf-8")
    return raw


def _instructions(data: dict[str, object], where: str, *, beside: Path) -> tuple[str, ...]:
    """Read the instruction entries, resolving paths against the folder.

    A path is read; anything else is taken literally. A path that looks like one
    and is not there is an error rather than prose — silently prepending
    ``./CONTRIBUTING.md`` to every prompt is the sort of thing nobody notices
    until the output is subtly wrong.
    """
    raw = data.get("instructions")
    if raw is None:
        return ()
    entries = [raw] if isinstance(raw, str) else raw
    if not isinstance(entries, list):
        raise ConfigError(f"{where}: instructions must be text or a list of text")
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ConfigError(f"{where}: every instruction must be text, got {entry!r}")
        looks_like_path = entry.endswith((".md", ".txt")) or entry.startswith(("./", "../", "~"))
        if not looks_like_path:
            out.append(entry)
            continue
        target = (
            Path(entry).expanduser() if entry.startswith("~") else (beside / entry).expanduser()
        )
        if not target.is_file():
            raise ConfigError(
                f"{where}: instructions names {entry!r}, which is not a file ({target})"
            )
        out.append(target.read_text(encoding="utf-8"))
    return tuple(out)


def _flag(data: dict[str, object], key: str, where: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false, got {value!r}")
    return value


def _optional_str(data: dict[str, object], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be text, got {type(value).__name__}")
    return value


def _optional_int(data: dict[str, object], key: str, where: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be a whole number, got {value!r}")
    return value


def _optional_number(data: dict[str, object], key: str, where: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}")
    return float(value)
