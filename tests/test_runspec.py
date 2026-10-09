"""The running contract: a pipeline folder, its input file, and where it runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl
from ictus.errors import CompositionError
from ictus.runspec.inputs import PipelineFolder, RunSpecError, read_input_file, split_frontmatter

if TYPE_CHECKING:
    from pathlib import Path

STR = PortType.STRING


def _pipeline(*, prose: bool = True, extra_required: bool = False) -> Pipeline:
    p = Pipeline(pipeline_id="demo")
    charge = p.declare_input("charge", STR, required=False, prose=prose)
    target = p.declare_input("target", STR, required=extra_required)
    node = p.add(
        AgentNode(
            node_id="work",
            inputs=(InputPort("charge", STR, optional=True), InputPort("target", STR)),
            prompt=tpl("do ", target.ref()),
            declared_outputs=(OutputPort("v", STR),),
        )
    )
    p.set_entry(node)
    p.connect_input(charge, node, "charge")
    p.connect_input(target, node, "target")
    p.route(node, END)
    return p


def _folder(tmp_path: Path, body: str = "") -> Path:
    folder = tmp_path / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pipeline.py").write_text("# placeholder\n")
    if body:
        (folder / "input.md").write_text(body)
    return folder


# --- the folder ------------------------------------------------------------


def test_a_folder_needs_a_pipeline_module(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(RunSpecError, match=r"pipeline\.py"):
        PipelineFolder.at(bare)


def test_a_folder_is_found_directly_or_as_a_container(tmp_path: Path) -> None:
    """`lint pipelines/` and `lint pipelines/one` should both mean the obvious thing."""
    _folder(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "pipeline.py").write_text("# placeholder\n")
    assert [f.name for f in PipelineFolder.find(tmp_path)] == ["demo", "other"]
    assert [f.name for f in PipelineFolder.find(tmp_path / "demo")] == ["demo"]


def test_a_directory_of_nothing_says_what_it_expected(tmp_path: Path) -> None:
    with pytest.raises(RunSpecError, match="no pipeline folders"):
        PipelineFolder.find(tmp_path)


def test_build_sits_beside_the_pipeline(tmp_path: Path) -> None:
    folder = PipelineFolder.at(_folder(tmp_path))
    assert folder.build == folder.root / "build"
    assert folder.input_file == folder.root / "input.md"


# --- the input file --------------------------------------------------------


def test_frontmatter_and_body_split() -> None:
    front, body = split_frontmatter("---\na: 1\n---\nthe body\n", where="x")
    assert front == {"a": 1}
    assert body == "the body"


def test_a_thematic_break_further_down_is_body_not_metadata() -> None:
    front, body = split_frontmatter("no frontmatter\n\n---\n\nstill body", where="x")
    assert front == {}
    assert body.startswith("no frontmatter")


def test_an_empty_frontmatter_block_is_a_block_not_a_body() -> None:
    """`---\\n---\\n` is how a pipeline with no inputs says so.

    A failed match makes the delimiters themselves the body.
    """
    front, body = split_frontmatter("---\n---\n", where="x")
    assert front == {}
    assert body == ""


def test_a_closing_delimiter_must_start_its_own_line() -> None:
    """The guard the empty case must not cost: `foo---` does not close a block."""
    front, body = split_frontmatter("---\nfoo---\n", where="x")
    assert front == {}
    assert body == "---\nfoo---"


def test_a_pipeline_with_no_inputs_accepts_an_empty_frontmatter_file(tmp_path: Path) -> None:
    p = Pipeline(pipeline_id="demo")
    folder = _folder(tmp_path, "---\n---\n")
    spec = read_input_file(folder / "input.md", p, cwd=tmp_path)
    assert spec.inputs == {}


def test_the_body_feeds_the_input_declared_as_prose(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\ntarget: HEAD~1\n---\nShip it Friday.\nBe careful.\n")
    spec = read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path)
    assert spec.inputs["target"] == "HEAD~1"
    assert spec.inputs["charge"] == "Ship it Friday.\nBe careful."


def test_body_text_with_nowhere_to_go_is_refused(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\ntarget: x\n---\nsome prose\n")
    with pytest.raises(RunSpecError, match="declares no input to receive it"):
        read_input_file(folder / "input.md", _pipeline(prose=False), cwd=tmp_path)


def test_a_key_that_matches_no_input_is_refused(tmp_path: Path) -> None:
    """Silently dropping `targt:` is how a run does the default thing unnoticed."""
    folder = _folder(tmp_path, "---\ntargt: HEAD~1\n---\n")
    with pytest.raises(RunSpecError, match="'targt' is not an input"):
        read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path)


def test_a_missing_required_input_is_refused(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\ncharge: hello\n---\n")
    with pytest.raises(RunSpecError, match=r"requires \['target'\]"):
        read_input_file(folder / "input.md", _pipeline(extra_required=True), cwd=tmp_path)


def test_values_cross_the_wire_as_text(tmp_path: Path) -> None:
    """YAML makes `3` an int and `true` a bool; Conductor takes -i name=value."""
    p = Pipeline(pipeline_id="demo")
    p.declare_input("target", STR)
    p.declare_input("charge", STR, required=False)
    folder = _folder(tmp_path, "---\ntarget: 3\ncharge: true\n---\n")
    spec = read_input_file(folder / "input.md", p, cwd=tmp_path)
    assert spec.inputs == {"target": "3", "charge": "true"}


def test_broken_frontmatter_names_the_file_and_the_usual_cause(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\ntarget: Fix this: now\n---\n")
    with pytest.raises(RunSpecError, match="block scalar"):
        read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path)


# --- where the work happens ------------------------------------------------


def test_without_a_repo_key_the_run_works_where_you_invoked_it(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\ntarget: x\n---\n")
    spec = read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path / "elsewhere")
    assert spec.working_dir == tmp_path / "elsewhere"


def test_a_relative_repo_resolves_against_the_input_file(tmp_path: Path) -> None:
    """Not the process cwd: a committed value must mean the same from anywhere."""
    project = tmp_path / "project"
    project.mkdir()
    folder = _folder(tmp_path, "---\nrepo: ../project\ntarget: x\n---\n")
    spec = read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path / "somewhere-else")
    assert spec.working_dir == project.resolve()


def test_a_repo_that_is_not_there_is_refused(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "---\nrepo: ../nope\ntarget: x\n---\n")
    with pytest.raises(RunSpecError, match="is not a directory"):
        read_input_file(folder / "input.md", _pipeline(), cwd=tmp_path)


def test_repo_reaches_the_pipeline_when_it_declares_one(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    p = Pipeline(pipeline_id="demo")
    p.declare_input("repo", STR)
    folder = _folder(tmp_path, "---\nrepo: ../project\n---\n")
    spec = read_input_file(folder / "input.md", p, cwd=tmp_path)
    assert spec.inputs["repo"] == str(project.resolve())


# --- the prose declaration -------------------------------------------------


def test_only_one_input_can_take_the_body() -> None:
    p = Pipeline(pipeline_id="demo")
    p.declare_input("a", STR, prose=True)
    with pytest.raises(CompositionError, match="already takes the input file's body"):
        p.declare_input("b", STR, prose=True)


def test_the_body_can_only_feed_a_string() -> None:
    p = Pipeline(pipeline_id="demo")
    with pytest.raises(CompositionError, match="cannot take an input file's body"):
        p.declare_input("a", PortType.OBJECT, prose=True)
