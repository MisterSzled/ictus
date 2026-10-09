"""Lints that are true because of how Conductor runs.

Every graph below passes ``conductor validate``, and none is reported without
a backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ictus import (
    END,
    AgentNode,
    Backoff,
    CompositionError,
    ContextTier,
    InputPort,
    OutputPort,
    Pipeline,
    PortType,
    ReasoningEffort,
    RetryOn,
    RetryPolicy,
    ScriptNode,
    Validator,
)
from ictus.graph.composition import TrimStrategy
from ictus.interfaces.conductor import conductor
from ictus.interfaces.conductor.lints import (
    HONOURED_BY,
    HONOURED_EVERYWHERE,
    VALIDATED_UPSTREAM,
)
from ictus.lint import lint_pipeline
from ictus.stdlib import Voice, approval_gate, constant, council, succeed

if TYPE_CHECKING:
    from ictus.graph.values import YamlDict

S = PortType.STRING


def _problems(pipeline: Pipeline, fragment: str) -> list[str]:
    return [p for p in lint_pipeline(pipeline, backend=conductor) if fragment in p]


def test_a_backendless_lint_reports_none_of_these() -> None:
    """The boundary itself: engine rules arrive with an engine, not by default."""
    p = Pipeline(pipeline_id="t")
    node = p.add(AgentNode(node_id="a", prompt="{{ workflow.input.nope }}"))
    p.route(node, END)
    assert lint_pipeline(p) == []
    assert lint_pipeline(p, backend=conductor)


def test_unknown_output_field_in_a_template_is_reported() -> None:
    """Conductor checks the agent segment of a reference, never the field segment."""
    p = Pipeline(pipeline_id="t")
    src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("real", S),)))
    dst = p.add(
        AgentNode(node_id="dst", inputs=(InputPort("v", S),), prompt="{{ src.output.typo }}")
    )
    p.connect(src, "real", dst, "v")
    p.route(dst, END)
    assert _problems(p, "src.output.typo")


def test_known_output_field_passes() -> None:
    p = Pipeline(pipeline_id="t")
    src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("real", S),)))
    dst = p.add(
        AgentNode(node_id="dst", inputs=(InputPort("v", S),), prompt="{{ src.output.real }}")
    )
    p.connect(src, "real", dst, "v")
    p.route(dst, END)
    assert lint_pipeline(p, backend=conductor) == []


def test_undeclared_workflow_input_in_a_template_is_reported() -> None:
    p = Pipeline(pipeline_id="t")
    node = p.add(AgentNode(node_id="a", prompt="{{ workflow.input.nope }}"))
    p.route(node, END)
    assert _problems(p, "references workflow input 'nope'")


def test_reserved_output_name_is_reported() -> None:
    """A field named for Conductor's wrapper reads back empty at run time."""
    p = Pipeline(pipeline_id="t")
    node = p.add(AgentNode(node_id="a", prompt="x", declared_outputs=(OutputPort("outputs", S),)))
    p.route(node, END)
    assert _problems(p, "collides with Conductor's output wrapper")


class TestDeferredReferences:
    """A reference to a node that may not have run yet must be guarded.

    The ``?`` suffix makes the dependency optional, not the Jinja variable.
    Passes ``conductor validate``; shows up only in a live run.
    """

    @staticmethod
    def _loop(prompt: str) -> Pipeline:
        p = Pipeline(pipeline_id="t", loop_passes=2)
        draft = p.add(
            AgentNode(
                node_id="draft",
                inputs=(InputPort("notes", S, optional=True),),
                prompt=prompt,
                declared_outputs=(OutputPort("text", S),),
            )
        )
        gate = p.add(approval_gate(node_id="review", prompt="ok?", inputs=(InputPort("text", S),)))
        done = p.add(succeed(node_id="done", reason="d"))
        p.set_entry(draft)
        p.connect(draft, "text", gate, "text")
        p.branch(gate, {"approved": done, "rejected": draft})
        p.feed(gate, "notes", draft, "notes")
        return p

    def test_unguarded_back_edge_reference_is_reported(self) -> None:
        p = self._loop("Revise using {{ review.output.additional_input.notes }}")
        assert _problems(p, "may not have run yet")

    def test_guarded_reference_is_accepted(self) -> None:
        p = self._loop(
            "Write it."
            "{% if review is defined %}"
            "Revise using {{ review.output.additional_input.notes }}"
            "{% endif %}"
        )
        assert lint_pipeline(p, backend=conductor) == []

    def test_the_gate_output_shape_is_understood(self) -> None:
        """``additional_input`` is fixed by Conductor, not declared by the author."""
        p = self._loop(
            "{% if review is defined %}{{ review.output.additional_input.notes }}{% endif %}"
        )
        assert lint_pipeline(p, backend=conductor) == []

    def test_a_reference_that_always_precedes_needs_no_guard(self) -> None:
        p = Pipeline(pipeline_id="t")
        src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("v", S),)))
        dst = p.add(
            AgentNode(node_id="dst", inputs=(InputPort("v", S),), prompt="{{ src.output.v }}")
        )
        p.connect(src, "v", dst, "v")
        p.route(dst, END)
        assert lint_pipeline(p, backend=conductor) == []


class TestUndeclaredReferences:
    """Under ``context.mode: explicit`` a node sees only what its ``input:`` names.

    Conductor cannot catch it: the reference is well-formed and the agent exists.
    """

    @staticmethod
    def _referencing(*, declare: bool) -> Pipeline:
        p = Pipeline(pipeline_id="t")
        src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("v", S),)))
        end = p.add(
            succeed(
                node_id="fin",
                reason="got {{ src.output.v }}",
                inputs=(InputPort("v", S),) if declare else (),
            )
        )
        p.route(src, end)
        if declare:
            p.feed(src, "v", end, "v")
        return p

    def test_an_undeclared_reference_is_reported(self) -> None:
        assert _problems(self._referencing(declare=False), "does not declare it as an input")

    def test_declaring_it_clears_the_problem(self) -> None:
        assert lint_pipeline(self._referencing(declare=True), backend=conductor) == []

    def test_accumulate_mode_is_not_flagged(self) -> None:
        """The rule is a consequence of explicit scoping, not of graphs."""
        p = Pipeline(pipeline_id="t", context_mode="accumulate")
        src = p.add(AgentNode(node_id="src", prompt="x", declared_outputs=(OutputPort("v", S),)))
        end = p.add(succeed(node_id="fin", reason="got {{ src.output.v }}"))
        p.route(src, end)
        assert lint_pipeline(p, backend=conductor) == []

    def test_a_group_member_may_be_named_through_its_group(self) -> None:
        p = Pipeline(pipeline_id="t")
        a = p.add(AgentNode(node_id="a", prompt="x", declared_outputs=(OutputPort("ok", S),)))
        b = p.add(AgentNode(node_id="b", prompt="x", declared_outputs=(OutputPort("ok", S),)))
        group = p.parallel("both", [a, b])
        reader = p.add(
            AgentNode(
                node_id="reader",
                inputs=(InputPort("v", S),),
                prompt="{{ both.outputs.a.ok }}",
                declared_outputs=(OutputPort("seen", S),),
            )
        )
        done = p.add(succeed(node_id="done", reason="d"))
        p.set_entry(group)
        p.route(group, reader)
        p.feed(a, "ok", reader, "v")
        p.route(reader, done)
        assert lint_pipeline(p, backend=conductor) == []


def _with(provider: str, **agent_fields: object) -> Pipeline:
    p = Pipeline(pipeline_id="t", provider=provider)
    node = p.add(AgentNode(node_id="a", prompt="x", **agent_fields))  # type: ignore[arg-type]
    p.route(node, END)
    return p


class TestFieldsTheProviderIgnores:
    """Conductor accepts these on any agent; only some providers act on them.

    No capability flag and no validator check, so a workflow setting one on a
    provider that ignores it loads, validates, runs and does nothing. The
    tables were read out of each provider's source, not out of the schema.
    """

    def test_retry_is_refused_on_the_provider_these_pipelines_use(self) -> None:
        """`claude-agent-sdk` is the only provider that never reads `agent.retry`."""
        problems = _problems(_with("claude-agent-sdk", retry=RetryPolicy()), "sets retry")
        assert problems
        assert "ignores" in problems[0]

    def test_retry_is_allowed_where_it_is_honoured(self) -> None:
        assert not _problems(_with("openai", retry=RetryPolicy()), "sets retry")
        assert not _problems(_with("copilot", retry=RetryPolicy()), "sets retry")

    def test_context_tier_is_refused_where_it_is_dropped(self) -> None:
        for provider in ("claude-agent-sdk", "claude", "openai", "hermes"):
            assert _problems(_with(provider, context_tier=ContextTier.LONG), "sets context_tier"), (
                provider
            )

    def test_context_tier_is_allowed_on_both_providers_that_forward_it(self) -> None:
        """`aca` forwards it too, which the schema's own docstring does not say."""
        for provider in ("copilot", "aca"):
            assert not _problems(
                _with(provider, context_tier=ContextTier.LONG), "sets context_tier"
            ), provider

    def test_an_unset_field_is_never_reported(self) -> None:
        assert not _problems(_with("claude-agent-sdk"), "ignores")

    def test_the_message_names_who_would_honour_it(self) -> None:
        """A refusal nobody can act on is just a failure."""
        problem = _problems(_with("claude-agent-sdk", retry=RetryPolicy()), "sets retry")[0]
        assert "'aca'" in problem and "'openai'" in problem
        assert "claude-agent-sdk" in problem


class TestRetryPolicyEmission:
    """Ours are English names; Conductor's spelling is the emitter's business."""

    def _agent(self, **fields: object) -> YamlDict:
        p = _with("copilot", **fields)
        agents = conductor.document(p)["agents"]
        assert isinstance(agents, list)
        entry = agents[0]
        assert isinstance(entry, dict)
        return entry

    def test_it_emits_conductors_field_names(self) -> None:
        emitted = self._agent(
            retry=RetryPolicy(
                attempts=4,
                backoff=Backoff.FIXED,
                first_delay_seconds=1.5,
                on=(RetryOn.TIMEOUT,),
            )
        )
        assert emitted["retry"] == {
            "max_attempts": 4,
            "backoff": "fixed",
            "delay_seconds": 1.5,
            "retry_on": ["timeout"],
        }

    def test_an_empty_category_list_leaves_the_engines_own(self) -> None:
        """`retry_on` absent means the engine's set; `[]` would mean retry nothing."""
        emitted = self._agent(retry=RetryPolicy(attempts=2))
        assert isinstance(emitted["retry"], dict)
        assert "retry_on" not in emitted["retry"]
        assert "delay_seconds" not in emitted["retry"]

    def test_context_tier_emits_its_value(self) -> None:
        assert self._agent(context_tier=ContextTier.LONG)["context_tier"] == "long_context"


class TestRetryPolicyRefusesNonsense:
    """Made unrepresentable rather than checked at the boundary."""

    @pytest.mark.parametrize("attempts", [0, 11, -1])
    def test_an_impossible_attempt_count_is_refused(self, attempts: int) -> None:
        with pytest.raises(CompositionError, match="1 to 10 attempts"):
            RetryPolicy(attempts=attempts)

    def test_a_non_positive_delay_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="must be positive"):
            RetryPolicy(first_delay_seconds=0)

    def test_a_repeated_category_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="repeats a failure category"):
            RetryPolicy(on=(RetryOn.TIMEOUT, RetryOn.TIMEOUT))


class TestHonouredByIsWellFormed:
    """The table is research, and research rots. These keep it honest.

    Every entry comes from a provider's ``CAPABILITIES`` or the code consuming
    the value, never from ``config/schema.py``.
    """

    def test_every_named_provider_is_one_conductor_has(self) -> None:
        """A typo here silently widens a refusal into a provider nobody runs."""
        known = conductor.capabilities().providers
        for field, providers in HONOURED_BY.items():
            assert providers <= known, f"{field} names {sorted(providers - known)}"

    def test_no_field_is_both_restricted_and_universal(self) -> None:
        assert not set(HONOURED_BY) & HONOURED_EVERYWHERE

    def test_a_universal_field_is_never_refused(self) -> None:
        """`timeout_seconds` and `validator` are engine-level, so they hold anywhere."""
        for field in HONOURED_EVERYWHERE:
            assert field not in HONOURED_BY

    def test_nothing_is_honoured_by_every_provider(self) -> None:
        """Such an entry belongs in HONOURED_EVERYWHERE, where it costs no lint."""
        known = conductor.capabilities().providers
        for field, providers in HONOURED_BY.items():
            assert providers != known, f"{field} is universal; move it"

    def test_the_two_wired_fields_are_covered(self) -> None:
        """The rest are inert until wired; these two can be written today."""
        assert "retry" in HONOURED_BY
        assert "context_tier" in HONOURED_BY


class TestTheProviderInUse:
    """What `claude-agent-sdk` — every demo pipeline's provider — actually drops."""

    def test_retry_is_dropped_silently_so_ictus_refuses_it(self) -> None:
        assert "claude-agent-sdk" not in HONOURED_BY["retry"]

    def test_reasoning_is_dropped_loudly_so_conductor_refuses_it(self) -> None:
        """Same provider, same absence, different guard — hence two tables."""
        assert "reasoning" in VALIDATED_UPSTREAM
        assert "reasoning" not in HONOURED_BY

    def test_but_it_does_take_working_dir_skills_and_plugins(self) -> None:
        for field in ("working_dir", "skills", "plugins"):
            assert "claude-agent-sdk" in HONOURED_BY[field], field

    def test_the_demo_pipelines_all_use_it(self) -> None:
        """If that changes, the two lints above stop describing what you run."""
        folders = sorted(Path("demo_work/pipelines").glob("*/config.yaml"))
        assert folders
        for config in folders:
            assert "claude-agent-sdk" in config.read_text(encoding="utf-8"), config


class TestRelativePathsOnAnAgent:
    """A relative path on an agent resolves against `build/`, which is output.

    ``working_dir``, ``skills`` and ``plugins`` all resolve against the emitted
    workflow's own directory (engine/workflow.py, ``_resolve_agent_working_dir``),
    which ``ictus emit``
    rewrites and prunes.
    """

    def test_a_relative_working_dir_is_refused(self) -> None:
        problems = _problems(_with("claude-agent-sdk", working_dir="../project"), "build/")
        assert problems
        assert "resolves against" in problems[0]

    def test_an_absolute_or_home_relative_one_is_fine(self) -> None:
        for value in ("/srv/project", "~/work/project"):
            assert not _problems(_with("claude-agent-sdk", working_dir=value), "build/"), value

    def test_a_templated_one_is_left_alone(self) -> None:
        """Rendered at run time, so ictus cannot know what it resolves to."""
        assert not _problems(
            _with("claude-agent-sdk", working_dir="{{ workflow.input.repo }}"), "build/"
        )

    def test_a_relative_skill_or_plugin_path_is_refused(self) -> None:
        assert _problems(_with("claude-agent-sdk", skills=("./skills/x",)), "build/")
        assert _problems(_with("claude-agent-sdk", plugins=("../plugins/p",)), "build/")

    def test_a_registered_name_is_not_a_path(self) -> None:
        """Conductor's own rule: a bare name can never be shadowed by a directory."""
        assert not _problems(_with("claude-agent-sdk", skills=("conductor",)), "build/")

    def test_a_script_step_keeps_its_relative_working_dir(self) -> None:
        """One Conductor field, two step kinds, two resolutions.

        A script's `working_dir` goes straight to the subprocess, so it
        resolves against the directory the run was launched from.
        """
        p = Pipeline(pipeline_id="t", provider="claude-agent-sdk")
        step = p.add(
            ScriptNode(node_id="s", command="sh", args=("x.sh",), working_dir="demo_work/scripts")
        )
        p.route(step, END)
        assert not _problems(p, "build/")


class TestSkillsAndPluginsTriState:
    """Same three states as `tools`, and the empty one has to survive emission."""

    def _agent(self, **fields: object) -> YamlDict:
        agents = conductor.document(_with("claude-agent-sdk", **fields))["agents"]
        assert isinstance(agents, list)
        entry = agents[0]
        assert isinstance(entry, dict)
        return entry

    def test_unset_omits_the_key_so_the_workflow_default_applies(self) -> None:
        emitted = self._agent()
        assert "skills" not in emitted
        assert "plugins" not in emitted

    def test_empty_emits_an_empty_list_which_denies_every_one(self) -> None:
        """`[]` and an omitted key are different instructions, not shades of one."""
        emitted = self._agent(skills=(), plugins=())
        assert emitted["skills"] == []
        assert emitted["plugins"] == []

    def test_named_entries_are_emitted_in_order(self) -> None:
        emitted = self._agent(skills=("conductor", "review"), plugins=("prs",))
        assert emitted["skills"] == ["conductor", "review"]
        assert emitted["plugins"] == ["prs"]

    def test_working_dir_reaches_the_engine(self) -> None:
        assert self._agent(working_dir="/srv/project")["working_dir"] == "/srv/project"

    def test_all_three_are_allowed_on_the_provider_these_pipelines_use(self) -> None:
        """The reason these three were wired and `retry`/`reasoning` were not."""
        clean = _with(
            "claude-agent-sdk", working_dir="/srv/p", skills=("conductor",), plugins=("prs",)
        )
        assert not _problems(clean, "ignores")


class TestUniversalLimits:
    """`timeout_seconds`, `max_session_seconds` and `validator` hold anywhere.

    Two are engine-level and the third is declared by every provider.
    """

    def _agent(self, **fields: object) -> YamlDict:
        agents = conductor.document(_with("claude-agent-sdk", **fields))["agents"]
        assert isinstance(agents, list)
        entry = agents[0]
        assert isinstance(entry, dict)
        return entry

    def test_no_provider_refuses_them(self) -> None:
        for provider in sorted(conductor.capabilities().providers):
            p = _with(
                provider,
                timeout_seconds=900,
                max_session_seconds=600,
                validator=Validator(criteria="cites a file"),
            )
            assert not _problems(p, "ignores"), provider

    def test_the_limits_reach_the_engine(self) -> None:
        emitted = self._agent(timeout_seconds=900, max_session_seconds=600)
        assert emitted["timeout_seconds"] == 900
        assert emitted["max_session_seconds"] == 600

    def test_a_validator_emits_its_rubric_and_its_retry_count(self) -> None:
        emitted = self._agent(validator=Validator(criteria="cites a file"))
        assert emitted["validator"] == {"criteria": "cites a file", "max_retries": 1}

    def test_declining_the_revision_still_runs_the_check(self) -> None:
        """`revise=False` reports without acting — one extra call, not two."""
        emitted = self._agent(validator=Validator(criteria="cites a file", revise=False))
        assert isinstance(emitted["validator"], dict)
        assert emitted["validator"]["max_retries"] == 0

    def test_a_cheaper_grader_is_passed_through(self) -> None:
        emitted = self._agent(validator=Validator(criteria="c", model="claude-haiku-4-5-20251001"))
        assert isinstance(emitted["validator"], dict)
        assert emitted["validator"]["model"] == "claude-haiku-4-5-20251001"

    def test_none_of_them_is_emitted_unset(self) -> None:
        emitted = self._agent()
        for key in ("timeout_seconds", "max_session_seconds", "validator"):
            assert key not in emitted


class TestLimitsRefuseNonsense:
    """Made unrepresentable rather than discovered on a run that cost money."""

    @pytest.mark.parametrize("field", ["timeout_seconds", "max_session_seconds"])
    def test_a_sub_second_ceiling_is_refused(self, field: str) -> None:
        """The engine's own floor is one second."""
        with pytest.raises(CompositionError, match="at least one second"):
            AgentNode(node_id="a", prompt="x", **{field: 0.5})  # type: ignore[arg-type]

    def test_a_session_budget_the_engine_beats_is_refused(self) -> None:
        """Otherwise it reads like a limit and can never fire."""
        with pytest.raises(CompositionError, match="before the provider's own budget"):
            AgentNode(node_id="a", prompt="x", timeout_seconds=120, max_session_seconds=300)

    def test_equal_values_are_refused_too(self) -> None:
        with pytest.raises(CompositionError, match="before the provider's own budget"):
            AgentNode(node_id="a", prompt="x", timeout_seconds=120, max_session_seconds=120)

    def test_a_session_budget_under_the_engines_is_fine(self) -> None:
        node = AgentNode(node_id="a", prompt="x", timeout_seconds=900, max_session_seconds=600)
        assert node.max_session_seconds == 600

    def test_either_alone_is_fine(self) -> None:
        assert AgentNode(node_id="a", prompt="x", timeout_seconds=60).timeout_seconds == 60
        assert AgentNode(node_id="b", prompt="x", max_session_seconds=60).max_session_seconds == 60

    def test_an_empty_rubric_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="needs criteria"):
            Validator(criteria="   ")


class TestReasoningEffort:
    """Per-node thinking budget, wired so a pipeline can vary it step by step.

    Each level is a token budget charged whether or not the step needed it, so
    there is no default.
    """

    def _agent(self, provider: str, **fields: object) -> YamlDict:
        agents = conductor.document(_with(provider, **fields))["agents"]
        assert isinstance(agents, list)
        entry = agents[0]
        assert isinstance(entry, dict)
        return entry

    def test_it_emits_conductors_nested_shape(self) -> None:
        emitted = self._agent("copilot", reasoning=ReasoningEffort.HIGH)
        assert emitted["reasoning"] == {"effort": "high"}

    def test_every_level_survives_emission(self) -> None:
        for level in ReasoningEffort:
            emitted = self._agent("copilot", reasoning=level)
            assert emitted["reasoning"] == {"effort": level.value}

    def test_unset_omits_the_key(self) -> None:
        """No default: each level is a token budget nobody asked for."""
        assert "reasoning" not in self._agent("copilot")

    def test_two_steps_can_differ_in_one_pipeline(self) -> None:
        """The whole reason it is per node rather than per workflow."""
        p = Pipeline(pipeline_id="t", provider="copilot")
        cheap = p.add(AgentNode(node_id="gather", prompt="x", reasoning=ReasoningEffort.LOW))
        hard = p.add(AgentNode(node_id="synth", prompt="y", reasoning=ReasoningEffort.MAX))
        p.route(cheap, hard)
        p.route(hard, END)
        agents = conductor.document(p)["agents"]
        assert isinstance(agents, list)
        by_name = {a["name"]: a for a in agents if isinstance(a, dict)}
        assert by_name["gather"]["reasoning"] == {"effort": "low"}
        assert by_name["synth"]["reasoning"] == {"effort": "max"}

    def test_ictus_does_not_lint_it(self) -> None:
        """Conductor refuses it itself, naming the provider and the levels."""
        assert "reasoning" in VALIDATED_UPSTREAM
        assert "reasoning" not in HONOURED_BY
        assert not _problems(_with("claude-agent-sdk", reasoning=ReasoningEffort.MAX), "ignores")


class TestTheTwoTablesStayDistinct:
    """`HONOURED_BY` is for silent no-ops; `VALIDATED_UPSTREAM` for caught ones.

    The entry criterion is whether anything upstream notices: `retry` on
    `claude-agent-sdk` validates clean and does nothing, `reasoning` is refused.
    """

    def test_no_field_appears_in_both(self) -> None:
        assert not set(HONOURED_BY) & VALIDATED_UPSTREAM

    def test_nor_in_the_universal_set(self) -> None:
        assert not VALIDATED_UPSTREAM & HONOURED_EVERYWHERE

    def test_an_upstream_checked_field_is_never_linted(self) -> None:
        for field in VALIDATED_UPSTREAM:
            assert field not in HONOURED_BY, f"{field} would duplicate conductor validate"


class TestContextCeiling:
    """A soft cap on accumulated context, and the one strategy that survives it.

    A deleted output is indistinguishable from one that has not run, so a loop
    reading it keeps rendering nothing.
    """

    def _looping(self, **kwargs: object) -> Pipeline:
        scope = council(
            stage_id="panel",
            voices=(
                Voice(node_id="a", persona="p", focus="f"),
                Voice(node_id="b", persona="q", focus="g"),
            ),
            rounds=3,
        )
        for key, value in kwargs.items():
            setattr(scope.body, key, value)
        scope.body.provider = "claude-agent-sdk"
        return scope.body

    def _flat(self, **kwargs: object) -> Pipeline:
        p = _with("claude-agent-sdk")
        for key, value in kwargs.items():
            setattr(p, key, value)
        return p

    def test_a_ceiling_and_a_strategy_are_emitted_together(self) -> None:
        p = self._flat(context_max_tokens=120_000, context_trim=TrimStrategy.TRUNCATE)
        block = conductor.document(p)["workflow"]
        assert isinstance(block, dict)
        assert block["context"] == {
            "mode": "explicit",
            "max_tokens": 120_000,
            "trim_strategy": "truncate",
        }

    def test_a_ceiling_with_no_strategy_is_refused(self) -> None:
        """The engine does not leave it unset — it uses the most destructive one."""
        problems = _problems(self._flat(context_max_tokens=120_000), "no context_trim")
        assert problems
        assert "drop_oldest" in problems[0]

    def test_a_strategy_with_no_ceiling_is_refused(self) -> None:
        """Nothing ever trims, so the strategy is never reached."""
        p = self._flat(context_trim=TrimStrategy.TRUNCATE)
        assert _problems(p, "context_max_tokens is not")

    @pytest.mark.parametrize("strategy", [TrimStrategy.DROP_OLDEST, TrimStrategy.SUMMARIZE])
    def test_a_deleting_strategy_is_refused_on_a_loop(self, strategy: TrimStrategy) -> None:
        problems = _problems(
            self._looping(context_max_tokens=120_000, context_trim=strategy), "graph that loops"
        )
        assert problems
        assert "stops deliberating" in problems[0]

    def test_truncate_is_allowed_on_a_loop(self) -> None:
        """It shortens fields in place, so every reference still resolves."""
        p = self._looping(context_max_tokens=120_000, context_trim=TrimStrategy.TRUNCATE)
        assert not _problems(p, "context_max_tokens")

    def test_a_deleting_strategy_is_fine_without_a_loop(self) -> None:
        """Nothing reads a previous pass, so nothing can be emptied behind it."""
        p = self._flat(context_max_tokens=120_000, context_trim=TrimStrategy.DROP_OLDEST)
        assert not _problems(p, "graph that loops")


class TestRetypedConstants:
    """Conductor YAML-loads a bare ``value:``, and the node still declares string.

    The gap this closes: ``constant(value="3")`` emits a node declaring
    ``string`` and binds the integer ``3``, so a route comparing it to ``"3"``
    is comparing two types and is never true. Nothing said so.
    """

    def _pipeline(self, value: str, **kw: object) -> Pipeline:
        p = Pipeline(pipeline_id="t", description="d")
        node = p.add(constant(node_id="c", value=value, **kw))  # type: ignore[arg-type]
        done = p.add(succeed(node_id="done", reason="x"))
        p.set_entry(node)
        p.route(node, done)
        return p

    def _problems(self, value: str, **kw: object) -> list[str]:
        return [
            m
            for m in lint_pipeline(self._pipeline(value, **kw), backend=conductor)
            if "output_type" in m
        ]

    @pytest.mark.parametrize(
        ("value", "becomes"),
        [("3", "int"), ("true", "bool"), ("1.5", "float"), ("null", "NoneType")],
    )
    def test_a_literal_that_retypes_is_refused(self, value: str, becomes: str) -> None:
        found = self._problems(value)
        assert found, f"{value!r} binds a {becomes} and was not refused"
        assert becomes in found[0]

    @pytest.mark.parametrize("value", ["approved", "a whole sentence", "CHANGE_ME"])
    def test_free_text_is_left_alone(self, value: str) -> None:
        assert not self._problems(value)

    @pytest.mark.parametrize("value", ["no", "yes", "on", "off"])
    def test_yaml_1_1_booleans_are_not_refused(self, value: str) -> None:
        """Conductor's loader is ``typ="safe", pure=True`` — YAML 1.2.

        These four are the reason the rule asks the engine's loader rather than
        carrying a word list. Under YAML 1.1 they are booleans; here they are
        strings, and refusing them would be refusing correct pipelines.
        """
        assert not self._problems(value)

    def test_declaring_the_type_settles_it(self) -> None:
        assert not self._problems("3", output_type=PortType.NUMBER)

    def test_a_template_is_not_guessed_at(self) -> None:
        """What a reference renders to is not knowable while the graph is written."""
        assert not self._problems("{{ other.output.value }}")
