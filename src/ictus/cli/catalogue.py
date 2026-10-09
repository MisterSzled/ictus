"""``ictus stdlib`` and ``ictus adapters`` — what is already built here.

The two verbs that touch no pipeline. They are here because the question they
answer — "is there already a thing for this?" — is asked before there is a
pipeline to point at, and because ``STDLIB.md`` only exists in a checkout.

``stdlib`` is rendered from ``ictus.stdlib.catalogue``, which reads the code.
``adapters`` from ``ictus.plugins``, which reads the installed metadata — so it
answers for this machine, third-party packages included, rather than for this
repository. Neither has anything to keep in step with anything.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ictus.cli.app import _fail, app
from ictus.plugins import installed, problems
from ictus.stdlib.catalogue import OUTCOMES, Entry, find, groups

#: Wide enough for `script_sequence`, the longest name, plus a space.
NAME_COLUMN = 18


@app.command()
def stdlib(
    term: Annotated[
        str | None,
        typer.Argument(help="Show only what matches this, by name or by what it does"),
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Print each one's whole docstring and parameters")
    ] = False,
) -> None:
    """List the ready-made nodes, stages and scopes a pipeline can use.

    With no argument, everything, grouped the way `src/ictus/stdlib/` is. With
    one, only what matches it — the search covers what each thing *does*, not
    just its name, so `ictus stdlib approve` finds the gates.

    `STDLIB.md` carries the same catalogue with the options spelled out and the
    gotchas that each cost a real run.
    """
    if term is not None:
        _matches(term, full=full)
        return
    for group in groups():
        typer.secho(f"\n{group.name}", fg=typer.colors.CYAN, bold=True, nl=False)
        typer.secho(f"  — {group.about}", fg=typer.colors.BRIGHT_BLACK)
        for entry in group.entries:
            _line(entry)
            if full:
                _detail(entry)
        if group.types:
            # Indented shallower than a summary on purpose: aligned with the
            # summary column it reads as a note about the last constructor.
            typer.secho(
                f"   specs it takes: {', '.join(group.types)}", fg=typer.colors.BRIGHT_BLACK
            )
    typer.secho(
        f"\noutcomes to route a scope on: {', '.join(OUTCOMES)}",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.secho(
        "every constructor also takes `description`, and most take `inputs`; "
        "`ictus stdlib <name>` for one of them",
        fg=typer.colors.BRIGHT_BLACK,
    )


def _matches(term: str, *, full: bool) -> None:
    found = find(term)
    if not found:
        _fail(
            f"nothing in the stdlib matches {term!r}. `ictus stdlib` lists all of it, "
            "and STDLIB.md says what each one is for."
        )
        return
    # One hit is somebody naming the thing they meant, so answer in full whether
    # or not they asked: the alternative is printing one line they already knew.
    detail = full or len(found) == 1
    for entry in found:
        _line(entry, group=True)
        if detail:
            _detail(entry)


def _line(entry: Entry, *, group: bool = False) -> None:
    typer.secho(f"  {entry.name:<{NAME_COLUMN}}", fg=typer.colors.GREEN, bold=True, nl=False)
    typer.echo(entry.summary + (f"  [{entry.group}]" if group else ""))


def _detail(entry: Entry) -> None:
    typer.secho(f"  {'':<{NAME_COLUMN}}{entry.importable}", fg=typer.colors.BRIGHT_BLACK)
    if entry.parameters:
        typer.secho(
            f"  {'':<{NAME_COLUMN}}takes: {', '.join(entry.parameters)}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    rest = entry.doc.splitlines()[1:]
    for line in rest:
        typer.secho(f"  {'':<{NAME_COLUMN}}{line}".rstrip(), fg=typer.colors.BRIGHT_BLACK)
    typer.echo()


@app.command()
def adapters(
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Only `notify` (reported to) or `sources` (read from)"),
    ] = None,
    check: Annotated[
        bool, typer.Option("--check", help="Import each one and say which will not work")
    ] = False,
) -> None:
    """List the services this installation can report to and read from.

    Read from installed metadata, not from a list in the library, so a
    third-party adapter appears here the moment its package is installed.
    Nothing is imported to list it; `--check` imports each one and says which
    cannot be, which is the slow half and the reason it is a flag.
    """
    if kind is not None and kind not in ("notify", "sources"):
        _fail(f"--kind is `notify` or `sources`, not {kind!r}")
        return
    found = installed(kind)
    if not found:
        typer.secho(
            "no adapters are registered. ictus ships several; an empty list means "
            "it is on the path without being installed (`pip install -e .`).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    for group in ("notify", "sources"):
        here = [a for a in found if a.kind == group]
        if not here:
            continue
        typer.secho(f"\n{group}", fg=typer.colors.CYAN, bold=True, nl=False)
        typer.secho(
            "  — somewhere a run reports to"
            if group == "notify"
            else "  — somewhere a run reads from",
            fg=typer.colors.BRIGHT_BLACK,
        )
        for adapter in here:
            typer.secho(f"  {adapter.name:<{NAME_COLUMN + 6}}", fg=typer.colors.GREEN, nl=False)
            typer.echo(adapter.importable, nl=False)
            typer.secho(
                f"   ({adapter.distribution})" if adapter.distribution else "",
                fg=typer.colors.BRIGHT_BLACK,
            )
    if not check:
        typer.secho("\n`ictus adapters --check` imports each one", fg=typer.colors.BRIGHT_BLACK)
        return
    broken = problems()
    if not broken:
        typer.secho(f"\nall {len(found)} import and look right", fg=typer.colors.GREEN)
        return
    typer.secho("", err=True)
    for problem in broken:
        typer.secho(f"  {problem.name}: {problem.detail}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)
