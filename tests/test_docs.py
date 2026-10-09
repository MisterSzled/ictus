"""The stdlib catalogue has to describe the stdlib that exists.

Catches a thing added and not written down, a thing removed and left in, an
option renamed underneath its description.

Checks names and signatures, never prose.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import ictus.stdlib as stdlib
import ictus.stdlib.llm as stdlib_llm
from ictus.runspec.config import PipelineConfig

if TYPE_CHECKING:
    from ictus.graph.scope import Scope

CATALOGUE = Path(__file__).resolve().parent.parent / "STDLIB.md"
DOC = CATALOGUE.read_text(encoding="utf-8")

# Constructors live above the "## Running" section; below it are tables about
# settings and commands, whose first column looks the same and is not a
# constructor. Scoping the scan is what keeps `provider:` from reading as one.
CATALOGUE_TEXT = DOC.split("\n## Running", 1)[0]

# A table row naming a constructor: `| `name` | use | options | ... |`
#: A catalogue row: `| `name` | use |`, with an optional third column of
#: outcomes for a scope. The options and produces columns went to
#: `ictus stdlib <name>`, which reads `inspect.signature` and the docstrings, so
#: a parameter renamed underneath its description is no longer possible to write
#: down — which is why the test that used to check that is gone rather than
#: broken.
ROWS = {
    m.group(1): (m.group(3) or "").strip()
    for m in re.finditer(
        r"^\| `([a-z_]+)` \| ([^|]+?) \|(?: ([^|]*) \|)?\s*$", CATALOGUE_TEXT, re.M
    )
}

CODE = "\n".join(re.findall(r"```(?:python|sh)\n(.*?)```", DOC, re.S))

# The catalogue covers the stdlib that exists, not the subset re-exported
# flat: `ictus.stdlib.llm` is not in the top-level namespace, but a pipeline
# may still reach for one.
MODULES = (stdlib, stdlib_llm)


def _exported(predicate: object) -> list[str]:
    return sorted(
        {
            name
            for module in MODULES
            for name in module.__all__
            if predicate(getattr(module, name))  # type: ignore[operator]
        }
    )


def _find(name: str) -> object | None:
    """The thing the catalogue names, from wherever in the stdlib it lives."""
    for module in MODULES:
        found: object = getattr(module, name, None)
        if found is not None:
            return found
    return None


CONSTRUCTORS = [n for n in _exported(callable) if n[0].islower()]
SPECS = _exported(inspect.isclass)


def test_the_catalogue_exists_at_the_project_root() -> None:
    assert CATALOGUE.is_file()


@pytest.mark.parametrize("name", CONSTRUCTORS)
def test_every_stdlib_constructor_is_catalogued(name: str) -> None:
    """Added and not written down is the failure this catches."""
    assert name in ROWS, f"{name} is exported from the stdlib but has no row in STDLIB.md"


#: The catalogue became an index, so a spec type may be documented on the page
#: that owns the thing taking it — `Voice` moved with the scopes. Searching the
#: pages STDLIB.md links to is the same question asked of the whole set.
LINKED = DOC + "".join(
    (CATALOGUE.parent / target).read_text(encoding="utf-8")
    for target in re.findall(r"\]\((docs/[\w.-]+\.md)\)", DOC)
    if (CATALOGUE.parent / target).is_file()
)


@pytest.mark.parametrize("name", SPECS)
def test_every_spec_type_is_mentioned(name: str) -> None:
    """`Voice`, `Attempt`, `ScriptStep`, `ReviewOption` — you cannot use the stage without them."""
    assert f"`{name}" in LINKED, (
        f"{name} is exported but appears in neither STDLIB.md nor any page it links to"
    )


@pytest.mark.parametrize("name", sorted(ROWS))
def test_nothing_catalogued_has_been_removed(name: str) -> None:
    """Removed and left in is the other half, and the one that misleads."""
    assert _find(name) is not None, f"STDLIB.md documents {name!r}, which the stdlib no longer has"


@pytest.mark.parametrize("name", sorted(n for n in ROWS if ROWS[n]))
def test_every_documented_outcome_is_a_real_one(name: str) -> None:
    """A scope's outcomes are what you write `branch_on_outcome` against.

    The options column is gone — `ictus stdlib <name>` reads those off the
    signature, so they cannot drift. Outcomes stayed in the table because you
    need them in front of you to route a scope, which means they can still be
    wrong, which means they still need checking.
    """
    from ictus.stdlib.scopes import outcomes

    real = {getattr(outcomes, n) for n in outcomes.__all__}
    claimed = set(re.findall(r"`([a-z_]+)`", ROWS[name]))
    unknown = sorted(claimed - real)
    assert not unknown, f"STDLIB.md gives {name} outcome(s) {unknown}; real ones are {sorted(real)}"

    # ...and that this scope actually produces them. Checking only that a name
    # is *some* outcome let `halted` pass for a default council, which declares
    # two. `branch_on_outcome` refuses a route to an outcome a scope cannot
    # reach, so a row that lists one sends the reader into a CompositionError.
    built = _a_scope(name)
    if built is not None:
        cannot = sorted(claimed - set(built.outcomes))
        assert not cannot, (
            f"STDLIB.md gives {name} outcome(s) {cannot}, which it does not declare: "
            f"{sorted(built.outcomes)}"
        )


def test_the_outcome_vocabularies_are_stated_correctly() -> None:
    """A caller must route every outcome, so the catalogue naming them wrongly is a trap."""
    assert stdlib.CONVERGED == "converged"
    assert stdlib.EXHAUSTED == "exhausted"
    assert stdlib.AGREED == "agreed"
    assert stdlib.UNRESOLVED == "unresolved"
    assert stdlib.HALTED == "halted"
    assert stdlib.OK == "ok"
    assert stdlib.FAILED == "failed"
    assert stdlib.ANSWERED == "answered"
    assert stdlib.READ == "read"
    assert stdlib.MISSING == "missing"
    for constant in ("converged", "exhausted", "agreed", "unresolved", "halted", "ok", "failed"):
        assert f"`{constant}`" in DOC, f"outcome {constant!r} is not named in STDLIB.md"


def _a_scope(name: str) -> Scope | None:
    """One instance of a scope with its defaults, or ``None`` if it needs more.

    Built rather than introspected because a scope's outcome vocabulary is
    decided at construction — `halted` exists on a council only with
    `interject=True`.
    """
    from ictus.stdlib.scopes import Speaker, Voice

    pair = (
        Voice(node_id="a", persona="p", focus="f"),
        Voice(node_id="b", persona="p", focus="f"),
    )
    speakers = (
        Speaker(node_id="a", persona="p", focus="f"),
        Speaker(node_id="b", persona="p", focus="f"),
    )
    try:
        match name:
            case "council":
                return stdlib.council(stage_id="s", voices=pair)
            case "roundtable":
                return stdlib.roundtable(stage_id="s", speakers=speakers)
            case "try_shell":
                return stdlib.try_shell(stage_id="s", command="true")
            case "read_ticket":
                return stdlib.read_ticket(stage_id="s", against=None)  # type: ignore[arg-type]
            case _:
                return None
    except Exception:
        return None


README = CATALOGUE.parent / "README.md"


def test_the_readme_points_at_the_catalogue() -> None:
    assert "STDLIB.md" in README.read_text(encoding="utf-8")


def _readme_signposts() -> list[str]:
    """Every constructor named in the README's "what is already built" table."""
    text = README.read_text(encoding="utf-8")
    block = text[text.index("## What is already built for you") : text.index("## Three tiers")]
    return sorted(
        {
            name
            for row in block.splitlines()
            if row.startswith("|")
            for name in re.findall(r"`([a-z_]+)`", row)
        }
    )


def test_the_readme_table_is_not_empty() -> None:
    """A heading that moved would make the parametrised test below vacuous."""
    assert len(_readme_signposts()) > 15


@pytest.mark.parametrize("name", _readme_signposts())
def test_every_readme_signpost_resolves(name: str) -> None:
    assert _find(name) is not None, (
        f"the README offers {name!r} to someone writing their first pipeline, "
        "and the stdlib does not have it"
    )


@pytest.mark.parametrize("gone", ["revise_loop", "poll_until"])
def test_deleted_constructors_are_not_offered_as_usable(gone: str) -> None:
    """Both were replaced by `converge`.

    Naming them in prose is fine; listing them as available, or in code
    somebody could copy, is not.
    """
    assert not hasattr(stdlib, gone)
    assert gone not in ROWS, f"STDLIB.md still offers {gone} as a constructor"
    assert gone not in CODE, f"STDLIB.md has copyable code using {gone}"
    readme = (CATALOGUE.parent / "README.md").read_text(encoding="utf-8")
    assert f"`{gone}`" not in readme, f"README.md still lists {gone}"


AGENTS = CATALOGUE.parent / "AGENTS.md"


class TestRepositoryInstructions:
    """What a run picks up from the repository itself, rather than from a pipeline.

    `ictus run` passes `--workspace-instructions`, and Conductor walks up to
    the git root for `AGENTS.md`, `.github/copilot-instructions.md`,
    `CLAUDE.md` and `.github/instructions/*.instructions.md`. Nothing else
    reaches a step: the provider pins `setting_sources=[]`.

    Under test: the repo-wide account lives here once, and a pipeline's own
    instructions carry only what is specific to that pipeline.
    """

    def test_the_repo_carries_an_agents_file_where_the_engine_looks(self) -> None:
        assert AGENTS.is_file(), "AGENTS.md is what a run against this repo reads"
        assert AGENTS.stat().st_size > 0

    def test_it_says_how_to_reach_the_engine_source(self) -> None:
        """Every "the engine cannot do X" claim is settled by this lookup."""
        text = AGENTS.read_text(encoding="utf-8")
        assert "readlink -f" in text, "the console-script resolution must be spelled out"
        assert "config/schema.py" in text
        assert "not importable" in text or "not* importable" in text

    def test_no_pipeline_restates_the_project(self) -> None:
        """Duplicated, the two drift and the run reads whichever is stale.

        Reads whatever prose a committed pipeline carries, rather than naming
        one file.
        """
        folders = sorted((CATALOGUE.parent / "demo_work" / "pipelines").glob("*/"))
        assert folders, "the gate needs at least one committed pipeline; see .gitignore"
        for prose in sorted((CATALOGUE.parent / "demo_work" / "pipelines").glob("*/*.md")):
            text = prose.read_text(encoding="utf-8")
            assert "readlink -f" not in text, (
                f"{prose}: the engine lookup belongs in AGENTS.md, which the run discovers"
            )
            assert "conductor-cli" not in text, prose


def test_every_config_setting_is_in_the_table() -> None:
    """`docs/configuration.md` is where somebody looks up what `config.yaml` takes.

    A setting absent from it is a setting nobody finds. `workspace_instructions`
    was missing while eleven of its twelve siblings were documented — it decides
    whether a run reads the target project's `AGENTS.md` at all, so the one
    undocumented field was not a minor one.
    """
    page = (CATALOGUE.parent / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([a-z_]+)`", page))
    missing = sorted(f.name for f in fields(PipelineConfig) if f.name not in documented)
    assert not missing, f"docs/configuration.md does not document {missing}"


def test_the_stdlib_group_table_lists_what_each_folder_holds() -> None:
    """`docs/source-layout.md` names every constructor, folder by folder.

    `STDLIB.md` is pinned to the library by `test_every_stdlib_constructor_is
    _catalogued`; this table is a second list of the same names, and nothing
    held it. Adding a constructor and forgetting one of the two is the way a
    reader is told a folder holds less than it does.
    """
    page = (CATALOGUE.parent / "docs" / "source-layout.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(\w+)/` \| [^|]+ \| ([^|]+) \|$", page, re.M)
    assert len(rows) >= 6, f"only {len(rows)} group rows found; the table moved"

    wrong: list[str] = []
    for folder, listed in rows:
        named = set(re.findall(r"`(\w+)`", listed))
        module = import_module(f"ictus.stdlib.{folder}")
        real = {
            name
            for name in dir(module)
            if not name.startswith("_") and name[0].islower() and callable(getattr(module, name))
        }
        if absent := sorted(named - real):
            wrong.append(f"{folder}/ is documented as holding {absent}, which it does not")
        if unlisted := sorted(real - named):
            wrong.append(f"{folder}/ holds {unlisted}, which the table does not name")
    assert not wrong, wrong
