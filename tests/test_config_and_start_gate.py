"""Run policy in `config.yaml`, and the start gate it switches on.

Conductor's dashboard has stop, kill and resume but no start, so "let me press
go" is built out of a gate every pipeline gets by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl
from ictus.assemble.start_gate import CANCELLED_ID, GATE_ID, add_start_gate
from ictus.errors import CompositionError
from ictus.graph.mapping import Item
from ictus.graph.traversal import budget_cost
from ictus.interfaces.conductor import ConductorBackend, conductor
from ictus.lint import lint_pipeline
from ictus.runspec.config import MINIMAL, ConfigError, PipelineConfig, read_config
from ictus.stdlib import Voice, council
from ictus.stdlib.baseline import AGENT_BASELINE
from ictus.stdlib.exits import succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.values import YamlDict

STR = PortType.STRING


def _pipeline() -> Pipeline:
    p = Pipeline(pipeline_id="demo", description="Does a thing")
    target = p.declare_input("target", STR, description="What to act on")
    charge = p.declare_input("charge", STR, required=False, prose=True)
    work = p.add(
        AgentNode(
            node_id="work",
            inputs=(InputPort("target", STR), InputPort("charge", STR, optional=True)),
            prompt=tpl("do ", target.ref()),
            declared_outputs=(OutputPort("out", STR),),
        )
    )
    p.set_entry(work)
    p.connect_input(target, work, "target")
    p.connect_input(charge, work, "charge")
    p.route(work, END)
    return p


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def _agent(pipeline: Pipeline, name: str) -> YamlDict:
    agents = conductor.document(pipeline)["agents"]
    assert isinstance(agents, list)
    for candidate in agents:
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return candidate
    raise AssertionError(f"no agent named {name!r}")


# --- config ----------------------------------------------------------------


def test_the_minimal_config_is_one_line(tmp_path: Path) -> None:
    settings = read_config(_config(tmp_path, MINIMAL))
    assert settings.provider == "claude-agent-sdk"
    assert settings.start_gate is True


def test_a_missing_config_says_what_to_write(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="provider: claude-agent-sdk"):
        read_config(tmp_path / "config.yaml")


def test_provider_has_no_default(tmp_path: Path) -> None:
    """Conductor's own default is copilot; inheriting it silently already bit us."""
    with pytest.raises(ConfigError, match="needs a `provider`"):
        read_config(_config(tmp_path, "start_gate: false\n"))


def test_a_key_that_does_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\['startgate'\] are not settings"):
        read_config(_config(tmp_path, "provider: claude\nstartgate: false\n"))


def test_policy_reaches_the_pipeline(tmp_path: Path) -> None:
    settings = read_config(
        _config(tmp_path, "provider: claude\ndefault_model: opus\nbudget_usd: 5\n")
    )
    pipeline = _pipeline()
    settings.apply(pipeline, where="config.yaml")
    assert pipeline.provider == "claude"
    assert pipeline.default_model == "opus"
    assert pipeline.budget_usd == 5.0


def test_two_sources_of_truth_that_disagree_are_refused(tmp_path: Path) -> None:
    """Whichever one you read would be the wrong one."""
    settings = read_config(_config(tmp_path, "provider: claude\n"))
    pipeline = _pipeline()
    pipeline.provider = "copilot"
    with pytest.raises(ConfigError, match="take it out of the composition"):
        settings.apply(pipeline, where="config.yaml")


def test_a_wall_clock_ceiling_is_policy_and_reaches_the_emitted_limits(tmp_path: Path) -> None:
    """Where the run happens decides how long it may take; the graph has no view."""
    settings = read_config(_config(tmp_path, "provider: claude\ntimeout_seconds: 900\n"))
    pipeline = _pipeline()
    settings.apply(pipeline, where="config.yaml")
    assert pipeline.timeout_seconds == 900
    workflow = conductor.document(pipeline)["workflow"]
    assert isinstance(workflow, dict)
    limits = workflow["limits"]
    assert isinstance(limits, dict)
    assert limits["timeout_seconds"] == 900


def test_a_ceiling_conductor_would_refuse_is_refused_in_the_file(tmp_path: Path) -> None:
    """Conductor bounds it at 1; a 0 here would load clean and fail at run time."""
    with pytest.raises(ConfigError, match="timeout_seconds must be at least 1"):
        read_config(_config(tmp_path, "provider: claude\ntimeout_seconds: 0\n"))


def test_a_flag_must_be_a_flag(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be true or false"):
        read_config(_config(tmp_path, "provider: claude\nstart_gate: yes please\n"))


def test_the_backend_declares_which_providers_exist() -> None:
    """A closed set in Conductor's schema, so a typo is knowable at composition."""
    providers = conductor.capabilities().providers
    assert "claude-agent-sdk" in providers
    assert "claud" not in providers


# --- the start gate --------------------------------------------------------


def test_the_gate_becomes_the_entry_point() -> None:
    pipeline = add_start_gate(_pipeline())
    assert pipeline.entry().node_id == GATE_ID
    doc = conductor.document(pipeline)
    workflow = doc["workflow"]
    assert isinstance(workflow, dict)
    assert workflow["entry_point"] == GATE_ID


def test_it_shows_the_actual_input_values_not_that_some_exist() -> None:
    prompt = _agent(add_start_gate(_pipeline()), GATE_ID)["prompt"]
    assert isinstance(prompt, str)
    assert "{{ workflow.input.target }}" in prompt
    assert "{{ workflow.input.charge }}" in prompt


def test_an_absent_optional_input_renders_nothing() -> None:
    """The engine binds it to None, so a definedness guard would print that word."""
    prompt = _agent(add_start_gate(_pipeline()), GATE_ID)["prompt"]
    assert isinstance(prompt, str)
    assert "{% if workflow.input.charge %}" in prompt
    assert "{% if workflow.input.target %}" not in prompt


def test_starting_goes_to_what_used_to_be_the_entry() -> None:
    options = _agent(add_start_gate(_pipeline()), GATE_ID)["options"]
    assert isinstance(options, list)
    assert [(o["value"], o["route"]) for o in options if isinstance(o, dict)] == [
        ("start", "work"),
        ("cancel", CANCELLED_ID),
    ]


def test_start_is_first_because_skip_gates_takes_the_first_option() -> None:
    """An unattended run has made this decision by being unattended."""
    options = _agent(add_start_gate(_pipeline()), GATE_ID)["options"]
    assert isinstance(options, list)
    first = options[0]
    assert isinstance(first, dict)
    assert first["value"] == "start"


def test_declining_is_a_success_not_a_failure() -> None:
    """A person looking at it and saying no is not an error for a caller to handle."""
    stopped = _agent(add_start_gate(_pipeline()), CANCELLED_ID)
    assert stopped["status"] == "success"
    assert stopped["output_template"] == {"started": "false"}


def test_a_name_collision_is_refused_rather_than_silently_renamed() -> None:
    p = _pipeline()
    p.add(AgentNode(node_id=GATE_ID, prompt="x"))
    with pytest.raises(CompositionError, match="already has a node called"):
        add_start_gate(p)


def test_a_gated_pipeline_is_lint_clean_and_loads(
    validates: Callable[[Pipeline], None],
) -> None:
    pipeline = add_start_gate(_pipeline())
    assert lint_pipeline(pipeline, backend=conductor) == []
    validates(pipeline)


def test_it_costs_one_iteration_and_no_provider_call() -> None:
    plain = conductor.document(_pipeline())["workflow"]
    gated = conductor.document(add_start_gate(_pipeline()))["workflow"]
    assert isinstance(plain, dict) and isinstance(gated, dict)
    before, after = plain["limits"], gated["limits"]
    assert isinstance(before, dict) and isinstance(after, dict)
    # The gate and its terminal: two steps, neither of which calls a provider.
    assert after["max_iterations"] == before["max_iterations"] + 2  # type: ignore[operator]


def _fan_out() -> Pipeline:
    """Three nodes, one of which fans out over six items: eight step executions."""
    p = Pipeline(pipeline_id="fan", provider="claude-agent-sdk")
    brief = p.declare_input("brief", STR)
    split = p.add(
        AgentNode(
            node_id="split",
            inputs=(InputPort("brief", STR),),
            prompt=tpl("Split ", brief.ref()),
            declared_outputs=(OutputPort("pieces", PortType.ARRAY, element={"repo": STR}),),
        )
    )
    piece = Item(name="piece", fields={"repo": STR})
    work = p.add(
        AgentNode(
            node_id="work",
            prompt=tpl("Do ", piece.ref("repo")),
            declared_outputs=(OutputPort("summary", STR),),
        )
    )
    fanout = p.map_over(
        "workers", source=split.ref("pieces"), item=piece, body=work, expect_items=6
    )
    done = p.add(succeed(node_id="done", reason="done"))
    p.set_entry(split)
    p.connect_input(brief, split, "brief")
    p.route(split, fanout)
    p.route(fanout, done)
    return p


def _looping() -> Pipeline:
    p = Pipeline(pipeline_id="loop", provider="claude-agent-sdk", loop_passes=3)
    a = p.add(
        AgentNode(
            node_id="a",
            inputs=(InputPort("in", STR, optional=True),),
            prompt="work",
            declared_outputs=(OutputPort("out", STR),),
        )
    )
    b = p.add(
        AgentNode(
            node_id="b",
            inputs=(InputPort("in", STR),),
            prompt="check",
            declared_outputs=(OutputPort("out", STR),),
        )
    )
    p.set_entry(a)
    p.connect(a, "out", b, "in")
    p.connect(b, "out", a, "in")
    return p


def test_the_gate_prices_a_fan_out_by_what_it_spends() -> None:
    """`len(nodes)` reads like a step count and is not one: it prices this at three."""
    pipeline = _fan_out()
    assert len(pipeline.nodes) == 3
    prompt = _agent(add_start_gate(pipeline), GATE_ID)["prompt"]
    assert isinstance(prompt, str)
    assert "8 step(s)" in prompt


def test_the_gate_says_what_a_loop_can_cost() -> None:
    """One pass is what usually happens; the budget is what the person is approving."""
    prompt = _agent(add_start_gate(_looping()), GATE_ID)["prompt"]
    assert isinstance(prompt, str)
    assert "2 step(s) on one pass, up to 6 with loops" in prompt


@pytest.mark.parametrize("build", [_fan_out, _looping])
def test_the_quoted_cost_and_the_compiled_limit_come_from_one_place(
    build: Callable[[], Pipeline],
) -> None:
    """A gate quoting a number the emitted budget contradicts is worse than no number."""
    pipeline = add_start_gate(build())
    workflow = conductor.document(pipeline)["workflow"]
    assert isinstance(workflow, dict)
    limits = workflow["limits"]
    assert isinstance(limits, dict)
    assert limits["max_iterations"] == budget_cost(pipeline)


class TestWorkspaceInstructions:
    """Conductor runs agents with no settings sources — no CLAUDE.md, no skills.

    A step arrives knowing nothing about the project beyond its own prompt.
    """

    def test_instructions_reach_every_step(self, tmp_path: Path) -> None:
        (tmp_path / "context.md").write_text("This project is a thing.\n")
        settings = read_config(
            _config(tmp_path, "provider: claude\ninstructions:\n  - ./context.md\n")
        )
        pipeline = _pipeline()
        settings.apply(pipeline, where="config.yaml")
        workflow = conductor.document(pipeline)["workflow"]
        assert isinstance(workflow, dict)
        assert workflow["instructions"] == ["This project is a thing.\n"]

    def test_a_path_that_is_not_there_is_refused(self, tmp_path: Path) -> None:
        """Silently prepending nothing to every prompt is not noticed until later."""
        with pytest.raises(ConfigError, match="is not a file"):
            read_config(_config(tmp_path, "provider: claude\ninstructions:\n  - ./missing.md\n"))

    def test_literal_text_is_taken_as_written(self, tmp_path: Path) -> None:
        settings = read_config(
            _config(tmp_path, "provider: claude\ninstructions:\n  - Be terse.\n")
        )
        assert settings.instructions == ("Be terse.",)

    def test_an_env_reference_in_instructions_is_refused(self, tmp_path: Path) -> None:
        """Expanded at load: unset it refuses the workflow, set it leaks the value.

        The value would be prepended to every prompt and sent to the provider.
        """
        (tmp_path / "c.md").write_text("Use ${GITHUB_TOKEN:-} for auth.\n")
        settings = read_config(_config(tmp_path, "provider: claude\ninstructions:\n  - ./c.md\n"))
        pipeline = _pipeline()
        settings.apply(pipeline, where="config.yaml")
        problems = lint_pipeline(pipeline, backend=conductor)
        assert any("GITHUB_TOKEN" in p and "every prompt" in p for p in problems), problems


class TestRemembering:
    """A step that starts cold has read nothing, whatever it read last round."""

    def test_council_voices_keep_their_own_session(self) -> None:
        body = council(
            stage_id="panel",
            voices=(Voice("a", "p", "f"), Voice("b", "p", "f")),
        ).body
        keys = {
            n.node_id: getattr(n, "session_key", None)
            for n in body.nodes
            if getattr(n, "session_key", None) is not None
        }
        assert keys == {"a": "panel-a", "b": "panel-b"}

    def test_the_keys_differ_because_voices_run_at_once(self) -> None:
        """A session cannot be shared by concurrent steps."""
        body = council(
            stage_id="panel",
            voices=(Voice("a", "p", "f"), Voice("b", "p", "f")),
        ).body
        keys = [
            getattr(n, "session_key", None) for n in body.nodes if getattr(n, "session_key", None)
        ]
        assert len(keys) == len(set(keys))

    def test_remembering_can_be_turned_off(self) -> None:
        body = council(
            stage_id="panel",
            voices=(Voice("a", "p", "f"), Voice("b", "p", "f")),
            remember=False,
        ).body
        assert not any(getattr(n, "session_key", None) for n in body.nodes)

    def test_a_provider_that_cannot_resume_is_caught_at_composition(self) -> None:
        """Conductor refuses the workflow; better to hear it before emitting."""
        scope = council(stage_id="panel", voices=(Voice("a", "p", "f"), Voice("b", "p", "f")))
        scope.body.provider = "copilot"
        problems = lint_pipeline(scope.body, backend=conductor)
        assert any("cannot do" in p and "copilot" in p for p in problems), problems


class TestSystemPrompt:
    """An unset system prompt is an empty one, not a default one.

    Conductor forwards `AgentDef.system_prompt` to the SDK, which turns `None`
    into `--system-prompt ""`.
    """

    @staticmethod
    def _emitted(pipeline: Pipeline) -> str:
        agents = conductor.document(pipeline)["agents"]
        assert isinstance(agents, list)
        entry = next(a for a in agents if isinstance(a, dict) and a["name"] == "work")
        return str(entry.get("system_prompt", ""))

    def test_every_model_call_gets_one_by_default(self, tmp_path: Path) -> None:
        settings = read_config(_config(tmp_path, MINIMAL))
        pipeline = _pipeline()
        settings.apply(pipeline, where="config.yaml")
        assert "Check before you assert" in self._emitted(pipeline)

    def test_it_reaches_a_nested_stage(self, tmp_path: Path) -> None:
        """A stage is its own file; a baseline that stopped at the top is no baseline."""
        settings = read_config(_config(tmp_path, MINIMAL))
        scope = council(stage_id="panel", voices=(Voice("a", "p", "f"), Voice("b", "p", "f")))
        parent = Pipeline(pipeline_id="host")
        seat = scope.instantiate(parent, node_id="panel")
        parent.set_entry(seat)
        settings.apply(parent, where="config.yaml")
        agents = conductor.document(scope.body)["agents"]
        assert isinstance(agents, list)
        voice_a = next(a for a in agents if isinstance(a, dict) and a["name"] == "a")
        assert "Check before you assert" in str(voice_a["system_prompt"])

    def test_a_step_can_override_it(self, tmp_path: Path) -> None:
        settings = read_config(_config(tmp_path, MINIMAL))
        pipeline = _pipeline()
        work = next(n for n in pipeline.nodes if n.node_id == "work")
        object.__setattr__(work, "system_prompt", "Be a pirate.")
        settings.apply(pipeline, where="config.yaml")
        assert self._emitted(pipeline) == "Be a pirate."

    def test_it_can_be_switched_off_deliberately(self, tmp_path: Path) -> None:
        settings = read_config(_config(tmp_path, "provider: claude\nsystem_prompt: none\n"))
        pipeline = _pipeline()
        settings.apply(pipeline, where="config.yaml")
        assert self._emitted(pipeline) == ""

    def test_a_file_replaces_it(self, tmp_path: Path) -> None:
        (tmp_path / "house.md").write_text("House style: terse.\n")
        settings = read_config(_config(tmp_path, "provider: claude\nsystem_prompt: ./house.md\n"))
        pipeline = _pipeline()
        settings.apply(pipeline, where="config.yaml")
        assert self._emitted(pipeline) == "House style: terse.\n"


class TestProjectInstructionDiscovery:
    """A step must arrive knowing what the target project says about itself.

    The provider pins ``setting_sources=[]``, so ``--workspace-instructions``
    is the only route to those files.
    """

    def test_it_is_on_unless_the_folder_says_otherwise(self, tmp_path: Path) -> None:
        assert read_config(_config(tmp_path, "provider: claude\n")).workspace_instructions

    def test_a_folder_can_turn_it_off(self, tmp_path: Path) -> None:
        """A run that must behave identically against any checkout."""
        settings = read_config(
            _config(tmp_path, "provider: claude\nworkspace_instructions: false\n")
        )
        assert settings.workspace_instructions is False

    def test_the_flag_reaches_the_engine(self) -> None:
        backend = ConductorBackend()
        with patch("subprocess.run") as ran:
            ran.return_value.returncode = 0
            backend.run(Path("w.yaml"), inputs={}, dashboard=False, workspace_instructions=True)
        assert "--workspace-instructions" in ran.call_args[0][0]

    def test_and_is_absent_when_off(self) -> None:
        """Off must mean off: the flag has no negative form on the engine side."""
        backend = ConductorBackend()
        with patch("subprocess.run") as ran:
            ran.return_value.returncode = 0
            backend.run(Path("w.yaml"), inputs={}, dashboard=False, workspace_instructions=False)
        assert "--workspace-instructions" not in ran.call_args[0][0]


class TestBaselineDiscipline:
    """The baseline stands in for a system prompt Conductor cannot ask for.

    ``AgentDef.system_prompt`` is ``str | None`` and naming the ``claude_code``
    preset needs a mapping. Each rule below replaces one it would have carried.
    """

    def test_a_two_case_claim_must_be_checked_in_both(self) -> None:
        """A verify step struck a true finding by reading one case's docstring."""
        assert "has to be checked in both" in AGENT_BASELINE

    def test_missing_means_looked_for_first(self) -> None:
        """`checkpoint` was reported unwired while ictus was emitting it."""
        assert "look for where it would already be handled" in AGENT_BASELINE

    def test_a_failed_tool_is_not_evidence_about_the_target(self) -> None:
        """Four voices read one ModuleNotFoundError as four missing features."""
        assert "tells you about this environment" in AGENT_BASELINE

    def test_it_says_to_batch_lookups_rather_than_naming_absent_tools(self) -> None:
        """`Grep` and `Glob` are not registered in an SDK session.

        A live probe's ``init`` event lists what a session built with
        Conductor's flags offers, and neither is in it.
        """
        assert "Send independent lookups together" in AGENT_BASELINE
        assert "Grep" not in AGENT_BASELINE
        assert "Glob" not in AGENT_BASELINE


def test_a_budget_mode_in_composition_survives_a_config_that_is_silent() -> None:
    """It used to be overwritten rather than conflicted with, so every pipeline
    asking for `enforce` emitted `audit` and nothing said so. The budget was
    recorded and never enforced on any of them."""
    pipeline = Pipeline(pipeline_id="p", budget_usd=1.0, budget_mode="enforce")
    PipelineConfig(provider="claude-agent-sdk").apply(pipeline, where="config.yaml")
    assert pipeline.budget_mode == "enforce"


def test_a_config_that_disagrees_about_the_budget_mode_says_so() -> None:
    """Reported like every other policy field, rather than resolved in silence."""
    pipeline = Pipeline(pipeline_id="p", budget_usd=1.0, budget_mode="enforce")
    config = PipelineConfig(provider="claude-agent-sdk", budget_mode="audit")
    with pytest.raises(ConfigError, match="budget_mode"):
        config.apply(pipeline, where="config.yaml")


def test_a_config_may_still_set_the_budget_mode() -> None:
    pipeline = Pipeline(pipeline_id="p", budget_usd=1.0)
    PipelineConfig(provider="claude-agent-sdk", budget_mode="enforce").apply(
        pipeline, where="config.yaml"
    )
    assert pipeline.budget_mode == "enforce"


# --- native_tools as a list: reading without a shell --------------------------


def test_a_tool_list_is_read_from_config() -> None:
    """`claude_code` grants a shell along with the reading. Naming the tools is
    how a step gets to look at a repository without being able to run it."""
    config = read_config_text("provider: claude-agent-sdk\nnative_tools: [Read, Grep, Glob]\n")
    assert config.native_tools == ("Read", "Grep", "Glob")


def test_an_empty_tool_list_is_refused() -> None:
    """Granting nothing is spelled `none`; an empty list reads as half-written."""
    with pytest.raises(ConfigError, match="empty list"):
        read_config_text("provider: claude-agent-sdk\nnative_tools: []\n")


def test_the_two_words_still_work() -> None:
    assert read_config_text("provider: claude-agent-sdk\n").native_tools == "none"
    assert (
        read_config_text("provider: claude-agent-sdk\nnative_tools: claude_code\n").native_tools
        == "claude_code"
    )


def test_a_tool_list_is_emitted_as_a_list() -> None:
    """A tuple is how ictus keeps a pipeline hashable, not the engine's spelling."""
    pipeline = Pipeline(pipeline_id="p", provider="claude-agent-sdk")
    pipeline.native_tools = ("Read", "Grep", "Glob")
    pipeline.add(AgentNode(node_id="only", prompt="x"))
    pipeline.set_entry(pipeline.nodes[0])
    workflow = conductor.document(pipeline)["workflow"]
    assert isinstance(workflow, dict)
    runtime = workflow["runtime"]
    assert isinstance(runtime, dict)
    block = runtime["provider"]
    assert isinstance(block, dict)
    assert block["native_tools"] == ["Read", "Grep", "Glob"]


def read_config_text(text: str) -> PipelineConfig:
    """Parse config text the way a folder's `config.yaml` is parsed."""
    import tempfile

    with tempfile.TemporaryDirectory() as into:
        path = Path(into) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return read_config(path)
