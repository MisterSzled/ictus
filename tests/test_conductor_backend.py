"""The Conductor backend: exact emitted shapes.

These assert the whole dict, not the presence of keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import (
    END,
    AgentNode,
    ComputeNode,
    InputPort,
    OutputPort,
    Pipeline,
    PortType,
    ScriptNode,
    Stage,
    WaitNode,
)
from ictus.errors import CompositionError
from ictus.interfaces.conductor import ConductorBackend
from ictus.interfaces.conductor.emit.serialize import dump_yaml
from ictus.stdlib import approval_gate, succeed

if TYPE_CHECKING:
    from ictus.graph.values import YamlDict, YamlValue

BACKEND = ConductorBackend()
S, A = PortType.STRING, PortType.ARRAY


def _d(value: YamlValue) -> YamlDict:
    """Narrow an emitted value to a mapping. Parsing at the edge of the assertion."""
    assert isinstance(value, dict), f"expected a mapping, got {type(value).__name__}"
    return value


def _agents(pipeline: Pipeline) -> dict[str, YamlDict]:
    agents = BACKEND.document(pipeline)["agents"]
    assert isinstance(agents, list)
    out: dict[str, YamlDict] = {}
    for entry in agents:
        agent = _d(entry)
        name = agent["name"]
        assert isinstance(name, str)
        out[name] = agent
    return out


def _workflow(pipeline: Pipeline) -> YamlDict:
    return _d(BACKEND.document(pipeline)["workflow"])


class TestAgentShapes:
    def test_agent_node_emits_exactly(self) -> None:
        p = Pipeline(pipeline_id="t")
        p.add(
            AgentNode(
                node_id="think",
                description="Think hard",
                prompt="ponder",
                declared_outputs=(OutputPort("idea", S, "an idea"),),
            )
        )
        p.route(p.nodes[0], END)
        assert _agents(p)["think"] == {
            "name": "think",
            "description": "Think hard",
            "type": "agent",
            "prompt": "ponder",
            "output": {"idea": {"type": "string", "description": "an idea"}},
            "routes": [{"to": "$end"}],
        }

    def test_gate_emits_options_not_routes(self) -> None:
        """A ``routes:`` block on a gate validates and is then ignored by the engine."""
        p = Pipeline(pipeline_id="t")
        gate = p.add(approval_gate(node_id="g", prompt="ok?", reject_label="Nope"))
        yes = p.add(succeed(node_id="yes", reason="y"))
        no = p.add(succeed(node_id="no", reason="n"))
        p.set_entry(gate)
        p.branch(gate, {"approved": yes, "rejected": no})
        emitted = _agents(p)["g"]
        assert emitted == {
            "name": "g",
            "type": "human_gate",
            "prompt": "ok?",
            "options": [
                {"label": "Approve", "value": "approved", "route": "yes"},
                {
                    "label": "Nope",
                    "value": "rejected",
                    "route": "no",
                    "prompt_for": "notes",
                    "multiline": True,
                },
            ],
        }
        assert "routes" not in emitted

    def test_terminate_emits_status_and_no_routes(self) -> None:
        p = Pipeline(pipeline_id="t")
        p.add(succeed(node_id="done", reason="all good", result={"r": "{{ x }}"}))
        assert _agents(p)["done"] == {
            "name": "done",
            "type": "terminate",
            "status": "success",
            "reason": "all good",
            "output_template": {"r": "{{ x }}"},
        }

    def test_zero_cost_kinds_emit_their_own_types(self) -> None:
        p = Pipeline(pipeline_id="t")
        setter = p.add(ComputeNode(node_id="s", value="no", value_type=S))
        waiter = p.add(WaitNode(node_id="w", duration=2.0, reason="cool off"))
        script = p.add(ScriptNode(node_id="sh", command="/bin/ls", args=("-l",), timeout=30))
        p.route(setter, waiter)
        p.route(waiter, script)
        p.route(script, END)
        emitted = _agents(p)
        assert _d(emitted["s"])["type"] == "set"
        assert _d(emitted["s"])["value"] == "no"
        assert _d(emitted["s"])["output_type"] == "string"
        assert emitted["w"] == {
            "name": "w",
            "type": "wait",
            "duration": 2.0,
            "reason": "cool off",
            "routes": [{"to": "sh"}],
        }
        assert _d(emitted["sh"])["type"] == "script"
        assert _d(emitted["sh"])["command"] == "/bin/ls"
        assert _d(emitted["sh"])["args"] == ["-l"]


class TestDataflow:
    def test_loop_back_reference_is_optional_and_forward_is_not(self) -> None:
        """On pass one the gate has not run; a required ref there is a hard failure."""
        p = Pipeline(pipeline_id="t", loop_passes=2)
        draft = p.add(
            AgentNode(
                node_id="draft",
                inputs=(InputPort("notes", S, optional=True),),
                prompt="write",
                declared_outputs=(OutputPort("text", S),),
            )
        )
        gate = p.add(approval_gate(node_id="review", prompt="ok?", inputs=(InputPort("text", S),)))
        done = p.add(succeed(node_id="done", reason="d"))
        p.set_entry(draft)
        p.connect(draft, "text", gate, "text")
        p.branch(gate, {"approved": done, "rejected": draft})
        p.feed(gate, "notes", draft, "notes")
        emitted = _agents(p)
        assert _d(emitted["draft"])["input"] == ["review.output.additional_input.notes?"]
        assert _d(emitted["review"])["input"] == ["draft.output.text"]

    def test_data_can_cross_a_gate_without_a_control_edge(self) -> None:
        p = Pipeline(pipeline_id="t")
        src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("v", A),)))
        gate = p.add(approval_gate(node_id="g", prompt="?", inputs=(InputPort("v", A),)))
        after = p.add(
            AgentNode(node_id="after", inputs=(InputPort("v", A),), prompt="use {{ src.output.v }}")
        )
        stop = p.add(succeed(node_id="stop", reason="s"))
        p.connect(src, "v", gate, "v")
        p.branch(gate, {"approved": after, "rejected": stop})
        p.feed(src, "v", after, "v")
        p.route(after, END)
        assert _agents(p)["after"]["input"] == ["src.output.v"]

    def test_workflow_input_reference_is_emitted(self) -> None:
        p = Pipeline(pipeline_id="t")
        param = p.declare_input("who", S, description="subject")
        node = p.add(AgentNode(node_id="hi", inputs=(InputPort("who", S),), prompt="hi {{ w }}"))
        p.connect_input(param, node, "who")
        p.route(node, END)
        assert _agents(p)["hi"]["input"] == ["workflow.input.who"]
        assert _workflow(p)["input"] == {
            "who": {"type": "string", "required": True, "description": "subject"}
        }

    def test_optional_workflow_input_is_marked_optional(self) -> None:
        p = Pipeline(pipeline_id="t")
        param = p.declare_input("who", S, required=False, default="world")
        node = p.add(AgentNode(node_id="hi", inputs=(InputPort("who", S),), prompt="hi"))
        p.connect_input(param, node, "who")
        p.route(node, END)
        assert _agents(p)["hi"]["input"] == ["workflow.input.who?"]


class TestRouteOrdering:
    def test_the_catch_all_is_emitted_last(self) -> None:
        """Conductor takes the first match, so authoring order must not decide this."""
        p = Pipeline(pipeline_id="t")
        src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("v", S),)))
        fallback = p.add(succeed(node_id="fallback", reason="f"))
        special = p.add(succeed(node_id="special", reason="s"))
        p.route(src, fallback)  # written first, on purpose
        p.route(src, special, when="{{ src.output.v == 'x' }}")
        assert _agents(p)["src"]["routes"] == [
            {"to": "special", "when": "{{ src.output.v == 'x' }}"},
            {"to": "fallback"},
        ]


class TestWorkflowBlock:
    def test_checkpointing_is_on_whether_or_not_a_gate_is_present(self) -> None:
        """The crash worth a checkpoint is the one that raises nothing.

        The engine saves on failure by itself; a hang or a killed process
        reaches no failure handler.
        """
        plain = Pipeline(pipeline_id="plain")
        plain.add(succeed(node_id="done", reason="d"))
        assert _d(_workflow(plain)["runtime"])["checkpoint"] == {"every_agent": True}

        gated = Pipeline(pipeline_id="gated")
        gate = gated.add(approval_gate(node_id="g", prompt="?"))
        ok = gated.add(succeed(node_id="ok", reason="o"))
        no = gated.add(succeed(node_id="no", reason="n"))
        gated.set_entry(gate)
        gated.branch(gate, {"approved": ok, "rejected": no})
        assert _d(_workflow(gated)["runtime"])["checkpoint"] == {"every_agent": True}

    def test_every_seconds_is_not_emitted_beside_every_agent(self) -> None:
        """Conductor ignores it whenever `every_agent` is set (config/schema.py)."""
        p = Pipeline(pipeline_id="t")
        p.add(succeed(node_id="done", reason="d"))
        assert "every_seconds" not in _d(_d(_workflow(p)["runtime"])["checkpoint"])

    def test_a_wall_clock_ceiling_is_emitted_into_limits(self) -> None:
        p = Pipeline(pipeline_id="t", timeout_seconds=900)
        p.add(succeed(node_id="done", reason="d"))
        assert _workflow(p)["limits"] == {"max_iterations": 1, "timeout_seconds": 900}

    def test_no_ceiling_emits_no_key(self) -> None:
        """Absent means unlimited; a zero or a default would be a number nobody chose."""
        p = Pipeline(pipeline_id="t")
        p.add(succeed(node_id="done", reason="d"))
        assert "timeout_seconds" not in _d(_workflow(p)["limits"])

    def test_a_ceiling_conductor_would_refuse_is_refused_where_it_is_written(self) -> None:
        with pytest.raises(CompositionError, match="timeout_seconds must be >= 1"):
            Pipeline(pipeline_id="t", timeout_seconds=0)

    def test_provider_is_always_explicit(self) -> None:
        """Conductor defaults to copilot; leaving it implicit fails at run time only."""
        p = Pipeline(pipeline_id="t")
        p.add(succeed(node_id="done", reason="d"))
        assert _d(_workflow(p)["runtime"])["provider"] == {"name": "copilot"}

    def test_budget_is_emitted_with_its_mode(self) -> None:
        p = Pipeline(pipeline_id="t", budget_usd=2.5, budget_mode="enforce")
        p.add(succeed(node_id="done", reason="d"))
        assert _workflow(p)["limits"] == {
            "max_iterations": 1,
            "budget_usd": 2.5,
            "budget_mode": "enforce",
        }


class TestStages:
    def test_a_stage_becomes_its_own_file_and_a_workflow_agent(self) -> None:
        stage = Stage(stage_id="inner", description="inner stage")
        param = stage.body.declare_input("x", S)
        step = stage.body.add(
            AgentNode(
                node_id="work",
                inputs=(InputPort("x", S),),
                prompt="work",
                declared_outputs=(OutputPort("y", S),),
            )
        )
        stage.body.connect_input(param, step, "x")
        stage.body.route(step, END)
        stage.body.expose_output("y", step, "y")

        parent = Pipeline(pipeline_id="outer")
        outer_param = parent.declare_input("x", S)
        host = stage.instantiate(parent, node_id="inner_call")
        done = parent.add(succeed(node_id="done", reason="d", inputs=(InputPort("y", S),)))
        parent.connect_input(outer_param, host, "x")
        parent.connect(host, "y", done, "y")

        compiled = BACKEND.compile(parent)
        assert [c.filename for c in compiled] == ["outer.yaml", "inner.yaml"]
        assert _agents(parent)["inner_call"] == {
            "name": "inner_call",
            "description": "inner stage",
            "type": "workflow",
            "workflow": "./inner.yaml",
            "input": ["workflow.input.x"],
            "input_mapping": {"x": "{{ workflow.input.x | tojson }}"},
            "routes": [{"to": "done"}],
        }

    def test_stage_output_types_survive_into_the_parent(self) -> None:
        """Conductor's ``output:`` map is dict[str, str]; the types live here or nowhere."""
        stage = Stage(stage_id="inner")
        stage.body.declare_input("x", S)
        step = stage.body.add(
            AgentNode(node_id="w", prompt="w", declared_outputs=(OutputPort("y", A),))
        )
        stage.body.route(step, END)
        stage.body.expose_output("y", step, "y")
        assert stage.output_ports == (OutputPort("y", A),)


class TestRoundTrip:
    """Emitted YAML must reload to exactly the values that were compiled.

    A wrapped double-quoted scalar loses its continuation marker, and
    reloading turns the break into a space.
    """

    @staticmethod
    def _reload(pipeline: Pipeline) -> object:
        from ruamel.yaml import YAML

        return YAML(typ="safe").load(dump_yaml(BACKEND.document(pipeline)))

    def test_a_long_multiline_prompt_survives(self) -> None:
        prompt = "x" * 120 + "\n\n{{ workflow.input.t }}\n\ntail " + "y" * 90
        p = Pipeline(pipeline_id="t")
        param = p.declare_input("t", S)
        node = p.add(AgentNode(node_id="a", inputs=(InputPort("t", S),), prompt=prompt))
        p.connect_input(param, node, "t")
        p.route(node, END)
        loaded = self._reload(p)
        assert isinstance(loaded, dict)
        assert loaded["agents"][0]["prompt"] == prompt

    def test_every_compiled_document_reloads_identically(self) -> None:
        p = Pipeline(pipeline_id="t", loop_passes=2)
        draft = p.add(
            AgentNode(
                node_id="draft",
                inputs=(InputPort("notes", S, optional=True),),
                prompt="Write it.\n\nNotes: {{ review.output.additional_input.notes }}\n"
                + "z" * 130,
                declared_outputs=(OutputPort("text", S),),
            )
        )
        gate = p.add(
            approval_gate(
                node_id="review",
                prompt="Accept?\n\n{{ draft.output.text }}\n" + "q" * 140,
                inputs=(InputPort("text", S),),
            )
        )
        done = p.add(succeed(node_id="done", reason="ok"))
        p.set_entry(draft)
        p.connect(draft, "text", gate, "text")
        p.branch(gate, {"approved": done, "rejected": draft})
        p.feed(gate, "notes", draft, "notes")
        assert self._reload(p) == BACKEND.document(p)
