"""Reading from Postgres, through ``psql``.

``psql`` rather than a driver because the program is standard library only, the
way every emitted program here is: a workflow that needed ``psycopg`` installed
wherever it runs would be a dependency the YAML cannot declare and preflight
cannot check. ``psql`` is a command, and a command is something a pipeline can
require and preflight can refuse on.

Read-only is set on the session as well as checked on the statement.
``default_transaction_read_only`` makes the server itself refuse a write, so a
statement that gets past the guard still does not land. Neither is a substitute
for connecting as a user with no write grants, which is the only layer that
survives somebody trying.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from ictus.graph.requirements import Datasource, Executable
from ictus.sources._guard import GUARD_SOURCE

if TYPE_CHECKING:
    from ictus.graph.requirements import EnvVar

__all__ = ["readonly_postgres"]

PSQL = "psql"

SETUP_HINT = (
    "Create a database user with SELECT and nothing else, and put its connection "
    "string in the variable. The session is opened read-only and the statement is "
    "checked, but a user without write grants is the only layer that holds against "
    "somebody trying rather than somebody slipping"
)


def readonly_postgres(
    *,
    dsn: EnvVar,
    name: str = "postgres",
    purpose: str = "Answer read-only questions about the database",
    setup_hint: str = SETUP_HINT,
) -> Datasource:
    """A Postgres connection that may be read and not written.

    ``dsn`` names the variable holding the connection string — never the string
    itself. It carries a password, so it is read at run time and never written
    into a pipeline or an emitted workflow.
    """
    return Datasource(
        name=name,
        purpose=purpose,
        env=(dsn,),
        read_only=True,
        command="python3",
        program=_program(dsn=dsn.name),
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


def _program(*, dsn: str) -> str:
    """The querying program, with this source's variable name baked in."""
    return _PROGRAM.substitute(dsn_name=repr(dsn), guard=GUARD_SOURCE, psql=repr(PSQL))


_PROGRAM = string.Template(
    r"""import csv, io, json, os, subprocess, sys

DSN_NAME = ${dsn_name}
PSQL = ${psql}
${guard}

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 200
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 30
    sql = sys.stdin.read().strip()
    dsn = os.environ.get(DSN_NAME, "")
    if not dsn:
        return fail("$$" + DSN_NAME + " is not set, so there was nothing to connect to")
    why = refuse(sql)
    if why:
        return fail("refused: " + why)
    env = dict(os.environ)
    # The server's own refusal, in front of the guard's. A statement that gets
    # past the text check still cannot write through this session.
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
    report(clipped, len(rows), truncated=len(rows) > limit)


def report(rows, total, truncated):
    print(
        json.dumps(
            {
                "rows": json.dumps(rows, default=str),
                "count": str(total),
                "ran": "true",
                "why": "showing the first " + str(len(rows)) + " of " + str(total)
                if truncated
                else "",
            }
        )
    )


def fail(why):
    # Never a non-zero exit. A query that could not run is a fact for the next
    # step to read, not a reason to end a run whose work so far succeeded; the
    # reason goes to stderr, which the dashboard and `ictus trace` both show.
    print(why, file=sys.stderr)
    print(json.dumps({"rows": "[]", "count": "0", "ran": "false", "why": why}))


main()
"""
)
