"""Where the binary is, what environment it is handed, and the argv that starts a run.

Filed under ``control`` rather than beside the backend class because of the
rule the two sibling packages already state: ``emit`` is "a pure function of a
pipeline — no environment read and no process started", and ``control`` is
"driving a run that is already happening". These call ``shutil.which``, read
``os.environ`` and build the command line. They were the only module-level
functions in the backend's own file and none of them touched a ``Pipeline``.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping
    from pathlib import Path

__all__ = ["BINARY", "TYPED_INPUT_FLAG", "binary", "launch_command", "launch_env"]


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


#: Variables about the machine, which a run cannot work without and which are
#: nobody's pipeline secret.
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
#: By prefix rather than by list, so a provider added upstream still works.
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

    The run's process is the only place an environment can be cut; a step with
    a shell sees everything the run was given. ``declared`` is the pipeline's
    integrations, datasources and MCP servers. Anything outside that and
    :data:`MACHINE_ENV` is absent rather than empty.
    """
    present = os.environ if source is None else source
    wanted = set(declared) | MACHINE_ENV
    return {
        name: value
        for name, value in present.items()
        if name in wanted or name.startswith(MACHINE_PREFIXES)
    }


TYPED_INPUT_FLAG = "--input-json"


def launch_command(
    executable: str,
    path: Path,
    *,
    inputs: Mapping[str, str],
    dashboard: bool,
    background: bool = False,
    workspace_instructions: bool = True,
    log_file: str | None = None,
    verbatim: Collection[str] = (),
) -> list[str]:
    """The argv that runs one compiled workflow.

    One place, so the CLI and the listener spell the flags the same way.

    ``verbatim`` names the inputs that must arrive as the text they were given,
    whatever they look like. Everything else is coerced by the engine, which is
    how an ``int`` input gets an int.
    """
    command = [executable, "run", str(path.resolve())]
    for name, value in inputs.items():
        if name in verbatim:
            command += [TYPED_INPUT_FLAG, f"{name}={json.dumps(value)}"]
        else:
            command += ["-i", f"{name}={value}"]
    if log_file is not None:
        # Verbatim: `auto` is Conductor's spelling for a generated temp path.
        command += ["--log-file", log_file]
    if workspace_instructions:
        # The provider runs every step with `setting_sources=[]`. This flag
        # walks from the working directory to the git root and prepends
        # AGENTS.md, CLAUDE.md, .github/copilot-instructions.md and
        # .github/instructions/*.instructions.md to every prompt.
        command.append("--workspace-instructions")
    if background:
        command.append("--web-bg")
    elif dashboard:
        command.append("--web")
    return command
