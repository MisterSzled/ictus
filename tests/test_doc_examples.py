"""An example in the documentation has to name things that exist.

`test_docs_layout.test_every_python_example_parses` checks that a block is
Python, and says so: most blocks are fragments, so it stops at syntax.
`test_docs.py` checks the names in `STDLIB.md`'s catalogue table. The defect
this file exists for sits between them — `smoke/README.md` told a reader to
write `from ictus import STR`, which parses, is in no catalogue table, and does
not exist: `STR` is a local alias for `PortType.STRING` that each demo pipeline
defines for itself. Nothing would have refused it. It was caught by hand, one
command before it shipped, and the method that caught it is the method
`test_every_python_example_parses` already admits to — running the complete
examples by hand during a sweep. A sweep happens when somebody remembers.

Imports only, deliberately. Whether an example *runs* cannot be checked, since
most are fragments and some are written with `...` where a credential goes; what
can be checked is that every name it tells a reader to import is real.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Checked out, built, or somebody else's.
SKIP = {".venv", ".venv-conductor", ".git", "node_modules", "build", "__pycache__"}


def _pages() -> list[Path]:
    """Every markdown page in the repository, wherever it lives.

    Broader than `test_docs_layout._pages`, and not shared with it, on purpose:
    that list is the set of pages held to a house style, and this rule is true
    of an example anywhere — including the demo folders, which are what somebody
    copies to write their first pipeline.
    """
    return sorted(
        path for path in ROOT.rglob("*.md") if not SKIP & set(path.relative_to(ROOT).parts)
    )


def _blocks(page: Path) -> list[str]:
    text = page.read_text(encoding="utf-8")
    return re.findall(r"^```python\n(.*?)^```", text, re.S | re.M)


def _resolves(module: str, name: str) -> bool:
    """Whether `from <module> import <name>` would find something.

    A name is either an attribute of the module or a submodule of it; `from
    ictus.notify import slack` is the second kind and has no attribute until
    something imports it.
    """
    try:
        found = importlib.import_module(module)
    except ImportError:
        return False
    if hasattr(found, name):
        return True
    try:
        return importlib.util.find_spec(f"{module}.{name}") is not None
    except (ImportError, ValueError):
        return False


def _imports(block: str) -> list[tuple[str, str]]:
    """Every `ictus` name this example tells a reader to import."""
    try:
        tree = ast.parse(textwrap.dedent(block))
    except SyntaxError:
        return []  # test_docs_layout owns that failure; reporting it twice helps nobody
    wanted: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `level` is a relative import, which no example should carry.
            if node.level or not (node.module or "").startswith("ictus"):
                continue
            wanted += [(node.module or "", alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            wanted += [
                (
                    alias.name.rsplit(".", 1)[0] if "." in alias.name else alias.name,
                    alias.name.rsplit(".", 1)[-1],
                )
                for alias in node.names
                if alias.name.startswith("ictus")
            ]
    return wanted


def test_there_are_examples_to_check() -> None:
    """A regex that stopped matching would make the rule below vacuous."""
    found = [page for page in _pages() if _blocks(page)]
    assert len(found) >= 5, f"only {len(found)} pages have python examples; the scan broke"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_example_imports_something_real(page: Path) -> None:
    missing = sorted(
        {
            f"from {module} import {name}"
            for block in _blocks(page)
            for module, name in _imports(block)
            if not _resolves(module, name)
        }
    )
    assert not missing, (
        f"{page.relative_to(ROOT)} tells a reader to write {missing}, which does not "
        "exist. An example naming something imaginary is worse than no example: it is "
        "believed before the traceback is."
    )
