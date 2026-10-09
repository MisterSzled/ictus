"""Reading back what a run actually did.

A step's output says what it concluded, not whether it looked at anything
first. The engine records the difference already.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, Stage
from ictus.interfaces.conductor.control.trace import find_logs, read_trace

if TYPE_CHECKING:
    from pathlib import Path


def _log(tmp_path: Path, *events: dict[str, object], name: str = "demo") -> Path:
    path = tmp_path / f"conductor-{name}-20260906-113009-025ea788.events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _tool(agent: str, tool: str, **args: str) -> dict[str, object]:
    return {
        "type": "agent_tool_start",
        "data": {"agent_name": agent, "tool_name": tool, "arguments": args},
    }


def test_it_separates_looking_something_up_from_emitting_an_answer(tmp_path: Path) -> None:
    """Counting StructuredOutput as activity would make every step look diligent."""
    path = _log(
        tmp_path,
        _tool("survey", "Read", file_path="/repo/README.md"),
        _tool("survey", "Bash", command="ls"),
        _tool("survey", "StructuredOutput"),
        _tool("voice", "StructuredOutput"),
        {"type": "agent_turn_start", "data": {"agent_name": "voice", "turn": "awaiting_model"}},
    )
    trace = read_trace(path)
    assert trace.steps["survey"].investigated == 2
    assert trace.steps["survey"].looked
    assert trace.steps["voice"].investigated == 0
    assert not trace.steps["voice"].looked


def test_a_step_that_answered_without_consulting_anything_is_named(tmp_path: Path) -> None:
    path = _log(
        tmp_path,
        _tool("survey", "Read", file_path="/repo/a.py"),
        {"type": "agent_turn_start", "data": {"agent_name": "survey", "turn": "awaiting_model"}},
        _tool("voice", "StructuredOutput"),
        {"type": "agent_turn_start", "data": {"agent_name": "voice", "turn": "awaiting_model"}},
    )
    assert [s.name for s in read_trace(path).incurious] == ["voice"]


def test_a_step_that_never_ran_is_not_accused_of_idleness(tmp_path: Path) -> None:
    """A gate nobody reached made no calls, which is not the same failing."""
    path = _log(tmp_path, {"type": "agent_started", "data": {"agent_name": "gate"}})
    assert read_trace(path).incurious == []


def test_it_records_what_was_opened(tmp_path: Path) -> None:
    path = _log(
        tmp_path,
        _tool("survey", "Read", file_path="/repo/a.py"),
        _tool("survey", "Read", file_path="/repo/b.py"),
    )
    assert read_trace(path).steps["survey"].reads == ["/repo/a.py", "/repo/b.py"]


def test_cost_and_tokens_come_from_the_completion_events(tmp_path: Path) -> None:
    path = _log(
        tmp_path,
        {
            "type": "parallel_agent_completed",
            "data": {"agent_name": "voice", "cost_usd": 0.12, "tokens": 20283, "elapsed": 9.5},
        },
    )
    step = read_trace(path).steps["voice"]
    assert step.cost_usd == 0.12
    assert step.tokens == 20283


def test_a_workflow_id_containing_hyphens_survives_the_filename(tmp_path: Path) -> None:
    path = _log(tmp_path, _tool("a", "Read"), name="needs-council")
    assert read_trace(path).workflow == "needs-council"


def test_a_truncated_final_line_is_normal_on_a_live_run(tmp_path: Path) -> None:
    """Tracing a run in flight must not fail on the line being written."""
    path = _log(tmp_path, _tool("survey", "Read", file_path="/repo/a.py"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "agent_tool_st')
    assert read_trace(path).steps["survey"].investigated == 1


def test_logs_come_back_newest_first(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import ictus.interfaces.conductor.control.trace as trace_module

    monkeypatch.setattr(trace_module, "LOG_DIR", tmp_path)
    older = _log(tmp_path, _tool("a", "Read"), name="demo")
    newer = tmp_path / "conductor-demo-20260907-113009-aaaaaaaa.events.jsonl"
    newer.write_text("{}\n")
    assert find_logs("demo")[0] == newer
    assert older in find_logs("demo")


def test_another_workflows_logs_are_not_returned(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import ictus.interfaces.conductor.control.trace as trace_module

    monkeypatch.setattr(trace_module, "LOG_DIR", tmp_path)
    _log(tmp_path, _tool("a", "Read"), name="demo")
    _log(tmp_path, _tool("a", "Read"), name="other")
    assert [p.name for p in find_logs("other")] == [
        "conductor-other-20260906-113009-025ea788.events.jsonl"
    ]


def test_a_downloaded_log_is_a_json_array_not_one_object_per_line(tmp_path: Path) -> None:
    """The dashboard's download button hands you the same events as an array.

    Reading only the line-per-object shape parses one to nothing.
    """
    path = tmp_path / "conductor-logs.json"
    path.write_text(
        json.dumps(
            [
                _tool("survey", "Read", file_path="/repo/a.py"),
                _tool("survey", "Bash", command="ls"),
                _tool("voice", "StructuredOutput"),
                {
                    "type": "agent_turn_start",
                    "data": {"agent_name": "voice", "turn": "awaiting_model"},
                },
            ]
        )
    )
    trace = read_trace(path)
    assert trace.steps["survey"].investigated == 2
    assert [s.name for s in trace.incurious] == ["voice"]


def test_a_pretty_printed_array_reads_the_same(tmp_path: Path) -> None:
    path = tmp_path / "conductor-logs.json"
    path.write_text(json.dumps([_tool("a", "Read", file_path="/x")], indent=2))
    assert read_trace(path).steps["a"].investigated == 1


def _turn(agent: str) -> dict[str, object]:
    return {"type": "agent_turn_start", "data": {"agent_name": agent, "turn": "awaiting_model"}}


def test_turns_are_counted_in_the_units_the_engine_enforces(tmp_path: Path) -> None:
    """The same event also carries a numeric index that advances about twice a round.

    Counting those reports 102 for a step the engine killed "after 51 turns".
    """
    path = _log(
        tmp_path,
        _turn("a"),
        {"type": "agent_turn_start", "data": {"agent_name": "a", "turn": 1}},
        {"type": "agent_turn_start", "data": {"agent_name": "a", "turn": 2}},
        _turn("a"),
    )
    assert read_trace(path).steps["a"].turns == 2


def test_a_step_stopped_by_the_ceiling_is_called_out(tmp_path: Path) -> None:
    """Running out of turns raises rather than returning, so it fails the run."""
    path = _log(tmp_path, *[_turn("verify") for _ in range(50)])
    trace = read_trace(path)
    assert trace.steps["verify"].at_the_cap
    assert [s.name for s in trace.capped] == ["verify"]


def test_an_ordinary_step_is_not(tmp_path: Path) -> None:
    path = _log(tmp_path, *[_turn("voice") for _ in range(26)])
    assert read_trace(path).capped == []


class TestDeclaredCeilings:
    """A step is at its ceiling when it reaches its own limit, not the default.

    Measuring a step that declares ``max_turns=200`` against the engine's
    fifty fires the one alarm that matters on a healthy run.
    """

    def _log(self, tmp_path: Path, step: str, turns: int) -> Path:
        path = tmp_path / "conductor-w-20260101-000000-abc.events.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "agent_turn_start",
                    "data": {"agent_name": step, "turn": "awaiting_model"},
                }
            )
            for _ in range(turns)
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_a_raised_ceiling_is_not_reported_as_hit(self, tmp_path: Path) -> None:
        seen = read_trace(self._log(tmp_path, "verify", 50), ceilings={"verify": 200})
        assert seen.steps["verify"].turns == 50
        assert not seen.capped, "50 of a declared 200 is not the ceiling"

    def test_the_engine_default_still_applies_to_a_step_that_set_nothing(
        self, tmp_path: Path
    ) -> None:
        seen = read_trace(self._log(tmp_path, "survey", 50))
        assert [s.name for s in seen.capped] == ["survey"]

    def test_a_raised_ceiling_is_reported_when_actually_reached(self, tmp_path: Path) -> None:
        seen = read_trace(self._log(tmp_path, "verify", 200), ceilings={"verify": 200})
        assert [s.name for s in seen.capped] == ["verify"]

    def test_ceilings_are_collected_through_nested_stages(self) -> None:
        """A stage's steps appear in the parent run's log under their own names."""
        from ictus.cli.tracing import _declared_ceilings

        parent = Pipeline(pipeline_id="parent")
        stage = Stage(stage_id="inner")
        param = stage.body.declare_input("x", PortType.STRING)
        deep = stage.body.add(
            AgentNode(
                node_id="deep",
                inputs=(InputPort("x", PortType.STRING),),
                prompt="p",
                max_turns=250,
                declared_outputs=(OutputPort("y", PortType.STRING),),
            )
        )
        stage.body.connect_input(param, deep, "x")
        stage.body.route(deep, END)
        stage.body.expose_output("y", deep, "y")
        stage.instantiate(parent)
        assert _declared_ceilings(parent) == {"deep": 250}
