"""Reading prompt text that lives beside the code instead of inside it.

Prose that reaches a model is data, not source. Inlined, it turns a module into
a wall of text with a few lines of composition buried in it, a diff into an
unreadable reflow, and a prompt into something only a Python programmer can
edit. So it lives in its own file, and this reads it.

The layout is fixed, because a convention a test cannot check is a suggestion.
**A module with prompt text is a folder.** The code is its ``__init__.py`` —
Python's ``index`` — and the text sits beside it, named for what it is::

    stdlib/llm/voice/__init__.py     _STANCE = prompt(__name__, "stance")
    stdlib/llm/voice/stance.md       the prose

The import path does not change: ``ictus.stdlib.llm.voice`` is the folder now,
and ``from ictus.stdlib.llm.voice import voice`` reads the same as before. The
folder is what makes a module and its text one thing to move, rename or delete,
and it is why the files need no prefix — the folder already says ``voice``, so
repeating it in ``voice.stance.md`` only made the name longer.

A module with no prompts stays a plain ``.py``. The folder appears exactly when
there is something to keep with it.

**Refs stay in code.** A prompt that interleaves typed references is composed
the way it always was — ``tpl(prompt(__name__, "charge"), node.ref("report"))``
— because the alternative is placeholders in the text, resolved by name, which
is precisely the string-matching that ``Ref`` exists to replace. The prose moves
out; the wiring does not.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

__all__ = ["SUFFIX", "PromptMissingError", "filename_for", "prompt"]

SUFFIX = ".md"


class PromptMissingError(LookupError):
    """A module asked for prompt text that is not in its folder."""


def filename_for(name: str) -> str:
    """The file ``prompt(module, name)`` reads, as a bare filename.

    Split out so the lint can compute the same answer without importing
    anything: a rule that had to run the code it checks would not survive the
    first module that fails to import.
    """
    return f"{name}{SUFFIX}"


@cache
def prompt(module: str, name: str) -> str:
    """The text in ``<name>.md``, in ``module``'s own folder.

    ``module`` is the caller's ``__name__``. Because a module with prompts is a
    package, that name *is* the folder — there is no path arithmetic at the call
    site and nothing to get wrong when the folder moves.

    Read through ``importlib.resources`` rather than ``open(__file__/..)``, so it
    holds in a zip install and anywhere else the package is not a directory on
    disk. Cached: read once per process, placed into however many nodes ask.

    Stripped. An editor adding a final newline should not change an emitted
    workflow, and ``test_committed_yaml_matches_a_fresh_emit`` would otherwise
    fail on somebody else's editor settings.
    """
    try:
        return (files(module) / filename_for(name)).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, ModuleNotFoundError, TypeError) as exc:
        raise PromptMissingError(
            f"{module} asked for prompt {name!r}. A module with prompt text is a "
            f"folder: put the prose in {module.replace('.', '/')}/{filename_for(name)} "
            f"beside that module's __init__.py"
        ) from exc
