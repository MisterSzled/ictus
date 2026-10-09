"""What specifies a run: the folder a pipeline lives in, and what it says.

Named for ``RunSpec``, the type it exists to produce — not for the folder it
happens to be read out of. A package called ``folder`` would name the medium and
say nothing about the concern, and ``from ictus.folder.config import read_config``
reads as a sentence about filesystems rather than about pipelines.

    pipeline.py   the graph
    config.yaml   policy: provider, budget, gates, instructions
    input.md      this run's values, as YAML frontmatter over a prose body
    build/        emitted YAML, committed so a diff shows what runs

``pipeline.py`` changes when the work changes, ``config.yaml`` when the budget
or the provider does, ``input.md`` every run.

Nothing here knows an engine or a service. It reads files, checks what they say,
and hands back values — the *selecting*, never the thing selected. The system
prompt ``config.yaml`` can choose is in ``ictus.stdlib.baseline``, because a
prompt beside a YAML parser reads like a parser setting.
"""

from __future__ import annotations

from ictus.runspec.config import CONFIG_FILE, MINIMAL, NO_BASELINE, PipelineConfig, read_config
from ictus.runspec.inputs import PipelineFolder, read_input_file
from ictus.runspec.scaffold import STARTER_INPUT, STARTER_PIPELINE

__all__ = [
    "CONFIG_FILE",
    "MINIMAL",
    "NO_BASELINE",
    "STARTER_INPUT",
    "STARTER_PIPELINE",
    "PipelineConfig",
    "PipelineFolder",
    "read_config",
    "read_input_file",
]
