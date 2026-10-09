"""Starting a run: where the binary is, and what environment it is handed.

``launch_env`` is the one that matters most and reads least like it: a
declaration in a pipeline stops being something a reviewer reads and becomes
the environment the process actually gets. Anything undeclared is absent, which
includes the listener's own credentials.

Split out of ``test_trigger.py`` when these three moved into
``interfaces/conductor/control/launch.py``. What stayed there is recognising a
request and deciding what to do about it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ictus.interfaces.conductor.control.launch import launch_env
from ictus.runs.launch import Asked, start
from ictus.runs.triggers import Need, Trigger

if TYPE_CHECKING:
    import pytest


def _ask() -> Asked:
    return Asked(question="why's it failing?", thread="1.5", channel="C", who="U")


MACHINE = {"PATH": "/usr/bin", "HOME": "/home/someone", "TMPDIR": "/tmp"}


SECRETS = {
    "SLACK_APP_TOKEN": "xapp-opens-a-socket",
    "SLACK_BOT_TOKEN": "xoxb-posts",
    "ATLANTIS_DSN": "postgres://atlantis",
    "BABYLON_DSN": "postgres://babylon",
    "AWS_SECRET_ACCESS_KEY": "nothing-to-do-with-this",
}


def test_an_undeclared_credential_never_reaches_the_run() -> None:
    """The declaration stops being something a reviewer reads and becomes the
    environment the run actually has."""
    passing = launch_env(["SLACK_BOT_TOKEN", "ATLANTIS_DSN"], MACHINE | SECRETS)
    assert passing["SLACK_BOT_TOKEN"] == "xoxb-posts"
    assert passing["ATLANTIS_DSN"] == "postgres://atlantis"
    assert "BABYLON_DSN" not in passing, "one environment declared is one environment reachable"
    assert "AWS_SECRET_ACCESS_KEY" not in passing


def test_the_listener_s_own_token_reaches_no_run() -> None:
    """No pipeline declares it, so no run it starts can open a socket as the app
    that started it."""
    passing = launch_env(["SLACK_BOT_TOKEN"], MACHINE | SECRETS)
    assert "SLACK_APP_TOKEN" not in passing


def test_the_machine_baseline_survives() -> None:
    """A run that cannot find its own commands is a confusing failure, not a
    safe one."""
    passing = launch_env([], MACHINE | SECRETS)
    assert set(MACHINE) <= set(passing)


def test_model_credentials_are_matched_by_prefix_not_listed() -> None:
    """A provider added upstream brings its own variable names, and a run that
    cannot authenticate fails in a way nobody connects to this."""
    passing = launch_env([], {**MACHINE, "ANTHROPIC_API_KEY": "k", "CONDUCTOR_HOME": "/c"})
    assert passing["ANTHROPIC_API_KEY"] == "k"
    assert passing["CONDUCTOR_HOME"] == "/c"


def test_what_was_never_set_stays_absent_rather_than_empty() -> None:
    """A program testing `os.environ.get(NAME)` should see the same nothing it
    would see on a machine where nobody set it."""
    passing = launch_env(["NEVER_SET_ANYWHERE"], MACHINE)
    assert "NEVER_SET_ANYWHERE" not in passing


def test_the_launch_passes_the_filtered_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not merely computed — handed to the subprocess."""
    seen: dict[str, dict[str, str]] = {}

    def _record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        handed = kwargs["env"]
        assert isinstance(handed, dict)
        seen["env"] = handed
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr("ictus.runs.launch.live_runs", lambda *_, **__: [])
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    trigger = Trigger(
        workflow=Path("x.yaml"),
        env=(Need("SLACK_BOT_TOKEN", "to post"),),
        commands=(),
    )
    assert start(_ask(), trigger).ok
    assert seen["env"]["SLACK_BOT_TOKEN"] == "xoxb-posts"
    assert "SLACK_APP_TOKEN" not in seen["env"]
    assert "ATLANTIS_DSN" not in seen["env"]


def test_a_run_works_in_its_pipeline_folder_not_the_listener_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployed pipeline's relative paths are its own. Inheriting the
    listener's directory put a run's files one level above the deployment."""
    seen: dict[str, object] = {}

    def _record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cwd"] = kwargs["cwd"]
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    folder = tmp_path / "deployed" / "smoke"
    (folder / "build").mkdir(parents=True)
    (folder / "build" / "smoke.yaml").write_text("workflow: {}\n", encoding="utf-8")
    elsewhere = tmp_path / "listener"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr("ictus.runs.launch.live_runs", lambda *_, **__: [])
    relative = Path("..") / "deployed" / "smoke" / "build" / "smoke.yaml"
    assert start(_ask(), Trigger(workflow=relative)).ok
    assert seen["cwd"] == folder.resolve()
    command = seen["command"]
    assert isinstance(command, list)
    assert str((folder / "build" / "smoke.yaml").resolve()) in command, (
        "a relative workflow path would name nothing from the run's own directory"
    )


def test_a_workflow_built_outside_a_pipeline_folder_works_where_it_sits(tmp_path: Path) -> None:
    """No `build/` to step out of, so nothing above it is presumed to be ours."""
    assert Trigger(workflow=tmp_path / "loose.yaml").folder == tmp_path.resolve()
