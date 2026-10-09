"""Writing a step's output to a file, built out of a `script` step.

Two ways it could be quietly wrong: a model's output reaching a shell as
source, and a declared output schema the engine rejects only after the file
has been written.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl
from ictus.errors import CompositionError
from ictus.interfaces.conductor import conductor
from ictus.lint import lint_pipeline
from ictus.stdlib import save_text, succeed

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ictus.graph.values import YamlDict

STR = PortType.STRING


def _pipeline(**kwargs: object) -> Pipeline:
    p = Pipeline(pipeline_id="saver", provider="claude-agent-sdk")
    name = p.declare_input("name", STR)
    draft = p.add(
        AgentNode(node_id="draft", prompt="write it", declared_outputs=(OutputPort("text", STR),))
    )
    save = p.add(
        save_text(
            node_id="save",
            text=draft.ref("text"),
            to=tpl("out/", name.ref(), ".md"),
            inputs=(InputPort("text", STR), InputPort("name", STR)),
            **kwargs,  # type: ignore[arg-type]
        )
    )
    done = p.add(
        succeed(
            node_id="done",
            reason="written",
            inputs=(InputPort("path", STR),),
            result={"path": tpl(save.ref("path"))},
        )
    )
    p.set_entry(draft)
    p.connect(draft, "text", save, "text")
    p.connect_input(name, save, "name")
    p.connect(save, "path", done, "path")
    return p


def _agent(pipeline: Pipeline, node_id: str) -> YamlDict:
    agents = conductor.document(pipeline)["agents"]
    assert isinstance(agents, list)
    for candidate in agents:
        if isinstance(candidate, dict) and candidate.get("name") == node_id:
            return candidate
    raise AssertionError(node_id)


def test_the_text_travels_on_stdin_never_in_the_command_line() -> None:
    """A model's output spliced into `sh -c` would make it executable."""
    save = _agent(_pipeline(), "save")
    assert save["stdin"] == "{{ draft.output.text }}"
    args = save["args"]
    assert isinstance(args, list)
    assert not any("draft.output.text" in str(a) for a in args)


def test_the_path_travels_as_an_argument_not_as_shell_source() -> None:
    args = _agent(_pipeline(), "save")["args"]
    assert isinstance(args, list)
    # -c, the script, $0, then the path — the script text never interpolates it.
    assert args[0] == "-c"
    assert args[2] == "sh"
    assert args[3] == "out/{{ workflow.input.name }}.md"


def test_the_path_can_carry_a_value_from_the_run() -> None:
    """One file per item in a fan-out, or a name taken from the ticket."""
    args = _agent(_pipeline(), "save")["args"]
    assert isinstance(args, list)
    assert "{{ workflow.input.name }}" in str(args[3])


def test_it_declares_the_written_path_as_a_typed_output() -> None:
    save = _agent(_pipeline(), "save")
    assert save["output"] == {
        "path": {"type": "string", "description": "The file that was written"}
    }


def test_somewhere_to_write_is_required() -> None:
    with pytest.raises(CompositionError, match="needs somewhere to write"):
        save_text(node_id="s", text="x", to="   ")


def test_it_is_lint_clean_and_loads(validates: Callable[[Pipeline], None]) -> None:
    pipeline = _pipeline()
    assert lint_pipeline(pipeline, backend=conductor) == []
    validates(pipeline)


# --- the shell command itself ----------------------------------------------
#
# Emitted YAML being right is not the same as the command working. These run it.


def _script(pipeline: Pipeline) -> list[str]:
    args = _agent(pipeline, "save")["args"]
    assert isinstance(args, list)
    return [str(a) for a in args]


@pytest.mark.parametrize(
    "payload",
    [
        "plain text\n",
        'shell metacharacters: $HOME `whoami` $(id) "quotes" \\backslash\n',
        "unicode: café — naïve\n",
        "",
    ],
)
def test_the_command_writes_exactly_what_it_was_given(tmp_path: Path, payload: str) -> None:
    """The payload is data. If any of it were shell source, these would differ."""
    script = _script(_pipeline())
    target = "out/nested/report.md"
    done = subprocess.run(
        ["sh", script[0], script[1], "sh", target],
        input=payload,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert (tmp_path / target).read_text() == payload
    assert json.loads(done.stdout) == {"path": target}


def test_the_command_creates_parent_directories(tmp_path: Path) -> None:
    """Otherwise a council that cost real money loses its report on a missing dir."""
    script = _script(_pipeline())
    subprocess.run(
        ["sh", script[0], script[1], "sh", "a/b/c/report.md"],
        input="hi",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert (tmp_path / "a/b/c/report.md").read_text() == "hi"


def test_stdout_is_valid_json_even_for_an_awkward_path(tmp_path: Path) -> None:
    """A declared output schema makes stdout a contract; Conductor parses it."""
    script = _script(_pipeline())
    target = 'out/a b/re"port.md'
    done = subprocess.run(
        ["sh", script[0], script[1], "sh", target],
        input="hi",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert json.loads(done.stdout) == {"path": target}


def test_append_adds_rather_than_replaces(tmp_path: Path) -> None:
    script = _script(_pipeline(append=True))
    for line in ("one\n", "two\n"):
        subprocess.run(
            ["sh", script[0], script[1], "sh", "log.md"],
            input=line,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=True,
        )
    assert (tmp_path / "log.md").read_text() == "one\ntwo\n"


def test_route_to_end_is_still_possible() -> None:
    p = Pipeline(pipeline_id="s2", provider="claude-agent-sdk")
    node = p.add(save_text(node_id="save", text="hello", to="a.txt"))
    p.set_entry(node)
    p.route(node, END)
    assert lint_pipeline(p, backend=conductor) == []
