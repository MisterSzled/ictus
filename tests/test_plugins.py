"""Adapters found through entry points, including ones ictus did not write.

The claim being tested is the one `notify` and `sources` have always made in
prose: a second destination is a new module and nothing else moves. Inside this
repository that was true. Outside it, it was not — there was no way to tell
ictus an adapter existed. So the test that matters here builds a package ictus
has never heard of, installs it, and asks whether it shows up.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from ictus.cli import app
from ictus.plugins import NOTIFY_GROUP, SOURCE_GROUP, installed, problems

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()

BUILT_IN_NOTIFY = {"slack_channel", "slack_webhook", "jira_cloud"}
BUILT_IN_SOURCES = {
    "readonly_sqlite",
    "readonly_postgres",
    "readonly_postgres_fleet",
    "readonly_jira",
}


# --- what ships -----------------------------------------------------------------


def test_every_built_in_adapter_registers_itself() -> None:
    """ictus declares its own through the public mechanism, not a hard-coded list."""
    found = {a.name for a in installed()}
    assert found >= BUILT_IN_NOTIFY
    assert found >= BUILT_IN_SOURCES


def test_the_two_groups_are_kept_apart() -> None:
    """An `Integration` registered as a source is refused by `query`, far too late."""
    assert {a.name for a in installed("notify")} >= BUILT_IN_NOTIFY
    assert {a.name for a in installed("notify")} & BUILT_IN_SOURCES == set()


def test_each_one_says_where_it_came_from_and_how_to_import_it() -> None:
    channel = next(a for a in installed() if a.name == "slack_channel")
    assert channel.importable == "from ictus.notify.slack import slack_channel"
    assert channel.distribution == "ictus"
    assert channel.group == NOTIFY_GROUP
    assert channel.kind == "notify"


def test_listing_imports_nothing(tmp_path: Path) -> None:
    """A broken third-party adapter must not stop you seeing what is installed.

    Run in a subprocess because the test session has already imported most of
    ictus; the question is what a fresh interpreter pulls in.
    """
    script = textwrap.dedent("""
        import sys
        from ictus.plugins import installed
        assert installed()
        leaked = [m for m in sys.modules if m.startswith(("ictus.notify.", "ictus.sources."))]
        print(repr(leaked))
    """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=tmp_path
    )
    assert done.stdout.strip() == "[]", f"listing imported adapters: {done.stdout}"


def test_everything_registered_actually_works() -> None:
    assert problems() == []


# --- one ictus has never heard of ------------------------------------------------


@pytest.fixture
def third_party(tmp_path: Path) -> Iterator[str]:
    """Build and install a package providing an adapter, then remove it.

    The whole point of the registry, exercised the only way it can be: by a
    distribution that is not this one.
    """
    pkg = tmp_path / "ictus_carrier"
    pkg.mkdir()
    (pkg / "ictus_carrier.py").write_text(
        textwrap.dedent('''
        """A destination ictus has never heard of."""
        from __future__ import annotations
        from ictus.graph.requirements import EnvVar, Integration

        def carrier_pigeon(*, loft: EnvVar) -> Integration:
            """Release a bird."""
            return Integration(
                name="pigeon", purpose="carry a report", env=(loft,), program="print('{}')"
            )
        '''),
        encoding="utf-8",
    )
    (pkg / "pyproject.toml").write_text(
        textwrap.dedent("""
        [project]
        name = "ictus-carrier"
        version = "1.0"
        [project.entry-points."ictus.notify"]
        carrier_pigeon = "ictus_carrier:carrier_pigeon"
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [tool.hatch.build.targets.wheel]
        packages = ["ictus_carrier.py"]
        """),
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--no-deps", str(pkg)],
        check=True,
        capture_output=True,
    )
    try:
        yield "carrier_pigeon"
    finally:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-qy", "ictus-carrier"],
            check=False,
            capture_output=True,
        )


def test_an_adapter_from_another_package_is_found(third_party: str) -> None:
    """Nothing in this repository mentions it. That is the test."""
    found = {a.name: a for a in installed()}
    assert third_party in found
    assert found[third_party].distribution == "ictus-carrier"
    assert found[third_party].importable == "from ictus_carrier import carrier_pigeon"


def test_a_third_party_adapter_builds_a_real_integration(third_party: str) -> None:
    from ictus.graph.requirements import EnvVar, Integration

    build = next(a for a in installed() if a.name == third_party).load()
    assert callable(build)
    assert isinstance(build(loft=EnvVar("LOFT", "where the birds live")), Integration)


def test_it_is_listed_by_the_command(third_party: str) -> None:
    result = runner.invoke(app, ["adapters"])
    assert result.exit_code == 0, result.output
    assert third_party in result.output
    assert "ictus-carrier" in result.output


def test_check_passes_with_a_sound_third_party_installed(third_party: str) -> None:
    assert [p.name for p in problems() if p.name == third_party] == []


# --- the command -----------------------------------------------------------------


def test_it_lists_both_kinds() -> None:
    result = runner.invoke(app, ["adapters"])
    assert result.exit_code == 0, result.output
    for name in BUILT_IN_NOTIFY | BUILT_IN_SOURCES:
        assert name in result.output


def test_a_kind_can_be_asked_for_alone() -> None:
    result = runner.invoke(app, ["adapters", "--kind", "sources"])
    assert result.exit_code == 0, result.output
    assert "readonly_sqlite" in result.output
    assert "slack_channel" not in result.output


def test_an_unknown_kind_is_refused() -> None:
    result = runner.invoke(app, ["adapters", "--kind", "wobble"])
    assert result.exit_code == 1
    assert "notify" in result.output


def test_check_reports_that_everything_imports() -> None:
    result = runner.invoke(app, ["adapters", "--check"])
    assert result.exit_code == 0, result.output
    assert "import and look right" in result.output


# --- the group a registration belongs to -----------------------------------------


def test_the_groups_are_named_in_the_packaging_metadata() -> None:
    """The entry-point group names are the public API; a typo is a silent no-op."""
    declared = json.loads(
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,tomllib,pathlib;"
                "print(json.dumps(tomllib.loads("
                "pathlib.Path('pyproject.toml').read_text())"
                "['project']['entry-points']))",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
        ).stdout
    )
    assert NOTIFY_GROUP in declared
    assert SOURCE_GROUP in declared
