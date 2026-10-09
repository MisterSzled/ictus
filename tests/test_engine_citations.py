"""Claims about Conductor's internals point at symbols, not line numbers.

Nine of the thirteen engine citations in this repository were stale when this
file was written, all in the same way: the line number was right once and the
engine moved. One of them had been corrected in `lints.py` two commits earlier
and missed in `scope.py`, because the audit went finding by finding rather than
symbol by symbol. Two more cited a docstring; one pair was simply reversed.

A line number cannot be checked without reading the line, so nobody did. A
symbol can be, so this does. The rule that falls out: cite
``engine/workflow.py``, ``_resolve_agent_working_dir`` — never the same file with a
line number glued on, which is what this refuses.

Conductor is a separate installation, not importable from this project's venv
(`AGENTS.md` says why, and how to find it), so this resolves the console script
the same way.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_WHERE = "import conductor, pathlib; print(pathlib.Path(conductor.__file__).parent)"

#: `engine/workflow.py` and friends, optionally followed by a line number. The
#: line number is what this file exists to forbid.
CITATION = re.compile(
    r"\b((?:config|engine|web|providers|executor|gates|interrupt|execution|fleet)"
    r"/[a-z_]+\.py)(:\d+(?:-\d+)?)?"
)

#: A nearby ``symbol`` in backticks or double backticks. Conductor's own names
#: are snake_case or CamelCase; a word with neither is prose.
SYMBOL = re.compile(r"[`']{1,2}([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)[`']{1,2}")


def _sources() -> list[Path]:
    found: list[Path] = []
    for area in ("src", "tests", "docs", "smoke", ".claude"):
        for pattern in ("*.py", "*.md"):
            found += (ROOT / area).rglob(pattern)
    found += [ROOT / name for name in ("README.md", "AGENTS.md", "STDLIB.md")]
    return [p for p in found if p.is_file() and "build" not in p.parts]


def _engine_root() -> Path | None:
    """Where Conductor is installed, via its console script's sibling python."""
    script = shutil.which("conductor")
    if script is None:
        return None
    python = Path(os.path.realpath(script)).parent / "python"
    if not python.exists():
        return None
    found = subprocess.run(
        [str(python), "-c", _WHERE],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(found.stdout.strip()) if found.returncode == 0 else None


def _cited() -> list[tuple[Path, str, str | None, str]]:
    """Every engine citation: the file it is in, the engine file, any line, the context."""
    found: list[tuple[Path, str, str | None, str]] = []
    for page in _sources():
        text = page.read_text(encoding="utf-8")
        for match in CITATION.finditer(text):
            before = text[max(0, match.start() - 180) : match.start()]
            after = text[match.end() : match.end() + 180]
            # Keep to the citation's own sentence: a neighbouring one often
            # names a symbol belonging to a different engine file.
            before = re.split(r"(?<=[.;])\s", before)[-1]
            after = re.split(r"(?<=[.;])\s", after)[0]
            context = before + match.group(0) + after
            found.append((page, match.group(1), match.group(2), context))
    return found


def test_there_are_citations_to_check() -> None:
    """A regex that matched nothing would pass every assertion below it."""
    assert len(_cited()) > 5


def test_no_citation_carries_a_line_number() -> None:
    """The number is right until the engine is edited above it, and then silently not."""
    numbered = [
        f"{page.relative_to(ROOT)}: {engine}{line}" for page, engine, line, _ in _cited() if line
    ]
    assert not numbered, (
        f"{numbered}. Name the function or class instead — a line number cannot be "
        "checked, so nobody checks it."
    )


def test_every_cited_engine_file_exists() -> None:
    engine = _engine_root()
    if engine is None:
        pytest.skip("conductor is not installed; see AGENTS.md")
    missing = sorted(
        {
            f"{page.relative_to(ROOT)}: {name}"
            for page, name, _, _ in _cited()
            if not (engine / name).exists()
        }
    )
    assert not missing, f"cited engine files that do not exist: {missing}"


def _our_own_names() -> set[str]:
    """Every name defined in this project, so a citation is not blamed for one.

    `AgentNode` sits next to `executor/script.py` in a sentence contrasting the
    two, and belongs to us. A name we define is never evidence about the engine.
    """
    names: set[str] = set()
    for module in (ROOT / "src").rglob("*.py"):
        names |= set(re.findall(r"^\s*(?:class|def)\s+(\w+)", module.read_text(), re.M))
    return names


def test_every_symbol_named_beside_a_citation_is_real() -> None:
    """The citation's whole value is that it points somewhere real.

    The symbol is looked for across the whole engine package rather than in the
    file cited beside it. A sentence often contrasts two files, and an event
    name travels between the emitter and the telemetry module — attributing each
    name to one file produced three false alarms and would have taught people to
    ignore this. Package-wide still catches the real failure: a symbol that was
    renamed, or one that never existed. `_run_subworkflow` was caught here, in a
    citation this very commit had just written.

    Names this project defines are skipped, and so is anything that does not
    look like an identifier — the point is to catch a dead symbol, not to parse
    English.
    """
    engine = _engine_root()
    if engine is None:
        pytest.skip("conductor is not installed; see AGENTS.md")

    ours = _our_own_names()
    bodies: dict[str, str] = {}
    wrong: set[str] = set()
    for page in _sources():
        text = page.read_text(encoding="utf-8")
        for match in CITATION.finditer(text):
            window = text[max(0, match.start() - 180) : match.end() + 180]
            nearby = {m.group(1) for m in CITATION.finditer(window)}
            if "package" not in bodies:
                bodies["package"] = "\n".join(
                    source.read_text(encoding="utf-8", errors="replace")
                    for source in engine.rglob("*.py")
                )
            pool = bodies["package"]
            for symbol in SYMBOL.findall(window):
                bare = symbol.split(".")[-1]
                if bare in ours:
                    continue
                if not re.search(r"[a-z]_[a-z]|^_[a-z]|^[A-Z][a-z]+[A-Z]", bare):
                    continue
                if re.search(rf"\b{re.escape(bare)}\b", pool):
                    continue
                wrong.add(f"{page.relative_to(ROOT)} names {symbol!r} beside {sorted(nearby)}")
    assert not wrong, sorted(wrong)
