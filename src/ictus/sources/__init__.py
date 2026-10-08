"""Where a pipeline reads data from.

The mirror of ``ictus.notify``. That package is the audience boundary — the one
place a destination's spelling is allowed to appear. This is the *source*
boundary, and the same rule holds: nothing above here knows Postgres, or any
other engine, exists. A pipeline declares a ``Datasource`` and a step runs a
statement through it; which engine answers is decided here and nowhere else.

Read-only is the point of the package, not a setting inside it. A constructor
that cannot promise it does not set ``read_only``, and ``query`` refuses to be
built against one that did not — so "this must not write" is answered while the
pipeline is being written rather than by whatever a model generated reaching a
production table.

That promise has three layers, and only the first is a guarantee:

1. **The credential.** A database user with no write grants. Nothing in this
   package can create that, and nothing in it can substitute for it.
2. **The session.** The connection is opened read-only, so the server refuses a
   write before any statement runs.
3. **The statement.** Anything that is not a single read is refused before it
   is sent, with comments stripped first so a ``--`` cannot hide a second one.

Layers 2 and 3 are defence in depth against a mistake. Layer 1 is the defence
against an attack, and the setup hint on every constructor here says so.
"""

from __future__ import annotations

from ictus.sources.fleet import readonly_postgres_fleet
from ictus.sources.jira import readonly_jira
from ictus.sources.postgres import readonly_postgres
from ictus.sources.sqlite import readonly_sqlite

__all__ = [
    "readonly_jira",
    "readonly_postgres",
    "readonly_postgres_fleet",
    "readonly_sqlite",
]
