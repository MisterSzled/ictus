"""The running contract: what a pipeline folder is, and how a run is specified.

A pipeline is a folder, not a module::

    needs-council/
      pipeline.py     the composition
      input.md        what to run it on
      build/          emitted YAML, committed so a diff shows what changed

``input.md`` is YAML frontmatter over a Markdown body. Frontmatter holds the
short values; the body is the long one, and which input it feeds is declared
with ``declare_input(..., prose=True)``. Every other frontmatter key must be a
declared input; one that matches nothing is refused.

``repo:`` is the one reserved key. It sets the directory the run works in, and
is also passed as an input where the pipeline declares one of that name. Left
out, the run works in the directory it was invoked from.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ictus.errors import IctusError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ictus.graph.pipeline import Pipeline

__all__ = [
    "BUILD_DIR",
    "REPO_KEY",
    "PipelineFolder",
    "RunSpec",
    "read_input_file",
    "split_frontmatter",
]

PIPELINE_FILE = "pipeline.py"
INPUT_FILE = "input.md"
BUILD_DIR = "build"
CONFIG_FILE = "config.yaml"

#: Sets the directory the run works in; also an input when the pipeline wants it.
REPO_KEY = "repo"

# Anchored at the start: a `---` further down is a thematic break in the body.
# The empty first branch matches the `---\n---` a pipeline with no inputs
# writes. An alternation rather than an optional newline, which would also let
# `foo---` close a block.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(|.*?\r?\n)---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

_BLOCK_SCALAR_HINT = (
    "A ':' followed by a space inside an unquoted value is invalid YAML. "
    "Use a block scalar:\n    key: |\n      text with: a colon"
)


class RunSpecError(IctusError):
    """The folder or its input file does not meet the contract."""


@dataclass(frozen=True, slots=True)
class PipelineFolder:
    """A pipeline folder, located but not yet loaded."""

    root: Path

    @property
    def module(self) -> Path:
        """The composition."""
        return self.root / PIPELINE_FILE

    @property
    def input_file(self) -> Path:
        """The default inputs, if the folder has any."""
        return self.root / INPUT_FILE

    @property
    def config_file(self) -> Path:
        """How it runs, as against what it is."""
        return self.root / CONFIG_FILE

    @property
    def build(self) -> Path:
        """Where emitted YAML goes."""
        return self.root / BUILD_DIR

    @property
    def name(self) -> str:
        """The folder's own name, which is how a run refers to it."""
        return self.root.name

    @classmethod
    def at(cls, path: Path) -> PipelineFolder:
        """The folder at ``path``, checked."""
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise RunSpecError(f"{path} is not a directory")
        folder = cls(root)
        if not folder.module.is_file():
            raise RunSpecError(
                f"{path} has no {PIPELINE_FILE}. A pipeline is a folder holding "
                f"{PIPELINE_FILE} and (optionally) {INPUT_FILE}."
            )
        return folder

    @classmethod
    def find(cls, path: Path) -> list[PipelineFolder]:
        """Every pipeline folder at or under ``path``, in name order.

        A folder holding ``pipeline.py`` is one; anything else is a container.
        """
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise RunSpecError(f"{path} is not a directory")
        if (root / PIPELINE_FILE).is_file():
            return [cls(root)]
        found = [
            cls(child)
            for child in sorted(root.iterdir())
            if child.is_dir() and (child / PIPELINE_FILE).is_file()
        ]
        if not found:
            raise RunSpecError(
                f"no pipeline folders under {path}: expected {path}/{PIPELINE_FILE} "
                f"or {path}/<name>/{PIPELINE_FILE}"
            )
        return found


@dataclass(frozen=True, slots=True)
class RunSpec:
    """What to run, on what, where.

    ``working_dir`` is where the agents read and write, resolved here so a run
    records which repository it touched.
    """

    inputs: Mapping[str, str]
    working_dir: Path
    source: Path | None = None


def split_frontmatter(text: str, *, where: str) -> tuple[dict[str, object], str]:
    """Split a leading ``---`` YAML block from the body that follows it."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}, text.strip()
    yaml = YAML(typ="safe")
    try:
        loaded = yaml.load(io.StringIO(match.group(1)))
    except YAMLError as exc:
        raise RunSpecError(
            f"{where}: frontmatter is not valid YAML: {exc}\n{_BLOCK_SCALAR_HINT}"
        ) from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RunSpecError(f"{where}: frontmatter must be a mapping, got {type(loaded).__name__}")
    return dict(loaded), text[match.end() :].strip()


def read_input_file(path: Path, pipeline: Pipeline, *, cwd: Path) -> RunSpec:
    """Read an ``input.md`` into the values a run needs.

    Every key must be a declared input, ``repo`` excepted. A key matching
    nothing is refused rather than ignored.
    """
    where = str(path)
    front, body = split_frontmatter(path.read_text(encoding="utf-8"), where=where)

    declared = {param.name: param for param in pipeline.workflow_inputs}
    prose = [param for param in pipeline.workflow_inputs if param.prose]
    if len(prose) > 1:
        names = ", ".join(sorted(p.name for p in prose))
        raise RunSpecError(
            f"pipeline {pipeline.pipeline_id!r} marks more than one input as prose ({names}); "
            "the body of an input file can only feed one of them"
        )

    values: dict[str, str] = {}
    for key, raw in front.items():
        if key == REPO_KEY and key not in declared:
            continue
        if key not in declared:
            known = ", ".join(sorted(declared)) or "(none)"
            raise RunSpecError(
                f"{where}: {key!r} is not an input of pipeline {pipeline.pipeline_id!r}; "
                f"declared inputs are {known}"
            )
        values[key] = _as_text(raw)

    if body:
        if not prose:
            raise RunSpecError(
                f"{where}: has body text, but pipeline {pipeline.pipeline_id!r} declares no "
                "input to receive it. Mark one with declare_input(..., prose=True), or move "
                "the text into a frontmatter key."
            )
        values[prose[0].name] = body

    missing = sorted(
        name for name, param in declared.items() if param.required and name not in values
    )
    if missing:
        raise RunSpecError(
            f"{where}: pipeline {pipeline.pipeline_id!r} requires {missing}, which the input "
            "file does not supply"
        )

    target = front.get(REPO_KEY)
    working = cwd if target is None else _resolve_repo(str(target), beside=path)
    if target is not None:
        if not working.is_dir():
            raise RunSpecError(f"{where}: repo {target!r} is not a directory ({working})")
        if REPO_KEY in declared:
            values[REPO_KEY] = str(working)
    return RunSpec(inputs=values, working_dir=working, source=path)


def _resolve_repo(value: str, *, beside: Path) -> Path:
    """Where ``repo:`` points.

    A relative path resolves against the input file's own folder, not the
    process's cwd. Absolute and ``~`` paths are taken as given.
    """
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = beside.parent / candidate
    return candidate.resolve()


def _as_text(value: object) -> str:
    """Every workflow input crosses the wire as text, whatever YAML made of it.

    Converted once, here, so a bool does not arrive as Python's ``True``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)
