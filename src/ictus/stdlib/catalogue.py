"""The stdlib describing itself, read off the code rather than a document.

``STDLIB.md`` is the written catalogue and only exists in a checkout. This
answers the same question from ``__all__``, ``inspect.signature`` and the
first line of each docstring, so there is nothing to keep in step.

Not imported by ``ictus.stdlib``, so it costs a pipeline nothing to exist.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ictus.stdlib import exits, gates, llm, scopes, stages, steps
from ictus.stdlib.scopes import outcomes as outcome_names

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["GROUPS", "OUTCOMES", "Entry", "Group", "catalogue", "find", "groups"]


@dataclass(frozen=True, slots=True)
class Entry:
    """One ready-made thing, and enough about it to decide whether to read on."""

    name: str
    group: str
    summary: str
    """The first line of its docstring."""

    parameters: tuple[str, ...]
    """Its keyword arguments, in declaration order, ``node_id`` and friends
    included — the answer to "what do I have to pass"."""

    doc: str
    """The whole docstring, for when the summary was not enough."""

    @property
    def importable(self) -> str:
        """The line to paste. ``llm`` is the one group not re-exported flat."""
        where = "ictus.stdlib.llm" if self.group == "llm" else "ictus.stdlib"
        return f"from {where} import {self.name}"


@dataclass(frozen=True, slots=True)
class Group:
    """A folder under ``stdlib/``, and what is in it."""

    name: str
    about: str
    entries: tuple[Entry, ...]
    types: tuple[str, ...] = ()
    """Spec types the constructors take — ``Voice``, ``ScriptStep``. You cannot
    use the thing without them, so a catalogue that omitted them would be
    describing half an interface."""

    constants: tuple[str, ...] = ()


#: In the order somebody meets them, not alphabetically: a person is asked, a
#: model is asked, something is computed, the run ends — then the two ways to
#: package all of that up.
GROUPS: tuple[tuple[str, str, ModuleType], ...] = (
    ("gates", "a run stops and waits for a person", gates),
    ("llm", "a model is asked something", llm),
    ("steps", "no model is called, and nothing is charged", steps),
    ("exits", "the run ends, distinguishably", exits),
    ("stages", "a reusable sub-graph, one iteration to its caller", stages),
    ("scopes", "a sub-graph whose every ending is a value the caller routes on", scopes),
)


def _summary(doc: str) -> str:
    """The first line of a docstring, or a stand-in saying there was none."""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first or "(undocumented)"


def _entry(name: str, group: str, thing: object) -> Entry:
    doc = inspect.getdoc(thing) or ""
    try:
        params = tuple(inspect.signature(thing).parameters)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - every stdlib entry has one
        params = ()
    return Entry(name=name, group=group, summary=_summary(doc), parameters=params, doc=doc)


def groups() -> list[Group]:
    """Every group, with what it holds. The shape the catalogue is read in."""
    found: list[Group] = []
    for name, about, module in GROUPS:
        exported = [(n, getattr(module, n)) for n in module.__all__]
        found.append(
            Group(
                name=name,
                about=about,
                entries=tuple(
                    _entry(n, name, thing)
                    for n, thing in exported
                    if n[0].islower() and callable(thing)
                ),
                types=tuple(n for n, thing in exported if inspect.isclass(thing)),
                constants=tuple(
                    n for n, thing in exported if not n[0].islower() and not inspect.isclass(thing)
                ),
            )
        )
    return found


def catalogue() -> list[Entry]:
    """Every ready-made constructor, flat."""
    return [entry for group in groups() for entry in group.entries]


def find(term: str) -> list[Entry]:
    """Everything whose name or summary mentions ``term``, case-insensitively.

    Deliberately matches the summary as well as the name. Somebody looking for
    "approval" should find ``briefing_gate``, whose name says nothing about one.
    """
    wanted = term.strip().lower()
    if not wanted:
        return catalogue()
    return [e for e in catalogue() if wanted in e.name.lower() or wanted in e.summary.lower()]


#: What a scope's ending can be, for routing one. Named here because
#: ``branch_on_outcome`` refuses to leave a member of the vocabulary unrouted,
#: so this is the list you need in front of you to write that call.
OUTCOMES: tuple[str, ...] = tuple(outcome_names.__all__)
