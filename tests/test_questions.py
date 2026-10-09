"""Asking a person for values the run could not work out.

A gate offers a decision; this collects values. Conductor fixes the output
shape, so the node type carries only what is legal on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import (
    END,
    AgentNode,
    CompositionError,
    InputPort,
    OutputPort,
    Pipeline,
    PortType,
    Question,
    QuestionsNode,
    tpl,
)
from ictus.graph.traversal import reachable_from_entry
from ictus.interfaces.conductor import ConductorBackend, conductor
from ictus.lint import lint_pipeline
from ictus.stdlib import ask_human, resolve_unknowns, succeed

if TYPE_CHECKING:
    from collections.abc import Callable

    from ictus.graph.values import YamlDict

S = PortType.STRING


def _asking(**kwargs: object) -> QuestionsNode:
    defaults: dict[str, object] = {
        "node_id": "ask",
        "questions": (Question(id="where", text="Where is it?"),),
    }
    return QuestionsNode(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestQuestionShape:
    def test_an_unanswerable_question_is_refused(self) -> None:
        """No choices and no free text leaves nothing the person can do."""
        with pytest.raises(CompositionError, match="unanswerable"):
            Question(text="pick", choices=(), allow_free_text=False)

    def test_choices_alone_are_answerable(self) -> None:
        assert Question(text="pick", choices=("a", "b"), allow_free_text=False).choices

    def test_an_id_must_be_an_identifier(self) -> None:
        """It becomes a key answers are read by."""
        with pytest.raises(CompositionError, match="not an identifier"):
            Question(id="Repo Path", text="where?")

    def test_empty_text_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="needs text"):
            Question(text="   ")


class TestNodeInvariants:
    def test_exactly_one_of_questions_or_source(self) -> None:
        with pytest.raises(CompositionError, match="exactly one"):
            QuestionsNode(node_id="ask")
        node = AgentNode(node_id="a", prompt="x", declared_outputs=(OutputPort("q", S),))
        with pytest.raises(CompositionError, match="exactly one"):
            QuestionsNode(node_id="ask", questions=(Question(text="t"),), source=node.ref("q"))

    def test_duplicate_answer_ids_are_refused(self) -> None:
        with pytest.raises(CompositionError, match="reuses answer id"):
            QuestionsNode(
                node_id="ask",
                questions=(Question(id="p", text="one"), Question(id="p", text="two")),
            )

    def test_a_named_question_becomes_a_typed_port(self) -> None:
        """`ask.ref("where")` is checked; a raw `answers.where` string is not."""
        node = _asking()
        assert node.get_output("where").port_type is S
        assert node.output_ref("where") == "answers.where"

    def test_the_fixed_shape_is_always_available(self) -> None:
        assert {"answers", "transcript", "answered_count", "outcome"} <= {
            p.name for p in _asking().outputs
        }

    def test_a_dynamic_node_has_only_the_fixed_shape(self) -> None:
        """Ids are unknown until it runs, so there is nothing else to declare."""
        node = AgentNode(node_id="a", prompt="x", declared_outputs=(OutputPort("q", S),))
        dynamic = QuestionsNode(node_id="ask", source=node.ref("q"))
        assert {p.name for p in dynamic.outputs} == {
            "answers",
            "transcript",
            "answered_count",
            "outcome",
        }


class TestEmission:
    @staticmethod
    def _pipeline() -> Pipeline:
        p = Pipeline(pipeline_id="t")
        ask = p.add(
            ask_human(
                node_id="ask",
                questions=(
                    Question(id="api", text="API repo path?", required=True, multiline=False),
                    Question(id="web", text="Web repo path?", choices=("~/src/web",)),
                ),
                allow_abort=True,
            )
        )
        work = p.add(
            AgentNode(
                node_id="work",
                inputs=(InputPort("api", S),),
                prompt=tpl("at ", ask.ref("api")),
                declared_outputs=(OutputPort("d", S),),
            )
        )
        gone = p.add(succeed(node_id="gone", reason="abandoned"))
        p.set_entry(ask)
        p.route(ask, work)
        p.abort_route(ask, gone)
        p.feed(ask, "api", work, "api")
        p.route(work, END)
        return p

    def _agent(self, name: str) -> YamlDict:
        agents = conductor.document(self._pipeline())["agents"]
        assert isinstance(agents, list)
        for candidate in agents:
            if isinstance(candidate, dict) and candidate.get("name") == name:
                return candidate
        raise AssertionError(f"no agent named {name!r} was emitted")

    def test_questions_emit_in_conductors_shape(self) -> None:
        assert self._agent("ask")["questions"] == [
            {"text": "API repo path?", "id": "api", "required": True, "multiline": False},
            {"text": "Web repo path?", "id": "web", "choices": ["~/src/web"]},
        ]

    def test_an_answer_is_read_through_the_answers_map(self) -> None:
        assert self._agent("work")["prompt"] == "at {{ ask.output.answers.api }}"
        assert self._agent("work")["input"] == ["ask.output.answers.api"]

    def test_abort_is_carried_once_not_twice(self) -> None:
        """Emitting it as a route too leaves a second unconditional route."""
        entry = self._agent("ask")
        assert entry["abort_route"] == "gone"
        assert entry["routes"] == [{"to": "work"}]

    def test_the_abort_target_is_reachable(self) -> None:
        """Without the abort edge in the graph it would look orphaned."""
        p = self._pipeline()
        assert "gone" in reachable_from_entry(p)
        assert lint_pipeline(p, backend=conductor) == []

    def test_it_loads(self, validates: Callable[[Pipeline], None]) -> None:
        validates(self._pipeline())


class TestResolveUnknowns:
    @staticmethod
    def _stage() -> Pipeline:
        return resolve_unknowns(
            subject="Jira ticket",
            needs=("the path of every repository this ticket touches",),
        ).body

    def test_it_asks_only_when_something_is_missing(self) -> None:
        """Stopping to ask about things it already knows trains people to skip it."""
        body = self._stage()
        identify = next(n for n in body.nodes if n.node_id == "identify")
        routes = [(e.describe_target, e.when is not None) for e in body.outgoing(identify)]
        assert routes == [("ready", True), ("ask", False)]

    def test_the_questions_come_from_the_identifying_step(self) -> None:
        ask = next(n for n in self._stage().nodes if n.node_id == "ask")
        assert isinstance(ask, QuestionsNode)
        assert ask.source is not None
        assert ask.source.source_id == "identify"

    def test_the_source_is_a_path_not_an_interpolation(self) -> None:
        """The engine resolves it itself; braces would hand it a literal."""
        agents = conductor.document(self._stage())["agents"]
        assert isinstance(agents, list)
        ask = next(a for a in agents if isinstance(a, dict) and a["name"] == "ask")
        assert ask["source"] == "identify.output.missing"

    def test_answers_are_defaulted_for_the_path_that_never_asked(self) -> None:
        emitted = ConductorBackend().document(self._stage())["output"]
        assert isinstance(emitted, dict)
        # The guard, not `| default()`: on the path that never asked, `ask` is
        # absent from context entirely and the attribute chain raises before any
        # filter runs. Both branches emit the same JSON so the type is stable.
        assert emitted["answers"] == (
            "{% if ask is defined %}{{ ask.output.answers | tojson }}{% else %}{}{% endif %}"
        )

    def test_an_empty_needs_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one thing"):
            resolve_unknowns(subject="x", needs=())

    def test_it_is_clean_and_loads(self, validates: Callable[[Pipeline], None]) -> None:
        assert lint_pipeline(self._stage(), backend=conductor) == []
        validates(self._stage())
