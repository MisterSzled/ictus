"""Where a pipeline reads data from.

The source boundary, mirroring ``ictus.notify``: nothing above here knows any
engine exists.

Read-only is the point of the package. A constructor that cannot promise it
does not set ``read_only``, and ``query`` refuses to be built against one.

Three layers, and only the first is a guarantee:

1. **The credential.** A database user with no write grants. Nothing here can
   create that or substitute for it.
2. **The session**, opened read-only, so the server refuses a write first.
3. **The statement**, checked before it is sent, with comments stripped.

One module per engine, imported by its own name::

    from ictus.sources.sqlite import readonly_sqlite
    from ictus.sources.jira import readonly_jira

Nothing is re-exported here, and that is the rule rather than an omission. This
file and ``ictus/notify/__init__.py`` are the two boundary packages, and a flat
re-export would put every engine's spelling into them — the one thing they exist
to keep out. It also makes the import site say *which* engine a pipeline reads
from, where a flat name only said that it reads from one.

``ictus adapters`` lists what is installed, built in and third-party alike.
"""

from __future__ import annotations

__all__: list[str] = []
