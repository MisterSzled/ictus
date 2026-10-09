"""One source over several environments of the same shape.

Which environment a change lands in is data; the set of them is not. The
pipeline names every one it may reach, each with its own connection string,
and preflight checks all of them.

The map is baked into the program at composition. The name arrives in text
somebody else wrote, so the lookup is a table of declared names, never
``f"{name}_DSN"``.

Read-only is unchanged by having several: more environments is more
credentials, not more capability.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from ictus.errors import CompositionError
from ictus.graph.requirements import Datasource, Executable
from ictus.sources._guard import GUARD_SOURCE
from ictus.sources.postgres import PSQL, SETUP_HINT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ictus.graph.requirements import EnvVar

__all__ = ["readonly_postgres_fleet"]


def readonly_postgres_fleet(
    *,
    environments: Mapping[str, EnvVar],
    name: str = "warehouses",
    purpose: str = "Answer read-only questions about one of several environments",
    setup_hint: str = SETUP_HINT,
) -> Datasource:
    """Several Postgres environments, each read-only, chosen at run time.

    ``environments`` maps the name a ticket would use to the variable holding
    that environment's connection string. Both halves matter: the name is what
    a step asks for, and the variable is what preflight checks and what the
    environment filter lets a run see.
    """
    if not environments:
        raise CompositionError(
            f"datasource {name!r} names no environments, so there would be nothing to "
            "read from. Give it at least one"
        )
    blank = sorted(key for key in environments if not key.strip())
    if blank:
        raise CompositionError(f"datasource {name!r} has an environment with no name")
    return Datasource(
        name=name,
        purpose=purpose,
        env=tuple(environments[key] for key in sorted(environments)),
        read_only=True,
        command="python3",
        program=_program({key: environments[key].name for key in sorted(environments)}),
        needs=(
            Executable(
                name=PSQL,
                purpose=f"Runs the statements {name!r} is asked",
                probe=("--version",),
                setup_hint="Install the PostgreSQL client tools (libpq / postgresql-client)",
            ),
        ),
        setup_hint=setup_hint,
    )


def _program(wheres: Mapping[str, str]) -> str:
    """The querying program, with the declared environments baked in."""
    return _PROGRAM.substitute(wheres=repr(dict(wheres)), guard=GUARD_SOURCE, psql=repr(PSQL))


_PROGRAM = string.Template(
    r"""import csv, io, json, os, subprocess, sys

WHERES = ${wheres}
PSQL = ${psql}
${guard}

def dsn_for(asked):
    # A table of declared names, never a pattern: the name arrives in text
    # somebody else wrote.
    wanted = (asked or "").strip().lower()
    known = ", ".join(sorted(WHERES))
    if not wanted:
        return None, "no environment was named; this reads " + known
    if wanted not in WHERES:
        return None, repr(wanted) + " is not an environment this reads; it reads " + known
    name = WHERES[wanted]
    value = os.environ.get(name, "")
    if not value:
        return None, "$$" + name + " is not set, so " + wanted + " cannot be reached"
    return value, ""


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 200
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 30
    asked = sys.argv[3] if len(sys.argv) > 3 else ""
    sql = sys.stdin.read().strip()
    dsn, why = dsn_for(asked)
    if dsn is None:
        return fail(why)
    why = refuse(sql)
    if why:
        return fail("refused: " + why)
    env = dict(os.environ)
    # The server's own refusal, in front of the guard's.
    env["PGOPTIONS"] = (
        "-c default_transaction_read_only=on -c statement_timeout=" + str(seconds * 1000)
    )
    command = [PSQL, dsn, "--csv", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    try:
        done = subprocess.run(
            command,
            input=sql,
            capture_output=True,
            text=True,
            timeout=seconds + 5,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return fail(PSQL + " is not on PATH where this step ran")
    except subprocess.TimeoutExpired:
        return fail("the query did not answer within " + str(seconds) + "s")
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return fail(said[-1] if said else "the query failed")
    rows = list(csv.DictReader(io.StringIO(done.stdout)))
    clipped = rows[:limit]
    report(clipped, len(rows), asked, truncated=len(rows) > limit)


def report(rows, total, where, truncated):
    note = "read " + where
    if truncated:
        note = note + ", showing the first " + str(len(rows)) + " of " + str(total)
    print(
        json.dumps(
            {
                "rows": json.dumps(rows, default=str),
                "count": str(total),
                "ran": "true",
                "why": note,
            }
        )
    )


def fail(why):
    # Never a non-zero exit: a query that could not run is a fact for the next
    # step, not a reason to end the run.
    print(why, file=sys.stderr)
    print(json.dumps({"rows": "[]", "count": "0", "ran": "false", "why": why}))


main()
"""
)
