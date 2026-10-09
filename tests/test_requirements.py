"""Declared requirements and the preflight that enforces them.

``validate`` asks whether a workflow is well-formed and must pass with no
credentials at all; ``preflight`` asks whether this machine can run it.
"""

from __future__ import annotations

import pytest

from ictus import (
    END,
    AgentNode,
    CompositionError,
    EnvVar,
    Executable,
    InputPort,
    McpServer,
    McpTransport,
    OutputPort,
    Pipeline,
    PortType,
    Stage,
)
from ictus.interfaces.conductor import conductor
from ictus.interfaces.conductor.emit.mcp import mcp_servers_block

STR = PortType.STRING


def _pipeline(*servers: McpServer) -> Pipeline:
    p = Pipeline(pipeline_id="t")
    node = p.add(AgentNode(node_id="a", prompt="x"))
    p.route(node, END)
    for server in servers:
        p.require_mcp(server)
    return p


def _stdio(
    name: str = "svc",
    command: str = "definitely-not-installed-xyz",
    env: tuple[EnvVar, ...] = (),
    setup_hint: str = "",
) -> McpServer:
    return McpServer(
        name=name,
        purpose="do a thing",
        transport=McpTransport.STDIO,
        command=command,
        env=env,
        setup_hint=setup_hint,
    )


class TestDeclaration:
    def test_a_server_needs_a_purpose(self) -> None:
        """The purpose is what the person being asked to configure it will read."""
        with pytest.raises(CompositionError, match="needs a purpose"):
            McpServer(name="svc", purpose="", command="x")

    def test_stdio_without_a_command_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="needs a command"):
            McpServer(name="svc", purpose="p", transport=McpTransport.STDIO)

    def test_http_without_a_url_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="needs a url"):
            McpServer(name="svc", purpose="p", transport=McpTransport.HTTP)

    def test_mixing_transports_is_refused(self) -> None:
        with pytest.raises(CompositionError, match="a url belongs to http or sse"):
            McpServer(name="svc", purpose="p", command="x", url="http://y")

    def test_duplicate_names_are_refused(self) -> None:
        p = _pipeline(_stdio())
        with pytest.raises(CompositionError, match="already requires an MCP server"):
            p.require_mcp(_stdio())

    def test_a_stages_requirement_is_the_callers_problem_too(self) -> None:
        """Preflight is about the launch, and a nested stage runs in that launch."""
        stage = Stage(stage_id="inner")
        param = stage.body.declare_input("x", STR)
        step = stage.body.add(
            AgentNode(
                node_id="w",
                inputs=(InputPort("x", STR),),
                prompt="w",
                declared_outputs=(OutputPort("y", STR),),
            )
        )
        stage.body.connect_input(param, step, "x")
        stage.body.route(step, END)
        stage.body.expose_output("y", step, "y")
        stage.body.require_mcp(_stdio(name="inner_need"))

        parent = Pipeline(pipeline_id="outer")
        parent.require_mcp(_stdio(name="outer_need"))
        stage.instantiate(parent)
        assert {s.name for s in parent.all_mcp_servers()} == {"outer_need", "inner_need"}


class TestEmission:
    def test_a_secret_is_emitted_as_a_reference_never_a_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_TOKEN", "hunter2")
        block = mcp_servers_block(_pipeline(_stdio(env=(EnvVar("SOME_TOKEN", "the token"),))))
        rendered = str(block)
        assert "hunter2" not in rendered, "a committed artifact must never carry the value"
        assert "${SOME_TOKEN:-}" in rendered

    def test_the_default_keeps_the_artifact_loadable_without_the_secret(self) -> None:
        """A bare ${VAR} is a hard load error, which would break CI validation."""
        block = mcp_servers_block(_pipeline(_stdio(env=(EnvVar("ABSENT_VAR", "x"),))))
        assert "${ABSENT_VAR:-}" in str(block)

    def test_transport_shapes_emit_their_own_fields(self) -> None:
        http = McpServer(
            name="remote", purpose="p", transport=McpTransport.HTTP, url="https://x/mcp"
        )
        assert mcp_servers_block(_pipeline(http))["remote"] == {
            "type": "http",
            "url": "https://x/mcp",
        }


class TestPreflight:
    def test_a_missing_command_blocks(self) -> None:
        issues = conductor.preflight(_pipeline(_stdio()), probe=False)
        assert [i for i in issues if "not on PATH" in i.problem]
        assert all(i.blocking for i in issues)

    def test_a_missing_variable_blocks_and_says_what_it_is_for(self) -> None:
        server = _stdio(env=(EnvVar("ABSENT_VAR", "read access to the widget API"),))
        issues = conductor.preflight(_pipeline(server), probe=False)
        env_issue = next(i for i in issues if "ABSENT_VAR" in i.problem)
        assert "read access to the widget API" in env_issue.problem

    def test_the_remedy_is_the_authors_setup_hint(self) -> None:
        issues = conductor.preflight(
            _pipeline(_stdio(setup_hint="run `brew install widget`")), probe=False
        )
        assert issues
        assert all(i.remedy == "run `brew install widget`" for i in issues)

    def test_a_satisfied_requirement_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRESENT_VAR", "set")
        server = _stdio(command="sh", env=(EnvVar("PRESENT_VAR", "x"),))
        assert conductor.preflight(_pipeline(server), probe=False) == []

    def test_a_pipeline_declaring_nothing_is_clean(self) -> None:
        assert conductor.preflight(_pipeline(), probe=True) == []

    def test_an_unreachable_endpoint_is_only_found_by_probing(self) -> None:
        """The offline check cannot know a URL is dead; that is what probe buys."""
        server = McpServer(
            name="remote",
            purpose="p",
            transport=McpTransport.HTTP,
            url="http://127.0.0.1:9/mcp",  # discard port: refuses immediately
        )
        p = _pipeline(server)
        assert conductor.preflight(p, probe=False) == []
        assert [i for i in conductor.preflight(p, probe=True) if "unreachable" in i.problem]


def _with_tool(*tools: Executable) -> Pipeline:
    p = Pipeline(pipeline_id="t")
    node = p.add(AgentNode(node_id="a", prompt="x"))
    p.route(node, END)
    for tool in tools:
        p.require_executable(tool)
    return p


class TestDeclaredExecutables:
    """A tool a step checks its claims against is a requirement like any other.

    A missing MCP server breaks a step; a missing reference tool does not —
    the step runs, the lookup fails, and the model reports absence.
    """

    def test_a_missing_command_blocks_the_launch(self) -> None:
        p = _with_tool(Executable(name="definitely-not-installed-xyz", purpose="ground truth"))
        issues = conductor.preflight(p, probe=False)
        assert [i for i in issues if i.requirement == "exe:definitely-not-installed-xyz"]
        assert all(i.blocking for i in issues)

    def test_the_purpose_reaches_whoever_has_to_fix_it(self) -> None:
        p = _with_tool(
            Executable(
                name="definitely-not-installed-xyz",
                purpose="the schema claims are checked against",
                setup_hint="uv tool install widget",
            )
        )
        issue = conductor.preflight(p, probe=False)[0]
        assert "the schema claims are checked against" in issue.problem
        assert issue.remedy == "uv tool install widget"

    def test_a_present_command_is_clean(self) -> None:
        assert conductor.preflight(_with_tool(Executable(name="sh", purpose="p")), probe=True) == []

    def test_on_path_is_not_the_same_as_working(self) -> None:
        """A probe catches the command that exists and still cannot answer."""
        tool = Executable(name="sh", purpose="p", probe=("-c", "exit 3"))
        p = _with_tool(tool)
        assert conductor.preflight(p, probe=False) == []
        issues = conductor.preflight(p, probe=True)
        assert [i for i in issues if "exited 3" in i.problem]

    def test_a_stage_requirement_is_the_callers_problem_too(self) -> None:
        """A stage runs in the caller's environment, so preflight must see it."""
        stage = Stage(stage_id="inner")
        param = stage.body.declare_input("x", STR)
        step = stage.body.add(
            AgentNode(
                node_id="w",
                inputs=(InputPort("x", STR),),
                prompt="w",
                declared_outputs=(OutputPort("y", STR),),
            )
        )
        stage.body.connect_input(param, step, "x")
        stage.body.route(step, END)
        stage.body.expose_output("y", step, "y")
        stage.body.require_executable(
            Executable(name="definitely-not-installed-xyz", purpose="ground truth")
        )
        parent = _with_tool(Executable(name="sh", purpose="p"))
        stage.instantiate(parent)
        assert {t.name for t in parent.all_executables()} == {
            "sh",
            "definitely-not-installed-xyz",
        }
        assert [i for i in conductor.preflight(parent, probe=False) if "not on PATH" in i.problem]

    def test_a_duplicate_name_is_refused_where_it_is_written(self) -> None:
        p = _with_tool(Executable(name="sh", purpose="p"))
        with pytest.raises(CompositionError, match="already requires an executable"):
            p.require_executable(Executable(name="sh", purpose="q"))
