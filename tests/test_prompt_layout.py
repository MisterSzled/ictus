"""Prose that reaches a model lives in a file, not in a Python string.

The rule, and it is mechanical rather than a matter of taste:

**A string literal in prompt position is a connective, not a sentence.** Over
``MAX_INLINE`` characters it has to come from ``<name>.md`` in the module's own
folder, read through ``ictus.prompting.prompt``.

**A module with prompt text is a folder.** The code is its ``__init__.py``; the
prose sits beside it. Checked both ways: a module asking for a prompt it has no
folder for fails, and an ``.md`` with no ``__init__.py`` beside it fails.

Prompt position is defined by where the value goes, not by what it looks like:
an argument to ``tpl`` or ``as_template``, or a ``prompt=``/``system_prompt=``
keyword. Those are the places a string ends up in front of a model.

Why a limit rather than a ban: a prompt is composed from typed references, and
the glue between them — ``"\\n\\n"``, a section rule, a two-word label — is part
of the composition and belongs with it. What does not belong is a paragraph.
``MAX_INLINE`` is about one line of prose, which separates the two cleanly on
everything in the tree today.

Why not a ban on long literals everywhere: most long strings in ictus are error
messages, and an error message belongs beside the condition that raises it. The
rule is about what a *model* reads.

Refs are deliberately not moved. ``tpl(prompt(__name__, "charge"), node.ref("x"))``
keeps the reference typed and checked where it is written; putting ``{x}`` in
the text and resolving it by name is the string-matching ``Ref`` exists to
replace, so the prose moves out and the wiring stays.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ictus.prompting import SUFFIX, filename_for

SRC = Path(__file__).resolve().parent.parent / "src" / "ictus"

#: About one line of prose. A literal longer than this in front of a model is a
#: paragraph somebody will have to edit through Python quoting.
MAX_INLINE = 60

#: Calls whose string arguments are read by a model.
TEMPLATE_CALLS = ("tpl", "as_template")

#: Keywords whose string value is read by a model. ``GateNode.prompt`` is read
#: by a person, but it goes through the same field name and the same rule does
#: it no harm.
PROMPT_KEYWORDS = ("prompt", "system_prompt", "charge", "intent", "instruction")


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _docstrings(tree: ast.Module) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _in_prompt_position(tree: ast.Module) -> list[tuple[int, str]]:
    """Every string literal a model would read, as (line, text).

    Returned as text rather than as nodes so the narrowing survives: an
    ``ast.Constant.value`` is a union, and every caller wants the ``str``.
    """
    docs = _docstrings(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        values: list[ast.expr] = []
        if called in TEMPLATE_CALLS:
            values += node.args
        values += [kw.value for kw in node.keywords if kw.arg in PROMPT_KEYWORDS]
        found += [
            (v.lineno, v.value)
            for v in values
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and id(v) not in docs
        ]
    return found


def test_there_is_something_to_check() -> None:
    """A glob matching nothing would pass every rule below it."""
    assert len(_modules()) > 50
    assert any(_in_prompt_position(ast.parse(p.read_bytes())) for p in _modules())


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_prose_is_inlined_in_a_prompt(path: Path) -> None:
    offending = [
        f"{path.relative_to(SRC)}:{line}: {len(text)} chars — {text[:48]!r}"
        for line, text in _in_prompt_position(ast.parse(path.read_bytes()))
        if len(text) > MAX_INLINE
    ]
    assert not offending, (
        f"prose inlined in a prompt (limit {MAX_INLINE} chars). Move it to "
        f"<module>.<name>{SUFFIX} in the same directory and read it with "
        f'`prompt(__name__, "<name>")`:\n' + "\n".join(offending)
    )


# --- the files themselves --------------------------------------------------------


def _prompt_files() -> list[Path]:
    return sorted(p for p in SRC.rglob(f"*{SUFFIX}") if p.is_file())


def _requested() -> list[tuple[Path, str]]:
    """Every ``prompt(__name__, "x")`` call, as (module file, prompt name)."""
    asked: list[tuple[Path, str]] = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "prompt":
                continue
            if len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
                asked.append((path, str(node.args[1].value)))
    return asked


def test_prompts_are_actually_being_used() -> None:
    assert _requested(), "nothing reads a prompt file; the rules below check nothing"


@pytest.mark.parametrize(
    ("module", "name"),
    _requested(),
    ids=lambda v: v if isinstance(v, str) else f"{Path(v).parent.name}/{Path(v).name}",
)
def test_every_requested_prompt_exists(module: Path, name: str) -> None:
    """A missing file is a runtime error at import; this makes it a test failure."""
    assert module.name == "__init__.py", (
        f"{module.relative_to(SRC)} reads a prompt, so it has to be a folder: "
        f"move it to {module.stem}/__init__.py with its text beside it"
    )
    expected = module.parent / filename_for(name)
    assert expected.is_file(), (
        f"{module.relative_to(SRC)} asks for {name!r}; no {expected.name} in its folder"
    )


def test_no_prompt_file_is_orphaned() -> None:
    """A prompt nothing reads is a prompt somebody will edit expecting an effect."""
    wanted = {(m.parent / filename_for(n)) for m, n in _requested()}
    orphans = [str(p.relative_to(SRC)) for p in _prompt_files() if p not in wanted]
    assert not orphans, f"prompt files nothing reads: {orphans}"


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_a_prompt_file_is_named_for_its_module(path: Path) -> None:
    """An ``.md`` sits in a module folder, beside the ``__init__.py`` that reads it."""
    assert "." not in path.name[: -len(SUFFIX)], (
        f"{path.name}: the folder already names the module, so the file is <name>{SUFFIX}"
    )
    assert (path.parent / "__init__.py").is_file(), (
        f"{path.relative_to(SRC)} has no __init__.py beside it. A module with prompt "
        "text is a folder: the code is its __init__.py and the text sits with it."
    )


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_a_prompt_file_has_something_in_it(path: Path) -> None:
    assert path.read_text(encoding="utf-8").strip(), f"{path.name} is empty"


# --- they have to be in the wheel ------------------------------------------------


def test_every_prompt_file_ships(tmp_path: Path) -> None:
    """A prompt that does not install is an ImportError on somebody else's machine.

    Nothing else catches it. Under `pip install -e .` the package resolves to
    this tree, so every other test here reads the files whether or not the build
    backend was ever told to include them — the failure appears only in a wheel
    nobody built, at import time, on a machine that is not this one.
    """
    import subprocess
    import sys
    import zipfile

    root = Path(__file__).resolve().parent.parent
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(tmp_path), str(root)],
        check=True,
        capture_output=True,
    )
    built = next(tmp_path.glob("ictus-*.whl"))
    shipped = {
        n.removeprefix("ictus/") for n in zipfile.ZipFile(built).namelist() if n.endswith(SUFFIX)
    }
    here = {p.relative_to(SRC).as_posix() for p in _prompt_files()}
    assert here <= shipped, f"prompt files missing from the wheel: {sorted(here - shipped)}"


def test_a_prompt_is_read_through_importlib_resources() -> None:
    """Not open(__file__/..), which breaks in a zip and in any relocated install."""
    from ictus.stdlib.llm.voice import _STANCE

    # Not the opening line: which paragraph comes first is a prompt-design
    # decision and has already moved once. That it loaded at all is the claim.
    assert "You are one voice among several" in _STANCE
    assert not _STANCE.endswith("\n"), "prompt() strips, so an editor's final newline is inert"
