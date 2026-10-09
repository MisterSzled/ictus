"""Finding the adapters installed here, through packaging entry points.

The claim ``notify`` and ``sources`` have always made is that a second
destination is a new module and nothing else moves. That was true inside this
repository and false outside it: a third party with a perfectly good adapter had
no way to make ictus aware of it except a pull request.

Two entry-point groups close that. Anything declaring one is found here, and
ictus's own adapters declare theirs the same way — in ``pyproject.toml``,
alongside everybody else's. Dogfooding the mechanism is the point: a registry
whose only real users are third parties is a registry nobody notices breaking.

**Nothing is imported to list it.** ``importlib.metadata`` reads what the
installer wrote, so ``ictus adapters`` costs no imports and a broken third-party
package cannot stop the listing. ``Adapter.load`` is where the import happens,
and it happens when somebody asks for that one.

A pipeline still writes ``from ictus.notify.slack import slack_channel``. This
is discovery, not a loader: an adapter you did not know existed is the problem
being solved, and an import you cannot read in the file is not the solution.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "NOTIFY_GROUP",
    "SOURCE_GROUP",
    "Adapter",
    "AdapterProblem",
    "installed",
    "problems",
]

#: Somewhere a run reports to.
NOTIFY_GROUP = "ictus.notify"

#: Somewhere a run reads from.
SOURCE_GROUP = "ictus.sources"

#: What each group's constructors must be annotated as returning. Checked rather
#: than assumed: the two groups take arguments that mean different things, and an
#: ``Integration`` registered as a source would be refused by ``query`` only once
#: somebody had written a pipeline around it.
RETURNS = {NOTIFY_GROUP: "Integration", SOURCE_GROUP: "Datasource"}


@dataclass(frozen=True, slots=True)
class Adapter:
    """One installed adapter, named but not yet imported."""

    name: str
    """What to import — the constructor's own name, e.g. ``slack_channel``."""

    group: str
    module: str
    """Where it lives, so the listing can print a line somebody can paste."""

    distribution: str
    """Which installed package provides it. The answer to "where did this come
    from", which for a third-party adapter is the only way to find its docs."""

    @property
    def kind(self) -> str:
        """``notify`` or ``sources``, for a listing that mixes both."""
        return self.group.removeprefix("ictus.")

    @property
    def importable(self) -> str:
        """The line to paste into a pipeline."""
        return f"from {self.module} import {self.name}"

    def load(self) -> object:
        """Import it, and hand back whatever the entry point resolved to.

        ``object`` rather than a callable type, because that is the honest
        answer: an entry point can point at anything, and the whole job of
        ``problems`` is to say when it pointed at the wrong thing. Typing this
        as callable would make the check below unreachable, which is how a
        registry comes to trust what it was told.
        """
        found = _entry_point(self.group, self.name)
        if found is None:  # pragma: no cover - it was there a moment ago
            raise LookupError(f"{self.name!r} is no longer registered under {self.group!r}")
        return found.load()


@dataclass(frozen=True, slots=True)
class AdapterProblem:
    """An entry point that claims to be an adapter and is not one."""

    name: str
    group: str
    detail: str
    """Phrased for whoever installed the package, not for a log."""


def _points(group: str) -> Iterator[EntryPoint]:
    yield from entry_points(group=group)


def _entry_point(group: str, name: str) -> EntryPoint | None:
    return next((p for p in _points(group) if p.name == name), None)


def _distribution_of(point: EntryPoint) -> str:
    """Which package declared it, or "" when the metadata does not say."""
    dist = getattr(point, "dist", None)
    return getattr(dist, "name", "") or ""


def installed(kind: str | None = None) -> list[Adapter]:
    """Every registered adapter, sorted. ``kind`` is ``notify`` or ``sources``.

    Imports nothing. An entry point whose target cannot be resolved is still
    listed — knowing a broken adapter is installed is more useful than a listing
    that silently omits the one you are looking for. ``problems`` says which.
    """
    groups = (NOTIFY_GROUP, SOURCE_GROUP) if kind is None else (f"ictus.{kind}",)
    found = [
        Adapter(
            name=point.name,
            group=group,
            module=point.value.partition(":")[0],
            distribution=_distribution_of(point),
        )
        for group in groups
        for point in _points(group)
    ]
    return sorted(found, key=lambda a: (a.group, a.name))


def problems() -> list[AdapterProblem]:
    """Registered adapters that will not do what their group promises.

    Imports each one, so this is the expensive half and is deliberately not run
    by ``installed``. ``ictus preflight`` and ``ictus adapters --check`` are
    where it belongs: the cost buys an answer before a pipeline is written
    against something that was never going to work.
    """
    found: list[AdapterProblem] = []
    for adapter in installed():
        try:
            loaded = adapter.load()
        # Bare `Exception` on purpose: this imports somebody else's module, and
        # every way that can go wrong is a fact to report rather than a crash in
        # the middle of listing what is installed.
        except Exception as exc:
            found.append(
                AdapterProblem(
                    adapter.name,
                    adapter.group,
                    f"{adapter.module} could not be imported: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if not callable(loaded):
            found.append(
                AdapterProblem(adapter.name, adapter.group, "is not callable, so it builds nothing")
            )
            continue
        wanted = RETURNS[adapter.group]
        annotation = getattr(loaded, "__annotations__", {}).get("return")
        if annotation is not None and _named(annotation) != wanted:
            found.append(
                AdapterProblem(
                    adapter.name,
                    adapter.group,
                    f"returns {_named(annotation)}, and {adapter.group!r} is for "
                    f"constructors returning {wanted}",
                )
            )
    return found


def _named(annotation: object) -> str:
    """An annotation's name, whether it is a class or a string.

    ``from __future__ import annotations`` makes every one of these a string, so
    both forms have to be handled and neither can be evaluated: evaluating a
    third party's annotation runs their code at listing time.
    """
    if isinstance(annotation, str):
        return annotation.rsplit(".", 1)[-1].strip()
    return getattr(annotation, "__name__", str(annotation))
