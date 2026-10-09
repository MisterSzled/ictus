"""Every page is reachable, and every link lands.

Splitting a long document into short ones only helps if the short ones can be
found. `STDLIB.md` was 4,400 words and all of it load-bearing — measuring showed
barely 2% of its prose was anywhere else — so it had to be broken up rather than
cut. The hazard that creates is a page nobody links to: still correct, still
maintained, and invisible.

So reachability is checked rather than hoped for. The rules are the same two the
prompt layout uses one level down — nothing orphaned, nothing dangling — because
a document set is a resource tree with the same two ways to break.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Where documentation lives. `.claude/skills` is included because a skill's
#: reference files are reachable only from its SKILL.md, which is the same rule.
AREAS = ("docs", ".claude/skills")

#: Pages that are entry points: somewhere else links to them, or a person types
#: their name. Nothing inside the repository has to point at them. A `SKILL.md`
#: is one too — the router reaches it by its frontmatter, never by a link, which
#: is the whole mechanism.
ENTRY = {"README.md", "AGENTS.md", "STDLIB.md", "CHANGELOG.md", "smoke/README.md"}


def _is_entry(page: Path) -> bool:
    rel = page.relative_to(ROOT).as_posix()
    return rel in ENTRY or page.name == "SKILL.md"


#: Skipped when checking that a link lands, because they are not repository
#: paths: anchors, URLs, and the placeholder paths docs use in examples.
NOT_A_PATH = ("http", "#", "mailto:", "<")


def _markdown() -> list[Path]:
    found = [ROOT / name for name in ENTRY]
    for area in AREAS:
        found += sorted((ROOT / area).rglob("*.md"))
    return [p for p in found if p.is_file()]


def _links(path: Path) -> list[str]:
    """Relative paths this page links to, as written."""
    body = path.read_text(encoding="utf-8")
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    return [
        target.split("#")[0]
        for target in re.findall(r"\]\(([^)]+)\)", body)
        if not target.startswith(NOT_A_PATH)
    ]


def test_there_are_pages_to_check() -> None:
    """A glob that matched nothing would pass everything below it."""
    assert len(_markdown()) > 8


@pytest.mark.parametrize("page", _markdown(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_link_lands(page: Path) -> None:
    """A link to a page that moved is worse than no link: it reads as checked."""
    broken = [
        target
        for target in _links(page)
        if target and not (page.parent / target).resolve().exists()
    ]
    assert not broken, f"{page.relative_to(ROOT)} links to {broken}, which do not exist"


def _linked_from_anywhere() -> set[Path]:
    found: set[Path] = set()
    for page in _markdown():
        for target in _links(page):
            if target:
                found.add((page.parent / target).resolve())
    return found


@pytest.mark.parametrize(
    "page",
    [p for p in _markdown() if not _is_entry(p)],
    ids=lambda p: p.relative_to(ROOT).as_posix(),
)
def test_every_page_is_reachable(page: Path) -> None:
    """Nothing in `docs/` or a skill is findable except by being linked to.

    `docs/run-events-plan.md` was unreachable for months while saying Phase 5
    was unstarted — work that had long since shipped. Nobody linked it, so
    nobody reread it.
    """
    assert page.resolve() in _linked_from_anywhere(), (
        f"{page.relative_to(ROOT)} is linked from nowhere. Add it to a page that "
        "is reachable, with a line saying when to read it."
    )


def test_the_index_names_every_page_it_split_into() -> None:
    """`STDLIB.md` is the index for what came out of it; the map has to be complete."""
    index = (ROOT / "STDLIB.md").read_text(encoding="utf-8")
    came_out_of_it = {
        "docs/wiring.md",
        "docs/deliberation.md",
        "docs/reporting.md",
        "docs/running-a-pipeline.md",
        "docs/gotchas.md",
    }
    missing = sorted(page for page in came_out_of_it if page not in index)
    assert not missing, f"STDLIB.md was split into {missing} and does not name them"
