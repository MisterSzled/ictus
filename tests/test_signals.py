"""Signals, integration declarations, and the mapping onto Conductor's events."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ictus import (
    END,
    ComputeNode,
    EnvVar,
    Integration,
    OutputPort,
    Pipeline,
    PortType,
    RunSignal,
    Stage,
)
from ictus.errors import CompositionError
from ictus.interfaces import Capabilities
from ictus.interfaces.conductor import ConductorBackend
from ictus.interfaces.conductor.control.signals import REPORTABLE, SIGNAL_EVENTS, signal_for
from ictus.interfaces.environment import integration_issues
from ictus.lint import lint_pipeline
from ictus.notify.slack import slack_webhook

FIXTURES = Path(__file__).parent / "fixtures"
S = PortType.STRING


def _pipeline(*targets: Integration) -> Pipeline:
    p = Pipeline(pipeline_id="demo")
    node = p.add(ComputeNode(node_id="step", value="x", declared_outputs=(OutputPort("value", S),)))
    p.set_entry(node)
    p.route(node, END)
    for target in targets:
        p.integrate(target)
    return p


def _integration(**kwargs: object) -> Integration:
    """Built through a constructor in notify, the way a pipeline author would."""
    fields: dict[str, object] = {
        "url": EnvVar("HOOK_URL", "where to post"),
        "reports": (RunSignal.DECISION_NEEDED,),
    }
    fields.update(kwargs)
    return slack_webhook(**fields)  # type: ignore[arg-type]


# --- declaring one ----------------------------------------------------------


def test_an_integration_is_addressed_by_name_so_two_cannot_share_one() -> None:
    p = _pipeline(_integration())
    with pytest.raises(CompositionError, match="already integrates something called"):
        p.integrate(_integration())


def test_signals_reach_the_caller_from_a_nested_stage() -> None:
    """A stage reports onto its caller's stream, so the subscription is theirs."""
    stage = Stage(stage_id="inner")
    inner = stage.body.add(
        ComputeNode(node_id="inner_step", value="x", declared_outputs=(OutputPort("value", S),))
    )
    stage.body.set_entry(inner)
    stage.body.route(inner, END)
    stage.body.expose_output("value", inner, "value")
    stage.body.integrate(_integration(name="child-slack", reports=(RunSignal.RUN_FAILED,)))

    parent = _pipeline(_integration(name="mine"))
    stage.instantiate(parent)

    assert sorted(t.name for t in parent.all_integrations()) == ["child-slack", "mine"]
    assert parent.subscribed_signals() == {RunSignal.DECISION_NEEDED, RunSignal.RUN_FAILED}
    assert [t.name for t in parent.integrations] == ["mine"], "the property stays local"


# --- what the backend can report -------------------------------------------


def test_a_signal_the_backend_cannot_report_is_a_lint_failure() -> None:
    """Configured, passing preflight, and silently never firing is the trap."""
    nothing_reportable = Capabilities(name="mute", kinds=frozenset(), signals=frozenset())

    class Mute(ConductorBackend):
        def capabilities(self) -> Capabilities:
            return nothing_reportable

    problems = lint_pipeline(_pipeline(_integration()), backend=Mute())
    assert any("cannot report" in p and "decision_needed" in p for p in problems)


def test_conductor_reports_every_signal_ictus_models() -> None:
    assert frozenset(RunSignal) == REPORTABLE


def test_a_subscribed_pipeline_passes_the_lint_on_conductor() -> None:
    assert lint_pipeline(_pipeline(_integration()), backend=ConductorBackend()) == []


# --- the mapping onto Conductor --------------------------------------------


def test_every_signal_maps_to_at_least_one_event() -> None:
    for signal in RunSignal:
        assert SIGNAL_EVENTS[signal], signal


def test_no_event_stands_for_two_signals() -> None:
    """Otherwise `signal_for` silently picks one and the other never fires."""
    seen: dict[str, RunSignal] = {}
    for signal, events in SIGNAL_EVENTS.items():
        for event in events:
            assert event not in seen, f"{event} maps to {seen.get(event)} and {signal}"
            seen[event] = signal


def test_an_unmapped_event_is_dropped_rather_than_raising() -> None:
    """An engine upgrade must not break a watcher that is already mid-run."""
    assert signal_for("agent_tool_start") is None
    assert signal_for("nothing_like_this") is None


@pytest.mark.parametrize("fixture", ["run-events-approved.jsonl", "run-events-rejected.jsonl"])
def test_the_recorded_runs_are_readable_as_signals(fixture: str) -> None:
    """The fixtures are real runs, so the names in them are the engine's own."""
    events = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
    signals = [signal_for(e["type"]) for e in events]
    assert RunSignal.RUN_STARTED in signals
    assert RunSignal.DECISION_NEEDED in signals
    assert RunSignal.DECISION_MADE in signals
    assert RunSignal.RUN_FINISHED in signals
    assert None in signals, "a trace event should map to nothing, not to a signal"


def test_the_gate_events_in_the_fixtures_still_have_the_names_mapped() -> None:
    """These names came out of a live 0.1.41 run, and the recordings depend on them."""
    recorded = {
        json.loads(line)["type"]
        for line in (FIXTURES / "run-events-approved.jsonl").read_text().splitlines()
    }
    assert "gate_presented" in recorded
    assert "gate_resolved" in recorded
    assert recorded >= {"workflow_started", "workflow_completed"}


#: Mapped names an engine older than the version given does not emit yet.
_INTRODUCED = {"mcp_failed": (0, 1, 41)}


def _installed_engine() -> tuple[str, tuple[int, ...]]:
    """The installed engine's source, and its version.

    Found through the console script: the engine lives in its own environment.
    """
    binary = shutil.which("conductor")
    assert binary is not None
    python = Path(os.path.realpath(binary)).parent / "python"
    located = subprocess.run(
        [
            str(python),
            "-c",
            "import conductor, pathlib; print(pathlib.Path(conductor.__file__).parent)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    package = Path(located.stdout.strip())
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
    printed = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", printed.stdout)
    assert found is not None, printed.stdout
    return source, tuple(int(part) for part in found.groups())


def test_every_mapped_event_is_one_the_installed_engine_emits(backend: ConductorBackend) -> None:
    """What catches the engine renaming an event under us.

    The watcher drops names it does not know, so a renamed event silently
    stops arriving. Reads the engine's own source rather than a recording.
    """
    assert backend is not None
    source, version = _installed_engine()
    expected = {
        name
        for names in SIGNAL_EVENTS.values()
        for name in names
        if version >= _INTRODUCED.get(name, (0, 0, 0))
    }
    assert sorted(name for name in expected if f'"{name}"' not in source) == []


# --- preflight --------------------------------------------------------------


def test_an_unset_endpoint_variable_blocks_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIKE_WEBHOOK_URL", raising=False)
    p = _pipeline(_integration(url=EnvVar("SPIKE_WEBHOOK_URL", "where to post")))
    issues = integration_issues(p)
    assert [i.requirement for i in issues] == ["integrate:slack"]
    assert issues[0].blocking
    assert "SPIKE_WEBHOOK_URL" in issues[0].problem


def test_a_set_endpoint_variable_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIKE_WEBHOOK_URL", "https://example.invalid/hook")
    p = _pipeline(_integration(url=EnvVar("SPIKE_WEBHOOK_URL", "where to post")))
    assert integration_issues(p) == []


def test_the_remedy_says_what_to_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIKE_WEBHOOK_URL", raising=False)
    p = _pipeline(
        _integration(
            url=EnvVar("SPIKE_WEBHOOK_URL", "where to post"),
            setup_hint="create an incoming webhook and export SPIKE_WEBHOOK_URL",
        )
    )
    assert "incoming webhook" in integration_issues(p)[0].remedy


def test_preflight_reports_integrations_alongside_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One question asked in one place; a second list would get forgotten."""
    monkeypatch.delenv("SPIKE_WEBHOOK_URL", raising=False)
    p = _pipeline(_integration(url=EnvVar("SPIKE_WEBHOOK_URL", "where to post")))
    issues = ConductorBackend().preflight(p, probe=False)
    assert any(i.requirement == "integrate:slack" for i in issues)


def test_an_integration_declaring_no_variable_needs_nothing_from_the_environment() -> None:
    """Not every service is reached with a credential."""
    free = Integration(
        name="stdout",
        purpose="A service that needs nothing configured",
        reports=(RunSignal.RUN_FAILED,),
        program="import sys; sys.stdin.read()",
    )
    assert integration_issues(_pipeline(free)) == []


def test_a_sender_that_is_not_installed_blocks_the_launch() -> None:
    """The one way a report can still fail a run: its step cannot start at all."""
    absent = Integration(
        name="absent",
        purpose="A service whose sender is not installed",
        reports=(RunSignal.RUN_FAILED,),
        command="definitely-not-a-command",
        program="pass",
    )
    (issue,) = integration_issues(_pipeline(absent))
    assert issue.blocking
    assert "not on PATH" in issue.problem
