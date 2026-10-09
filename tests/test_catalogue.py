"""`ictus stdlib` — the catalogue, read off the code rather than a document.

What is asserted is completeness, not contents: a constructor in a group's
`__all__` is in the catalogue.
"""

from __future__ import annotations

import inspect

import pytest
from typer.testing import CliRunner

import ictus.stdlib as stdlib
import ictus.stdlib.llm as stdlib_llm
from ictus.cli import app
from ictus.stdlib.catalogue import OUTCOMES, catalogue, find, groups

runner = CliRunner()


def _invoke(*args: str) -> str:
    result = runner.invoke(app, ["stdlib", *args])
    assert result.exit_code == 0, result.output
    return result.output


# --- the catalogue is the code ------------------------------------------------


def test_every_flat_export_that_is_a_constructor_is_catalogued() -> None:
    """The failure this prevents: a new group, and nobody listing it."""
    exported = {
        name for name in stdlib.__all__ if name[0].islower() and callable(getattr(stdlib, name))
    }
    assert exported <= {entry.name for entry in catalogue()}


def test_the_demoted_model_calls_are_catalogued_too() -> None:
    """`ictus.stdlib.llm` is not re-exported flat, which is not a reason to hide it."""
    assert set(stdlib_llm.__all__) - {"SATISFIED"} <= {e.name for e in catalogue()}


def test_each_entry_says_where_to_import_it_from() -> None:
    """A catalogue that names a thing and not its module is half an answer."""
    by_name = {e.name: e for e in catalogue()}
    assert by_name["council"].importable == "from ictus.stdlib import council"
    assert by_name["voice"].importable == "from ictus.stdlib.llm import voice"


def test_every_entry_carries_a_summary_and_its_parameters() -> None:
    bare = [e.name for e in catalogue() if e.summary == "(undocumented)" or not e.parameters]
    assert not bare, f"catalogued with nothing useful to say: {bare}"


def test_a_summary_is_the_first_line_of_the_real_docstring() -> None:
    found = next(e for e in catalogue() if e.name == "succeed")
    first = (inspect.getdoc(stdlib.succeed) or "").splitlines()[0]
    assert found.summary == first


def test_the_groups_are_listed_in_the_order_a_pipeline_is_built() -> None:
    assert [g.name for g in groups()] == ["gates", "llm", "steps", "exits", "stages", "scopes"]


def test_the_spec_types_are_listed_with_the_things_that_take_them() -> None:
    """`Voice` without `council` is unusable, so a catalogue omitting it misleads."""
    scopes = next(g for g in groups() if g.name == "scopes")
    assert {"Voice", "Speaker", "Attempt"} <= set(scopes.types)


# --- finding one --------------------------------------------------------------


def test_the_search_covers_what_a_thing_does_not_only_its_name() -> None:
    """Somebody searching "approve" does not know the word "gate" yet."""
    assert "approval_gate" in {e.name for e in find("approve")}


def test_the_search_is_case_insensitive() -> None:
    assert find("COUNCIL") == find("council")


def test_an_empty_search_is_the_whole_catalogue() -> None:
    assert find("   ") == catalogue()


# --- the command --------------------------------------------------------------


def test_it_lists_every_group_and_every_constructor() -> None:
    out = _invoke()
    for group in groups():
        assert group.name in out
        for entry in group.entries:
            assert entry.name in out


def test_it_names_the_outcomes_a_scope_is_routed_on() -> None:
    """You cannot write `branch_on_outcome` without them."""
    out = _invoke()
    for outcome in OUTCOMES:
        assert outcome in out


def test_one_hit_is_answered_in_full_without_being_asked() -> None:
    """Printing a line they already knew is not an answer to naming a thing."""
    out = _invoke("try_shell")
    assert "from ictus.stdlib import try_shell" in out
    assert "takes:" in out


def test_a_term_matching_nothing_says_where_to_look() -> None:
    result = runner.invoke(app, ["stdlib", "wobble"])
    assert result.exit_code == 1
    assert "STDLIB.md" in result.output


@pytest.mark.parametrize("name", ["council", "try_shell", "approval_gate", "query"])
def test_what_it_prints_is_importable(name: str) -> None:
    """The paste-able line has to actually work, or it is worse than nothing."""
    entry = next(e for e in catalogue() if e.name == name)
    module, _, symbol = entry.importable.removeprefix("from ").partition(" import ")
    assert hasattr(__import__(module, fromlist=[symbol]), symbol)
