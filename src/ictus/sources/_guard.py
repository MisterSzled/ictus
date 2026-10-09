"""The statement guard, as source text every engine's program embeds.

One copy, shared: the cases are the same whatever answers — a second statement
behind a comment, a data-modifying CTE opening with ``WITH``, a keyword inside
a string literal.

The third and weakest layer of read-only. It catches a mistake, not somebody
determined; that is a user with no write grants.
"""

from __future__ import annotations

__all__ = ["GUARD_SOURCE"]

#: Embedded verbatim into each engine's ``python3 -c`` program. Standard
#: library only, and no ``{}``: the text is substituted into a template.
GUARD_SOURCE = r'''
WRITES = (
    "insert", "update", "delete", "merge", "upsert", "replace",
    "drop", "alter", "create", "truncate", "rename", "comment",
    "grant", "revoke", "vacuum", "analyze", "reindex", "cluster",
    "copy", "call", "do", "execute", "prepare", "lock", "listen",
    "notify", "set", "reset", "begin", "start", "commit", "rollback",
    "savepoint", "attach", "detach", "pragma", "load_extension",
    # `SELECT ... INTO t` creates a table and `INTO OUTFILE` writes a file,
    # both opening with a read.
    "into", "outfile", "dumpfile",
)
OPENERS = ("select", "with", "table", "values", "show", "explain")


def _bare(sql):
    """``sql`` with comments and string literals replaced by spaces.

    Both are places a keyword can hide. Replacing rather than deleting keeps
    every offset the same length, so a statement split afterwards still lines
    up with the text that was actually sent.
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            depth = 1
            out.append("  ")
            i += 2
            while i < n and depth:
                if sql[i : i + 2] == "/*":
                    depth += 1
                    out.append("  ")
                    i += 2
                elif sql[i : i + 2] == "*/":
                    depth -= 1
                    out.append("  ")
                    i += 2
                else:
                    out.append(" " if sql[i] != "\n" else "\n")
                    i += 1
        elif ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == quote and sql[i : i + 2] == quote * 2:
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append(" " if sql[i] != "\n" else "\n")
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _words(text):
    word = []
    for ch in text.lower():
        if ch.isalnum() or ch == "_":
            word.append(ch)
        elif word:
            yield "".join(word)
            word = []
    if word:
        yield "".join(word)


def refuse(sql):
    """Why ``sql`` is not a single read, or "" when it is."""
    bare = _bare(sql)
    statements = [part for part in bare.split(";") if part.strip()]
    if not statements:
        return "there is no statement to run"
    if len(statements) > 1:
        return (
            "this is "
            + str(len(statements))
            + " statements and a read-only query runs one; send them one at a time"
        )
    words = list(_words(statements[0]))
    if not words:
        return "there is no statement to run"
    if words[0] not in OPENERS:
        return "a read-only query starts with " + ", ".join(OPENERS) + ", not " + words[0]
    hit = [word for word in words if word in WRITES]
    if hit:
        # `WITH t AS (DELETE ... RETURNING *) SELECT * FROM t` opens with
        # `with` and writes, so the whole statement is scanned.
        return "it names " + ", ".join(sorted(set(hit))) + ", which a read-only query may not do"
    return ""
'''
