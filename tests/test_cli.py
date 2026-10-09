"""CLI behaviour, including the failure modes that previously exited zero."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ictus import cli
from ictus.cli import app
from ictus.interfaces.conductor import ConductorBackend
from ictus.runspec.config import MINIMAL as MINIMAL_CONFIG

runner = CliRunner()

MINIMAL = """
from ictus import AgentNode, END, OutputPort, Pipeline, PortType
demo = Pipeline(pipeline_id="{pid}")
node = demo.add(AgentNode(node_id="only", prompt="hello",
                          declared_outputs=(OutputPort("v", PortType.STRING),)))
demo.route(node, END)
"""

NEEDS_INPUT = """
from ictus import AgentNode, END, InputPort, OutputPort, Pipeline, PortType, tpl
demo = Pipeline(pipeline_id="demo")
subject = demo.declare_input("subject", PortType.STRING, prose=True)
node = demo.add(AgentNode(node_id="a", inputs=(InputPort("s", PortType.STRING),),
                          prompt=tpl("do ", subject.ref()),
                          declared_outputs=(OutputPort("v", PortType.STRING),)))
demo.set_entry(node)
demo.connect_input(subject, node, "s")
demo.route(node, END)
"""

BROKEN = """
from ictus import AgentNode, InputPort, Pipeline, PortType
demo = Pipeline(pipeline_id="broken")
demo.add(AgentNode(node_id="a", inputs=(InputPort("never_wired", PortType.STRING),),
                   prompt="x"))
"""


def _write(directory: Path, name: str, body: str, *, config: str = MINIMAL_CONFIG) -> Path:
    """Lay out one pipeline folder: pipeline.py plus the config every folder needs."""
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pipeline.py").write_text(body)
    (folder / "config.yaml").write_text(config)
    return folder


def test_emit_is_a_subcommand(tmp_path: Path) -> None:
    """`ictus emit <dir>` must parse; a one-command Typer app swallows the argument."""
    src, out = tmp_path / "pipelines", tmp_path / "out"
    _write(src, "demo", MINIMAL.format(pid="demo"))
    result = runner.invoke(app, ["emit", str(src), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "demo.yaml").is_file()


def test_emit_fails_when_nothing_was_emitted(tmp_path: Path) -> None:
    """Success on an empty run reported green while producing no artifacts."""
    src, out = tmp_path / "pipelines", tmp_path / "out"
    src.mkdir(parents=True)
    result = runner.invoke(app, ["emit", str(src), "--out", str(out)])
    assert result.exit_code == 1
    assert "no pipeline folders" in result.output


def test_emit_prunes_yaml_no_pipeline_claims(tmp_path: Path) -> None:
    src, out = tmp_path / "pipelines", tmp_path / "out"
    _write(src, "demo", MINIMAL.format(pid="demo"))
    out.mkdir(parents=True)
    stale = out / "deleted-last-week.yaml"
    stale.write_text("workflow: {}\n")
    result = runner.invoke(app, ["emit", str(src), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert not stale.exists()


def test_emit_writes_nothing_when_the_lints_fail(tmp_path: Path) -> None:
    src, out = tmp_path / "pipelines", tmp_path / "out"
    _write(src, "broken", BROKEN)
    result = runner.invoke(app, ["emit", str(src), "--out", str(out)])
    assert result.exit_code == 1
    assert "never_wired" in result.output
    assert not list(out.glob("*.yaml")) if out.exists() else True


def test_emit_refuses_two_pipelines_claiming_one_filename(tmp_path: Path) -> None:
    """Last-write-wins silently deleted a pipeline from the output directory."""
    src, out = tmp_path / "pipelines", tmp_path / "out"
    _write(src, "first", MINIMAL.format(pid="clash"))
    _write(src, "second", MINIMAL.format(pid="clash"))
    result = runner.invoke(app, ["emit", str(src), "--out", str(out)])
    assert result.exit_code == 1
    assert "claimed by both" in result.output
    assert str(out) in result.output, "say where the two would land, not just the name"


def test_emit_lets_two_folders_hold_the_same_filename(tmp_path: Path) -> None:
    """Each folder builds into its own `build/`, so the names never meet.

    Two pipelines both placing `read.yaml` is the ordinary consequence of
    reusing a stdlib stage.
    """
    src = tmp_path / "pipelines"
    _write(src, "first", MINIMAL.format(pid="same_name"))
    _write(src, "second", MINIMAL.format(pid="same_name"))
    result = runner.invoke(app, ["emit", str(src)])
    assert result.exit_code == 0, result.output
    assert "claimed by both" not in result.output
    assert (src / "first" / "build" / "same_name.yaml").is_file()
    assert (src / "second" / "build" / "same_name.yaml").is_file()


def test_lint_reports_without_writing(tmp_path: Path) -> None:
    src = tmp_path / "pipelines"
    _write(src, "broken", BROKEN)
    result = runner.invoke(app, ["lint", str(src)])
    assert result.exit_code == 1
    assert "never_wired" in result.output


def test_validate_requires_emitted_yaml(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(app, ["validate", str(out)])
    assert result.exit_code == 1
    assert "run `ictus emit` first" in result.output


def test_validate_accepts_what_emit_produced(tmp_path: Path) -> None:
    """The whole loop: author, emit, and have Conductor accept the result."""
    src, out = tmp_path / "pipelines", tmp_path / "out"
    _write(src, "demo", MINIMAL.format(pid="demo"))
    assert runner.invoke(app, ["emit", str(src), "--out", str(out)]).exit_code == 0
    result = runner.invoke(app, ["validate", str(out)])
    assert result.exit_code == 0, result.output


def test_run_refuses_a_folder_that_is_not_a_pipeline(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-pipeline"
    bare.mkdir()
    result = runner.invoke(app, ["run", str(bare)])
    assert result.exit_code == 1
    assert "pipeline.py" in result.output


def test_run_says_what_the_input_file_is_missing(tmp_path: Path) -> None:
    """A required input with nowhere to come from should name the file to put it in."""
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", NEEDS_INPUT)
    result = runner.invoke(app, ["run", str(folder), "--skip-preflight"])
    assert result.exit_code == 1
    assert "requires ['subject']" in result.output
    assert "input.md" in result.output


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record what the backend would launch, without launching it."""
    calls: list[list[str]] = []

    def fake(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("ictus.interfaces.conductor.subprocess.run", fake)
    return calls


def test_dry_run_asks_for_a_plan_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that spends nothing has no dashboard to serve and nothing to detach."""
    calls = _capture(monkeypatch)
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    result = runner.invoke(app, ["run", str(folder), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "--dry-run" in calls[0]
    assert "--web" not in calls[0]
    assert "--web-bg" not in calls[0]


def test_dry_run_does_not_demand_inputs_it_will_never_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conductor plans from the workflow file alone, so the values are not read."""
    calls = _capture(monkeypatch)
    folder = _write(tmp_path / "pipelines", "demo", NEEDS_INPUT)
    result = runner.invoke(app, ["run", str(folder), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "requires" not in result.output
    assert "--dry-run" in calls[0]


def test_dry_run_still_refuses_an_input_name_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelling is a defect whether or not the value gets spent."""
    _capture(monkeypatch)
    folder = _write(tmp_path / "pipelines", "demo", NEEDS_INPUT)
    result = runner.invoke(app, ["run", str(folder), "--dry-run", "-i", "subjekt=x"])
    assert result.exit_code == 1
    assert "subjekt" in result.output


def test_the_log_file_reaches_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-mortem needs the debug output, and only the engine can write it."""
    calls = _capture(monkeypatch)
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    result = runner.invoke(
        app, ["run", str(folder), "--skip-preflight", "--foreground", "-l", "auto"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][calls[0].index("--log-file") + 1] == "auto"


def test_no_log_file_passes_no_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    result = runner.invoke(app, ["run", str(folder), "--skip-preflight", "--foreground"])
    assert result.exit_code == 0, result.output
    assert "--log-file" not in calls[0]


def test_init_writes_the_files_a_folder_needs(tmp_path: Path) -> None:
    folder = tmp_path / "fresh"
    assert runner.invoke(app, ["init", str(folder)]).exit_code == 0
    assert (folder / "config.yaml").is_file()
    assert (folder / "input.md").is_file()
    assert (folder / "pipeline.py").is_file()


def test_init_keeps_what_is_already_there(tmp_path: Path) -> None:
    folder = tmp_path / "fresh"
    folder.mkdir()
    (folder / "config.yaml").write_text("provider: mine\n")
    runner.invoke(app, ["init", str(folder)])
    assert (folder / "config.yaml").read_text() == "provider: mine\n"


def test_validate_does_not_mistake_the_config_for_a_workflow(tmp_path: Path) -> None:
    """`config.yaml` sits in the folder; globbing *.yaml handed it to the loader."""
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"))
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    result = runner.invoke(app, ["validate", str(folder)])
    assert result.exit_code == 0, result.output
    assert "config.yaml" not in result.output


def test_a_folder_without_a_config_says_what_to_write(tmp_path: Path) -> None:
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"))
    (folder / "config.yaml").unlink()
    result = runner.invoke(app, ["emit", str(folder)])
    assert result.exit_code == 1
    assert "provider: claude-agent-sdk" in result.output


def test_lint_checks_the_graph_even_with_no_config(tmp_path: Path) -> None:
    """The one command whose job is finding graph problems reported only a file."""
    src = tmp_path / "pipelines"
    folder = _write(src, "broken", BROKEN)
    (folder / "config.yaml").unlink()
    result = runner.invoke(app, ["lint", str(folder)])
    assert result.exit_code == 1
    assert "never_wired" in result.output
    assert "does not exist" in result.output


def test_a_clean_graph_with_no_config_still_lints(tmp_path: Path) -> None:
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"))
    (folder / "config.yaml").unlink()
    result = runner.invoke(app, ["lint", str(folder)])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output


def test_a_malformed_config_still_fails_the_lint(tmp_path: Path) -> None:
    """Absent is a decision not yet made; malformed is an error to fix."""
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"), config="provider: []\n")
    result = runner.invoke(app, ["lint", str(folder)])
    assert result.exit_code == 1
    assert "needs a `provider`" in result.output


def test_the_start_gate_is_on_by_default(tmp_path: Path) -> None:
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"))
    runner.invoke(app, ["emit", str(folder)])
    emitted = (folder / "build" / "demo.yaml").read_text()
    assert "entry_point: confirm_start" in emitted


def test_a_pipeline_can_turn_the_start_gate_off(tmp_path: Path) -> None:
    src = tmp_path / "pipelines"
    folder = _write(
        src,
        "demo",
        MINIMAL.format(pid="demo"),
        config="provider: claude-agent-sdk\nstart_gate: false\n",
    )
    runner.invoke(app, ["emit", str(folder)])
    emitted = (folder / "build" / "demo.yaml").read_text()
    assert "confirm_start" not in emitted


def test_a_misspelled_provider_is_caught_where_it_is_written(tmp_path: Path) -> None:
    """Otherwise it surfaces from Conductor's loader, after the whole thing compiles."""
    src = tmp_path / "pipelines"
    folder = _write(src, "demo", MINIMAL.format(pid="demo"), config="provider: claud\n")
    result = runner.invoke(app, ["lint", str(folder)])
    assert result.exit_code == 1
    assert "is not a provider" in result.output
    assert "claude-agent-sdk" in result.output


LOADS_TWICE = """
from pathlib import Path
from ictus import AgentNode, END, OutputPort, Pipeline, PortType
marker = Path(__file__).with_name("loads.txt")
seen = len(marker.read_text()) if marker.exists() else 0
marker.write_text("x" * (seen + 1))
demo = Pipeline(pipeline_id="demo")
node = demo.add(AgentNode(node_id="only" if seen == 0 else "reloaded", prompt="hello",
                          declared_outputs=(OutputPort("v", PortType.STRING),)))
demo.route(node, END)
"""


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Intercept the launch, so a CLI test never starts a real Conductor process.

    `conductor` is on PATH here, so without this a run-path test spends money.
    """
    calls: list[dict[str, object]] = []

    def _fake(_self: ConductorBackend, path: Path, **kwargs: object) -> int:
        calls.append({"path": path, **kwargs})
        return 0

    monkeypatch.setattr(ConductorBackend, "run", _fake)
    return calls


def test_run_lints_before_it_writes_or_launches(
    tmp_path: Path, launched: list[dict[str, object]]
) -> None:
    """`run` consulted no lint, and `--reemit` is on: it compiled and launched anyway."""
    folder = _write(tmp_path / "pipelines", "broken", BROKEN)
    result = runner.invoke(app, ["run", str(folder), "--skip-preflight"])
    assert result.exit_code == 1
    assert "never_wired" in result.output
    assert not (folder / "build").exists()
    assert not launched


def test_run_does_not_overwrite_an_artifact_it_cannot_check(
    tmp_path: Path, launched: list[dict[str, object]]
) -> None:
    """build/ is committed; a run that cannot lint the source must not rewrite it."""
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    committed = (folder / "build" / "demo.yaml").read_text()
    (folder / "pipeline.py").write_text(BROKEN)

    result = runner.invoke(app, ["run", str(folder), "--skip-preflight"])
    assert result.exit_code == 1
    assert (folder / "build" / "demo.yaml").read_text() == committed
    assert not launched


def test_emit_writes_the_graph_it_checked(tmp_path: Path) -> None:
    """emit linted the pipelines it loaded, then wrote pipelines it loaded again."""
    folder = _write(tmp_path / "pipelines", "demo", LOADS_TWICE)
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    assert "reloaded" not in (folder / "build" / "demo.yaml").read_text()
    assert (folder / "loads.txt").read_text() == "x"


def test_emit_says_whether_the_committed_bytes_moved(tmp_path: Path) -> None:
    """ "emitted" for an identical recompile hides the writes that are real changes."""
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    first = runner.invoke(app, ["emit", str(folder)])
    assert first.exit_code == 0, first.output
    assert "wrote" in first.output

    second = runner.invoke(app, ["emit", str(folder)])
    assert second.exit_code == 0, second.output
    assert "same" in second.output
    assert "updated" not in second.output


def test_reemit_reports_what_it_changed_and_prunes(
    tmp_path: Path, launched: list[dict[str, object]]
) -> None:
    """A silent reemit rewrote committed YAML and left YAML nothing claims beside it."""
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    stale = folder / "build" / "renamed-last-week.yaml"
    stale.write_text("workflow: {}\n")
    (folder / "pipeline.py").write_text(MINIMAL.format(pid="demo").replace("hello", "goodbye"))

    result = runner.invoke(app, ["run", str(folder), "--skip-preflight"])
    assert result.exit_code == 0, result.output
    assert "updated" in result.output
    assert "renamed-last-week" in result.output
    assert not stale.exists()
    assert launched


def test_a_quiet_reemit_means_the_artifact_already_matched(
    tmp_path: Path, launched: list[dict[str, object]]
) -> None:
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    result = runner.invoke(app, ["run", str(folder), "--skip-preflight"])
    assert result.exit_code == 0, result.output
    assert "updated" not in result.output
    assert "pruned" not in result.output
    assert launched


def test_background_without_a_dashboard_is_refused(
    tmp_path: Path, launched: list[dict[str, object]]
) -> None:
    """`--web-bg` is what detaches, so --no-web -b served a port and said nothing."""
    folder = _write(tmp_path / "pipelines", "demo", MINIMAL.format(pid="demo"))
    result = runner.invoke(app, ["run", str(folder), "-b", "--no-web", "--skip-preflight"])
    assert result.exit_code == 1
    assert "--background" in result.output
    assert not launched


def test_init_does_not_tell_you_to_run_the_placeholder(tmp_path: Path) -> None:
    """Following the closing line launched a real, billable run on untouched scaffold."""
    folder = tmp_path / "fresh"
    result = runner.invoke(app, ["init", str(folder)])
    assert result.exit_code == 0, result.output
    assert "ictus lint" in result.output
    assert "ictus run" not in result.output


def test_the_untouched_scaffold_does_not_lint(tmp_path: Path) -> None:
    folder = tmp_path / "fresh"
    assert runner.invoke(app, ["init", str(folder)]).exit_code == 0
    result = runner.invoke(app, ["lint", str(folder)])
    assert result.exit_code == 1
    assert "CHANGE-ME" in result.output


def test_the_scaffold_works_once_the_decisions_are_made(tmp_path: Path) -> None:
    """A scaffold that does not lint and emit after editing is a trap, not a start."""
    folder = tmp_path / "fresh"
    assert runner.invoke(app, ["init", str(folder)]).exit_code == 0
    source = folder / "pipeline.py"
    source.write_text(source.read_text().replace("CHANGE-ME", "my-pipeline"))
    assert runner.invoke(app, ["lint", str(folder)]).exit_code == 0
    assert runner.invoke(app, ["emit", str(folder)]).exit_code == 0
    assert (folder / "build" / "my-pipeline.yaml").is_file()


class TestEveryCommandIsReachable:
    """A command in the source must be a command in the program.

    The way to lose one is to add a module and not import it from
    `cli/__init__.py`. Scans every file in the package for `@app.command()`
    and asserts the running program offers each, as `__main__` in a subprocess.
    """

    def _declared(self) -> set[str]:
        package = Path(cli.__file__).parent
        return {
            line.split("def ", 1)[1].split("(", 1)[0]
            for module in sorted(package.glob("*.py"))
            for prev, line in itertools.pairwise(module.read_text(encoding="utf-8").splitlines())
            if prev.strip() == "@app.command()" and line.startswith("def ")
        }

    def test_the_scan_finds_something(self) -> None:
        """A glob that matches nothing would make every assertion below vacuous."""
        assert len(self._declared()) >= 8

    def _as_main(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ictus.cli", *args],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "COLUMNS": "200"},
        )

    def test_running_as_main_offers_every_command(self) -> None:
        listed = self._as_main("--help").stdout
        missing = sorted(name for name in self._declared() if f" {name} " not in listed)
        assert not missing, (
            f"{missing} decorated with @app.command() but absent from "
            "`python -m ictus.cli --help` — every module holding commands must be "
            "imported from cli/__init__.py"
        )

    def test_trace_is_the_one_that_caught_it(self) -> None:
        assert "trace" in self._declared()
        assert self._as_main("trace", "--help").returncode == 0
