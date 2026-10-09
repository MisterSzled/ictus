"""Reading from a database, and the three layers that keep it to reading."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from ictus import Datasource, EnvVar, InputPort, Pipeline, PortType, tpl
from ictus.errors import CompositionError
from ictus.lint import lint_pipeline
from ictus.sources.fleet import readonly_postgres_fleet
from ictus.sources.postgres import readonly_postgres
from ictus.sources.sqlite import readonly_sqlite
from ictus.stdlib import query, succeed

if TYPE_CHECKING:
    from pathlib import Path

STR = PortType.STRING


def _guard() -> object:
    """The statement guard, as the emitted program would run it."""
    namespace: dict[str, object] = {}
    source = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    # Everything above `def main` — the guard and nothing that would run.
    exec(source[: source.index("def main(")], namespace)
    return namespace["refuse"]


REFUSE = _guard()


# --- the statement, which is the weakest of the three layers -----------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select name from staff where id = 4",
        "WITH recent AS (SELECT * FROM shifts) SELECT count(*) FROM recent",
        "  SELECT 1  ;  ",
        "SELECT 'delete me'",
        'SELECT "update", id FROM t',
        "SELECT 1 -- ; DROP TABLE staff",
        "TABLE staff",
        "VALUES (1), (2)",
    ],
)
def test_a_read_is_allowed(sql: str) -> None:
    assert REFUSE(sql) == "", sql  # type: ignore[operator]


@pytest.mark.parametrize(
    ("sql", "because"),
    [
        ("DELETE FROM timesheets", "does not open with a read"),
        ("UPDATE staff SET name = 'x'", "does not open with a read"),
        ("DROP TABLE staff", "does not open with a read"),
        ("SELECT 1; DROP TABLE staff", "two statements"),
        ("SELECT 1 /* ; */ ; DROP TABLE staff", "a block comment hides neither"),
        (
            "WITH gone AS (DELETE FROM timesheets RETURNING *) SELECT * FROM gone",
            "a data-modifying CTE opens with WITH and still writes",
        ),
        ("BEGIN; DELETE FROM t; COMMIT", "a transaction is still not a read"),
        ("", "there is nothing to run"),
        ("   \n  ", "there is nothing to run"),
        ("SELECT 1 INTO OUTFILE '/tmp/x'", "writing somewhere is writing"),
        ("COPY staff TO '/tmp/x'", "does not open with a read"),
    ],
)
def test_a_write_is_refused(sql: str, because: str) -> None:
    assert REFUSE(sql), because  # type: ignore[operator]


def test_the_refusal_says_what_it_objected_to() -> None:
    """Whoever reads it next is a model or a person, and both need the reason."""
    assert "delete" in REFUSE("WITH g AS (DELETE FROM t RETURNING *) SELECT * FROM g")  # type: ignore[operator]
    assert "2 statements" in REFUSE("SELECT 1; SELECT 2")  # type: ignore[operator]


# --- the whole program, against a real database -------------------------------


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "timesheets.db"
    link = sqlite3.connect(path)
    link.execute("CREATE TABLE timesheets (id INTEGER PRIMARY KEY, who TEXT, client TEXT)")
    link.executemany(
        "INSERT INTO timesheets (who, client) VALUES (?, ?)",
        [("ana", "NHS Digital"), ("bo", "NHS Digital"), ("ana", "NHS Digital")],
    )
    link.commit()
    link.close()
    return path


def _run(program: str, sql: str, path: Path, *, limit: int = 200) -> dict[str, str]:
    done = subprocess.run(
        [sys.executable, "-c", program, str(limit), "5"],
        input=sql,
        capture_output=True,
        text=True,
        env={"DB": str(path), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert done.returncode == 0, "a query that cannot run is not a run that cannot finish"
    answer: dict[str, str] = json.loads(done.stdout)
    return answer


def test_a_query_answers_with_its_rows(tmp_path: Path) -> None:
    path = _database(tmp_path)
    program = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    answer = _run(program, "SELECT who, client FROM timesheets ORDER BY id", path)
    assert answer["ran"] == "true"
    assert answer["count"] == "3"
    assert json.loads(answer["rows"])[0] == {"who": "ana", "client": "NHS Digital"}


def test_a_write_reaches_the_database_as_nothing_at_all(tmp_path: Path) -> None:
    """The row count after is the assertion. Everything else is a claim."""
    path = _database(tmp_path)
    program = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    answer = _run(program, "DELETE FROM timesheets", path)
    assert answer["ran"] == "false"
    assert "refused" in answer["why"]
    link = sqlite3.connect(path)
    assert link.execute("SELECT count(*) FROM timesheets").fetchone()[0] == 3
    link.close()


def test_the_connection_refuses_a_write_the_guard_let_through(tmp_path: Path) -> None:
    """The second layer, on its own. If the text check were removed tomorrow,
    the database still would not be written to through this source."""
    path = _database(tmp_path)
    program = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    # Reach past the guard the way a bug would: run the program's own open.
    naked = program.replace(
        "    why = refuse(sql)\n    if why:\n        return fail",
        "    if False:\n        return fail",
    )
    answer = _run(naked, "DELETE FROM timesheets", path)
    assert answer["ran"] == "false"
    assert "readonly" in answer["why"].lower() or "read-only" in answer["why"].lower()
    link = sqlite3.connect(path)
    assert link.execute("SELECT count(*) FROM timesheets").fetchone()[0] == 3
    link.close()


def test_more_rows_than_asked_for_are_clipped_and_said_to_be(tmp_path: Path) -> None:
    """A step that silently filled a context window is harder to notice than one
    that says it showed the first two."""
    path = _database(tmp_path)
    program = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    answer = _run(program, "SELECT * FROM timesheets", path, limit=2)
    assert len(json.loads(answer["rows"])) == 2
    assert answer["count"] == "3", "the count is what came back, not what survived"
    assert "first 2 of 3" in answer["why"]


def test_a_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    program = readonly_sqlite(path=EnvVar("DB", "the file", secret=False)).program
    answer = _run(program, "SELECT 1", tmp_path / "nothing.db")
    assert answer["ran"] == "false"
    assert "no database to open" in answer["why"]


# --- declaring one ------------------------------------------------------------


def test_a_query_refuses_a_source_that_does_not_promise_read_only() -> None:
    """There is no flag to override it: a step that may write is a different
    step, and spelling the difference as an argument makes it a typo away."""
    writable = Datasource(name="prod", purpose="everything", program="pass", read_only=False)
    with pytest.raises(CompositionError, match="does not promise read_only"):
        query(node_id="ask", sql="SELECT 1", against=writable)


def test_the_constructors_promise_it() -> None:
    assert readonly_sqlite(path=EnvVar("DB", "f", secret=False)).read_only
    assert readonly_postgres(dsn=EnvVar("DSN", "a read-only connection string")).read_only


def test_postgres_brings_the_command_it_needs() -> None:
    """Declared with the source, so preflight refuses a machine without it and
    nobody has to remember that one implies the other."""
    pipeline = Pipeline(pipeline_id="ask")
    pipeline.require_datasource(
        readonly_postgres(dsn=EnvVar("DSN", "a read-only connection string"))
    )
    assert "psql" in {tool.name for tool in pipeline.all_executables()}


def test_a_source_is_named_once() -> None:
    pipeline = Pipeline(pipeline_id="ask")
    pipeline.require_datasource(readonly_sqlite(path=EnvVar("DB", "f", secret=False)))
    with pytest.raises(CompositionError, match="already reads from"):
        pipeline.require_datasource(readonly_sqlite(path=EnvVar("DB", "f", secret=False)))


def test_a_connection_string_never_reaches_the_program_as_a_value() -> None:
    """The variable's name is baked in; its value is read at run time."""
    source = readonly_postgres(dsn=EnvVar("REPORTING_DSN", "a read-only connection string"))
    assert "REPORTING_DSN" in source.program
    assert "postgresql://" not in source.program


# --- a pipeline has to say what it reaches ------------------------------------


def _reading(*, declared: bool) -> Pipeline:
    """A pipeline that queries a database, having said so or not."""
    pipeline = Pipeline(pipeline_id="asks")
    sql = pipeline.declare_input("sql", STR)
    source = readonly_sqlite(path=EnvVar("DB", "the file", secret=False))
    if declared:
        pipeline.require_datasource(source)
    ran = pipeline.add(query(node_id="ran", sql=sql.ref(), against=source))
    done = pipeline.add(
        succeed(node_id="done", inputs=(InputPort("rows", STR),), reason=tpl(ran.ref("rows")))
    )
    pipeline.set_entry(ran)
    pipeline.connect(ran, "rows", done, "rows")
    return pipeline


def test_reading_a_database_without_declaring_it_is_refused() -> None:
    """Otherwise a pipeline opens a production database while preflight reports
    no requirements and passes."""
    problems = lint_pipeline(_reading(declared=False))
    assert any("uses 'sqlite', which this pipeline does not declare" in p for p in problems)


def test_declaring_it_is_what_makes_it_pass() -> None:
    assert not [p for p in lint_pipeline(_reading(declared=True)) if "does not declare" in p]


def test_declaring_it_is_what_preflight_reads() -> None:
    """The declaration is not decoration: it is the list preflight walks."""
    declared = _reading(declared=True)
    assert [source.name for source in declared.all_datasources()] == ["sqlite"]
    assert _reading(declared=False).all_datasources() == ()


# --- several environments, one declaration ------------------------------------

FLEET = {
    "atlantis": EnvVar("ATLANTIS_DSN", "read-only connection for atlantis"),
    "babylon": EnvVar("BABYLON_DSN", "read-only connection for babylon"),
    "original": EnvVar("ORIGINAL_DSN", "read-only connection for original"),
}


def _fleet_says(environment: str, sql: str, present: dict[str, str]) -> dict[str, str]:
    program = readonly_postgres_fleet(environments=FLEET).program
    done = subprocess.run(
        [sys.executable, "-c", program, "50", "5", environment],
        input=sql,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **present},
        check=False,
    )
    assert done.returncode == 0, "a query that cannot run is not a run that cannot finish"
    answer: dict[str, str] = json.loads(done.stdout)
    return answer


def test_every_environment_is_declared_so_preflight_checks_them_all() -> None:
    assert [v.name for v in readonly_postgres_fleet(environments=FLEET).env] == [
        "ATLANTIS_DSN",
        "BABYLON_DSN",
        "ORIGINAL_DSN",
    ]


def test_naming_no_environments_is_refused() -> None:
    with pytest.raises(CompositionError, match="names no environments"):
        readonly_postgres_fleet(environments={})


def test_an_environment_nobody_declared_is_refused_with_the_list() -> None:
    """The name arrives in text somebody else wrote, so the lookup is a table of
    declared names and never a pattern — otherwise a ticket naming
    `prod_primary` would resolve against whatever happened to be exported."""
    answer = _fleet_says("prod_primary", "SELECT 1", {"ATLANTIS_DSN": "x"})
    assert answer["ran"] == "false"
    assert "is not an environment this reads" in answer["why"]
    assert "atlantis, babylon, original" in answer["why"]


def test_naming_nothing_is_refused_too() -> None:
    assert "no environment was named" in _fleet_says("", "SELECT 1", {})["why"]


def test_a_declared_environment_with_no_connection_says_which_variable() -> None:
    answer = _fleet_says("babylon", "SELECT 1", {"ATLANTIS_DSN": "x"})
    assert "$BABYLON_DSN is not set" in answer["why"]


def test_read_only_holds_across_every_environment() -> None:
    """More environments is more credentials, not more capability."""
    for where in FLEET:
        answer = _fleet_says(where, "DELETE FROM timesheets", {f"{where.upper()}_DSN": "x"})
        assert answer["ran"] == "false"
        assert "refused" in answer["why"]


def test_the_environment_travels_as_data_on_the_step() -> None:
    """A ticket says where a change is going, so the choice is a value."""
    step = query(
        node_id="ask",
        sql="SELECT 1",
        against=readonly_postgres_fleet(environments=FLEET),
        environment="atlantis",
    )
    assert "atlantis" in step.args


def test_a_fleet_still_promises_read_only_to_the_step() -> None:
    assert readonly_postgres_fleet(environments=FLEET).read_only
