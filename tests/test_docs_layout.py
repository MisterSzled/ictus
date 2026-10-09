"""Documentation is laid out to be consumed, by a reader or a model.

These are the rules from `.claude/skills/skill-writer/check_skill.py` that
survive the trip from a skill to a document. Running that checker on `docs/`
directly is useless — every page fails on "no YAML frontmatter" and returns
before anything else runs — so what transfers was taken and the rest left.

What transfers: a page you load has a size, and a wall inside it is worse than
its total; asking for blanket approval is dangerous wherever it is written; a
link that dangles and a page nothing reaches are both still bugs. What does not:
frontmatter, `name`, kebab-case, the Level 1 budget, bundled scripts.

Reachability is in `test_docs_reachable.py`; this file is about what a page
looks like once you are on it.
"""

from __future__ import annotations

import ast
import itertools
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: One section past this is a wall: you scroll it rather than scan it, and a
#: model loads all of it to answer a question about any of it. `docs/gotchas.md`
#: was a single 2,587-token section of twenty entries before it was grouped.
SECTION_CEILING = 1200

#: Prose wraps; the repository writes at 79. The ceiling is loose because the
#: rule is "no walls of text", not a house style — and frontmatter is exempt
#: because a `description:` is one YAML scalar and folding it would change it.
LINE_CEILING = 110

#: From the skill checker, unchanged. A benign task-specific approval with
#: "Don't ask again" carries over to closely related harmful actions, and a
#: document telling a model to ask for one is the same hazard as a skill doing it.
BLANKET_APPROVAL = (
    "don't ask again",
    "dont ask again",
    "do not ask again",
    "always approve",
    "auto-approve",
    "skip confirmation",
)

AREAS = ("docs", ".claude/skills")
TOP = ("README.md", "AGENTS.md", "STDLIB.md", "smoke/README.md")


def _pages() -> list[Path]:
    found = [ROOT / name for name in TOP]
    for area in AREAS:
        found += sorted((ROOT / area).rglob("*.md"))
    return [p for p in found if p.is_file()]


def _prose(text: str) -> str:
    """The text with fenced blocks removed.

    A `#` inside a shell block is a comment and a `|` is a pipe. Counting either
    as markdown is how a checker reports three H1s in a file with one.
    """
    return re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)


def _sections(text: str) -> dict[str, int]:
    """Each `##` section and its rough token count.

    A page with no `##` at all is one section: itself. Returning `{}` for it was
    a hole — `docs/preflight.md` ran unsectioned and therefore unmeasured, and
    could have grown to any size without the ceiling ever applying.
    """
    body = _prose(text)
    parts = re.split(r"^## ", body, flags=re.M)[1:]
    if not parts:
        return {"(whole page, no sections)": len(body) // 4}
    return {s.splitlines()[0][:50]: len(s) // 4 for s in parts if s.strip()}


def test_there_are_pages_to_check() -> None:
    assert len(_pages()) > 8


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_section_is_a_wall(page: Path) -> None:
    """A long page is fine; a long *section* is not.

    Splitting by heading is what lets a reader skip and a model load less. This
    is the check that forced `gotchas.md` into five sections named for when you
    would hit each one.
    """
    oversized = {
        name: size
        for name, size in _sections(page.read_text(encoding="utf-8")).items()
        if size > SECTION_CEILING
    }
    assert not oversized, (
        f"{page.relative_to(ROOT)} has section(s) over ~{SECTION_CEILING} tokens: {oversized}. "
        "Break them up, naming each for the question it answers."
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_page_says_what_it_is_before_its_first_section(page: Path) -> None:
    """A heading and then straight into `##` leaves a reader guessing.

    The doc version of a skill's `description`: a page has to say what it covers
    and when to read it, before anything else.
    """
    text = _prose(page.read_text(encoding="utf-8"))
    lead = re.split(r"^## ", text, flags=re.M)[0]
    skip = ("#", "---", "name:", "description:")
    body = "\n".join(line for line in lead.splitlines() if not line.startswith(skip))
    assert len(body.split()) >= 15, (
        f"{page.relative_to(ROOT)} opens straight into its first section. "
        "Say what the page is and when to read it."
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_exactly_one_title(page: Path) -> None:
    text = _prose(page.read_text(encoding="utf-8"))
    titles = re.findall(r"^# (.*)$", text, re.M)
    assert len(titles) == 1, f"{page.relative_to(ROOT)} has {len(titles)} H1s: {titles}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_heading_level_is_skipped(page: Path) -> None:
    """`#` to `###` reads as a missing section rather than a deliberate one."""
    levels = [len(h) for h in re.findall(r"^(#+) \S", _prose(page.read_text()), re.M)]
    jumps = [(a, b) for a, b in itertools.pairwise(levels) if b - a > 1]
    assert not jumps, f"{page.relative_to(ROOT)} skips heading levels: {jumps}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_code_block_names_its_language(page: Path) -> None:
    """A bare fence renders unhighlighted and tells a reader nothing.

    `text` is the honest answer for a tree or a diagram — it is not a shell
    session, and saying so stops a highlighter guessing.
    """
    fences = re.findall(r"^(```.*)$", page.read_text(encoding="utf-8"), re.M)
    bare = [f for f in fences[0::2] if f.strip() == "```"]
    assert not bare, (
        f"{page.relative_to(ROOT)} has {len(bare)} code block(s) with no language. "
        "Use ```sh, ```python, ```yaml or ```text."
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_prose_is_wrapped(page: Path) -> None:
    """Frontmatter and tables exempt: neither can wrap without changing meaning."""
    text = page.read_text(encoding="utf-8")
    frontmatter = text.startswith("---")
    long_lines, in_code = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith("|") or (frontmatter and number <= 5):
            continue
        if len(line) > LINE_CEILING:
            long_lines.append(f"{number} ({len(line)} chars)")
    assert not long_lines, f"{page.relative_to(ROOT)} has unwrapped prose at {long_lines}"


def _used_not_mentioned(text: str) -> str:
    """The text with quoted and backticked spans removed.

    The use-mention distinction, and the same reasoning that makes the vendor
    check tokenise: `trust-and-review.md` explains that an approval carrying
    "Don't ask again" is dangerous, which is the opposite of asking for one. A
    phrase in quotes is being named; a phrase in running prose is being said.
    """
    text = re.sub(r"`[^`]*`", " ", text)
    return re.sub(r"[\"\u201c\u2018][^\"\u201d\u2019]*[\"\u201d\u2019]", " ", text)


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_nothing_asks_for_blanket_approval(page: Path) -> None:
    lowered = _used_not_mentioned(page.read_text(encoding="utf-8")).lower()
    asked = [phrase for phrase in BLANKET_APPROVAL if phrase in lowered]
    assert not asked, (
        f"{page.relative_to(ROOT)} asks for blanket approval {asked}; it carries over to "
        "closely related harmful actions."
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_nothing_is_left_unfinished(page: Path) -> None:
    """A TODO in a page a model is told to trust is an instruction to improvise."""
    left = re.findall(r"\b(TODO|TBD|FIXME|XXX)\b", page.read_text(encoding="utf-8"))
    assert not left, f"{page.relative_to(ROOT)} still says {sorted(set(left))}"


def test_the_source_tree_names_only_paths_that_exist() -> None:
    """`docs/source-layout.md` draws the tree, so the tree has to be real.

    It is package-level for exactly this reason: a file-by-file version named
    `stdlib/prompts.py` for a week after that module became `stdlib/baseline/`,
    and nothing noticed because nothing checked.
    """
    page = ROOT / "docs" / "source-layout.md"
    drawn = re.search(r"```text\n(src/ictus/.*?)```", page.read_text(encoding="utf-8"), re.S)
    assert drawn, "no source tree found in docs/source-layout.md"

    named: list[Path] = []
    stack: list[str] = []
    for line in drawn.group(1).splitlines():
        match = re.match(r"^(\s*)(\S+)", line)
        if not match or match.group(2).startswith("src/"):
            continue
        depth = len(match.group(1)) // 2
        stack = [*stack[: depth - 1], match.group(2).rstrip("/")]
        named.append(ROOT / "src" / "ictus" / "/".join(stack))

    missing = sorted(str(p.relative_to(ROOT)) for p in named if not p.exists())
    assert not missing, f"docs/source-layout.md draws paths that do not exist: {missing}"


def test_the_source_tree_names_every_package() -> None:
    """A package missing from the tree is a package nobody knows is there."""
    page = (ROOT / "docs" / "source-layout.md").read_text(encoding="utf-8")
    packages = {p.parent.name for p in (ROOT / "src" / "ictus").glob("*/__init__.py")}
    missing = sorted(name for name in packages if f"{name}/" not in page)
    assert not missing, f"docs/source-layout.md does not draw {missing}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_table_has_been_flattened_into_prose(page: Path) -> None:
    """A row that lost its line breaks renders as a wall of pipes.

    This is here because a reflow pass did exactly that to the provider table in
    `gotchas.md`, and every other check passed: the page was still the right
    size, still wrapped, still one H1. Only a reader would have noticed.
    """
    bad = [
        f"{number}: {line.strip()[:60]}"
        for number, line in enumerate(_prose(page.read_text(encoding="utf-8")).splitlines(), 1)
        if line.strip().count("|") >= 2 and not line.strip().startswith(("|", "#"))
    ]
    assert not bad, f"{page.relative_to(ROOT)} has table rows inside a paragraph at {bad}"


def test_the_layers_diagram_names_every_package() -> None:
    """`AGENTS.md` draws the direction, so a new package has to appear in it.

    The source tree one file over is checked the same way, and this one was not:
    `cli`, `plugins` and `prompting` were all missing, which is the one question
    the diagram exists to answer.
    """
    page = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    drawn = re.search(r"```text\n(errors .*?)```", page, re.S)
    assert drawn, "no layers diagram found in AGENTS.md"
    packages = {p.parent.name for p in (ROOT / "src" / "ictus").glob("*/__init__.py")}
    missing = sorted(name for name in packages if name not in drawn.group(1))
    assert not missing, f"AGENTS.md draws no direction for {missing}"


#: A page past this is a manual, and nobody reads a manual to answer one
#: question. The skill checker's Level 2 ceiling, applied to a document for the
#: same reason: it is loaded whole or not at all. README.md reached 5,679 before
#: anything measured it, by growing a copy of nearly every page under `docs/`.
PAGE_CEILING = 3000

#: The longest run of words two pages may share. Set above what survives
#: deliberately — a quoted rule, a shared definition — and far below a copied
#: section. Two README sections were word-for-word identical to
#: `running-a-pipeline.md` and nothing noticed, because nothing compared pages.
SHARED_RUN_CEILING = 40


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_page_is_a_manual(page: Path) -> None:
    """Sections can each be small while the page they are in is enormous."""
    size = len(_prose(page.read_text(encoding="utf-8"))) // 4
    assert size <= PAGE_CEILING, (
        f"{page.relative_to(ROOT)} is ~{size} tokens, over ~{PAGE_CEILING}. "
        "Give the parts that answer their own question their own page, and link them."
    )


def _words(page: Path) -> list[str]:
    return re.findall(r"[a-z0-9]+", _prose(page.read_text(encoding="utf-8")).lower())


def _longest_shared_run(first: list[str], second: list[str]) -> int:
    """How many consecutive words the two have in common, at most.

    Binary search over shingle length: if they share no run of n words they
    share none of n+1 either, so the predicate is monotone.
    """

    def share(size: int) -> bool:
        head = {tuple(first[i : i + size]) for i in range(len(first) - size + 1)}
        tail = {tuple(second[i : i + size]) for i in range(len(second) - size + 1)}
        return bool(head & tail)

    # Searching past the ceiling buys nothing: the answer is already "too much".
    # A reported run equal to the bound means "at least this", hence the `+`.
    best, low, high = 0, 1, SHARED_RUN_CEILING + 1
    while low <= high:
        mid = (low + high) // 2
        if share(mid):
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best


def test_no_page_carries_a_copy_of_another() -> None:
    """One owner per fact; everyone else links to it.

    A copy is not merely waste — it rots apart from the original. Every stale
    claim found in the last audit was in a copy: the README said a roundtable
    reads alone first, and said gates are `HUMAN_DECISION`, for exactly as long
    as it took the owning page to be corrected without it.
    """
    words = {page: _words(page) for page in _pages()}
    copied = [
        f"{a.relative_to(ROOT)} and {b.relative_to(ROOT)} share {run}+ words"
        for a, b in itertools.combinations(words, 2)
        if (run := _longest_shared_run(words[a], words[b])) > SHARED_RUN_CEILING
    ]
    assert not copied, f"{copied}. Decide which page owns it; the other links there."


#: Installing this name from an index gets you somebody else's package. The
#: distribution really is called `conductor-cli`, but that name on PyPI belongs
#: to an unrelated research orchestrator whose command is `cond` — it installs
#: cleanly, provides no `conductor`, and `smoke/README.md` records the afternoon
#: it cost. Naming it is fine; telling somebody to install it is not.
INSTALL_FROM_AN_INDEX = re.compile(
    r"(?:pip|uv(?:\s+tool|\s+pip)?)\s+install\s+(?:[-\w]+\s+)*conductor-cli"
)

#: A span in single backticks on one line. Stripping these is the use-mention
#: distinction again — `smoke/README.md` quotes the command in order to warn
#: against it. `_used_not_mentioned` is the wrong tool here because it also
#: strips fenced blocks, and the defect this found was inside one: a
#: `setup_hint=` in a Python example.
INLINE_CODE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_nothing_tells_anyone_to_install_conductor_from_an_index(page: Path) -> None:
    """A guard for this existed and covered only `demo_work/pipelines/*/*.md`.

    `docs/reaching-the-project.md` carried `uv tool install conductor-cli` as a
    `setup_hint` — the text ictus prints to somebody whose preflight just failed,
    which is exactly the moment they will run what it says.
    """
    said = INLINE_CODE.sub(" ", page.read_text(encoding="utf-8"))
    found = INSTALL_FROM_AN_INDEX.findall(said)
    assert not found, (
        f"{page.relative_to(ROOT)} says {found}; that name on an index is a different "
        "package. Conductor installs from its repository."
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_python_example_parses(page: Path) -> None:
    """An example that does not parse is worse than no example.

    Most blocks here are fragments, so this checks syntax and not whether they
    run. The two complete pipelines — in `README.md` and the pipeline skill's
    `building-the-graph.md` — were executed by hand during the page-by-page
    sweep: both compose, lint clean against the Conductor backend, and emit.

    What a by-hand sweep misses is in `test_doc_examples.py`, which resolves
    every name an example imports from `ictus`: `from ictus import STR` parses
    and is not a thing.
    """
    broken: list[str] = []
    text = page.read_text(encoding="utf-8")
    for block in re.findall(r"^```python\n(.*?)^```", text, re.S | re.M):
        try:
            ast.parse(textwrap.dedent(block))
        except SyntaxError as exc:
            broken.append(f"line {exc.lineno}: {exc.msg}")
    assert not broken, f"{page.relative_to(ROOT)} has python that does not parse: {broken}"
