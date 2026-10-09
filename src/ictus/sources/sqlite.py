"""Reading from a SQLite file.

No command and no driver: ``sqlite3`` is in the standard library, so this is
the one source that needs nothing installed.

Read-only twice over: a ``mode=ro`` URI plus ``query_only``. There is no user
to drop grants from, so the file's own permissions are the outer layer.
"""

from __future__ import annotations

import string

from ictus.graph.requirements import Datasource, EnvVar
from ictus.sources._guard import GUARD_SOURCE

__all__ = ["readonly_sqlite"]

SETUP_HINT = (
    "Point the variable at the database file. It is opened read-only, so the "
    "process cannot write to it through this source — but it is still a file the "
    "process can see, so its permissions are what stop anything else reaching it"
)


def readonly_sqlite(
    *,
    path: EnvVar,
    name: str = "sqlite",
    purpose: str = "Answer read-only questions about the database file",
    setup_hint: str = SETUP_HINT,
) -> Datasource:
    """A SQLite file that may be read and not written.

    ``path`` names the variable holding the file path, not the path itself:
    where the data lives is an environment's business, and a pipeline that
    hard-coded it would only run on the machine it was written on.
    """
    return Datasource(
        name=name,
        purpose=purpose,
        env=(EnvVar(path.name, path.purpose, secret=False),),
        read_only=True,
        command="python3",
        program=_program(path=path.name),
        setup_hint=setup_hint,
    )


def _program(*, path: str) -> str:
    """The querying program, with this source's variable name baked in."""
    return _PROGRAM.substitute(path_name=repr(path), guard=GUARD_SOURCE)


_PROGRAM = string.Template(
    r"""import json, os, sqlite3, sys

PATH_NAME = ${path_name}
${guard}

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 200
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 30
    sql = sys.stdin.read().strip()
    path = os.environ.get(PATH_NAME, "")
    if not path:
        return fail("$$" + PATH_NAME + " is not set, so there was no database to open")
    if not os.path.isfile(path):
        return fail(path + " is not a file, so there was no database to open")
    why = refuse(sql)
    if why:
        return fail("refused: " + why)
    uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    try:
        link = sqlite3.connect(uri, uri=True, timeout=seconds)
    except sqlite3.Error as exc:
        return fail("could not open " + path + " read-only: " + str(exc))
    try:
        link.row_factory = sqlite3.Row
        # The same refusal one layer in, so a build that ignored the URI
        # still refuses.
        link.execute("PRAGMA query_only = ON")
        found = link.execute(sql).fetchall()
    except sqlite3.Error as exc:
        return fail(str(exc))
    finally:
        link.close()
    rows = [dict(row) for row in found]
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
    # step to read, not a reason to end a run whose work so far succeeded.
    print(why, file=sys.stderr)
    print(json.dumps({"rows": "[]", "count": "0", "ran": "false", "why": why}))


main()
"""
)
