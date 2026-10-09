# Reaching the target project

Loaded when a step cannot see what it is meant to work on, or when `ictus
lint`/`preflight` rejects a node's tools, bounds or declarations.


This is the part that decides whether your pipeline can do anything. Two
independent settings, and both must be right.

## Tools — three states, not shades of one thing

```python
AgentNode(node_id="survey", prompt=..., tools=None)  # the workflow default
AgentNode(node_id="judge", prompt=..., tools=())  # no tools at all
AgentNode(node_id="x", prompt=..., tools=["Read"])  # REFUSED at composition
```

- **`tools=None`** — the workflow's default set, which `config.yaml` decides.
  `native_tools` defaults to `none`, so a plain node has **no filesystem and no
  shell**: set `native_tools: claude_code` for a node that must read a real
  project. `AgentNode.tools` already defaults to `None`, so the thing to set is
  the workflow, not the node.
- **`tools=()`** — denied. Correct for a node whose entire input is in its
  prompt: a council of four judging a diff should not be four agents opening the
  same file. `voice()` defaults to this deliberately.
- **a list** — refused while you write it. Conductor's `tools:` are *workflow*
  tool names, not the CLI's, and its provider raises mid-run rather than
  granting the wrong ones. There is no per-tool allowlist for an agent step. An
  MCP server has its own `tools:`, but on `claude-agent-sdk` a narrowing one
  raises mid-run as well — leave it unset and take everything the server
  offers.

## Three ways to bound a step, and they are not interchangeable

| Setting | Bounds | Enforced by |
| --- | --- | --- |
| `max_turns` | tool-use rounds | the engine, fatally — see below |
| `timeout_seconds` | wall clock, whole call | the engine, cancelling from outside |
| `max_session_seconds` | wall clock, provider session | the provider itself |

Setting `max_session_seconds` at or above `timeout_seconds` is refused at
composition: the engine gets there first, so the number could never fire.

There is a fourth check that is about the *answer* rather than the clock:

```python
AgentNode(
    node_id="survey",
    prompt=...,
    tools=None,
    max_turns=200,
    timeout_seconds=900,
    validator=Validator(criteria="Every claim cites a file and line."),
)
```

`validator` runs a second model call that judges the output against the rubric
and re-runs the step once with the feedback. Budget two calls per step where you
set it, three where the revision fires; `revise=False` reports without acting.
It catches what the other checks cannot — `declared_outputs` fixes the shape and
`retry` covers a call that fell over, but neither notices a well-formed answer
that is wrong.

## Turn budget — the default is a kill, not a throttle

A node with tools will use them. The engine allows **50 tool-use rounds** and
then *raises* rather than returning what the step had. That error is not one a
scope can turn into an outcome, so it destroys the whole run — after every
earlier step has been paid for.

```python
AgentNode(node_id="survey", prompt=..., tools=None, max_turns=200)
Voice(node_id="capability", persona=..., focus=..., tools=None, max_turns=200)
```

`voice()` **refuses at composition** if you give it tools and no `max_turns`.
For raw `AgentNode`s nothing forces you; set it anyway on anything told to go
and look. 200 is a sane figure for a node reading a repository.

## What a step knows about the project

A step is **not** a Claude Code session opened in the repo, and the difference is
context, not capability. The provider pins `setting_sources=[]` — no `CLAUDE.md`,
no settings, no ambient skills, no hooks.

Two levers, both already on by default:

| Lever | Reaches | Set in |
| --- | --- | --- |
| `--workspace-instructions` | the **target project's** `AGENTS.md`, `.github/copilot-instructions.md`, `CLAUDE.md`, `.github/instructions/*.instructions.md`, walking up to its git root | on by default; `workspace_instructions: false` in `config.yaml` to disable |
| `instructions:` in `config.yaml` | literal text or paths **you** name, prepended to every prompt | `instructions: [./context.md]` |

Use `instructions:` for what the *pipeline* needs every step to know; leave
`--workspace-instructions` on so the step also reads what the *project* says
about itself.

What you cannot recover: **Claude Code's own system prompt.** Conductor types
`AgentDef.system_prompt` as `str | None`, and naming the SDK's `claude_code`
preset needs a mapping — so a plain string *replaces* the preset rather than
appending to it. `ictus.stdlib.baseline.AGENT_BASELINE` is the stand-in. Put working
discipline there or in `instructions:`, not in the hope that the model brings it.

`AgentNode` exposes `working_dir`, `skills` and `plugins` — of the fields only
some providers act on, the three `claude-agent-sdk` reads. `skills` and `plugins` are tri-state like
`tools`: unset takes the workflow default, `()` denies every one, a non-empty
tuple names exactly what to load.

```python
AgentNode(
    node_id="review",
    prompt=...,
    tools=None,
    max_turns=200,
    working_dir="/srv/target",
    plugins=("prs",),
)
```

Three traps here, each named at the point of use with what to write instead.
`ictus lint` catches two: a **relative path** on an agent, which resolves
against `build/` rather than the project, and a field this **provider does not
read**. `ictus validate` catches the third, a **skill named outside a plugin
root**. Write it, run both, read what they say. The docstrings in
`graph/node.py` say what each field means.

## Pointing the run at a project

Three ways, in precedence order:

1. `ictus run <folder> --repo ~/work/the-project`
2. `repo: ../../the-project` in `input.md` frontmatter — resolved **relative to
   the input file**, and passed to the pipeline as an input named `repo` if one
   is declared.
3. `cd ~/work/the-project && ictus run ~/…/pipelines/<name>` — the default is
   the directory you launched from.

A run started from a channel (`ictus-bridge listen` or `overhear`) has none of
these: it works in **its own pipeline folder**, the one holding `build/`, never
in the directory the listener was started from. A deployed pipeline's relative
paths therefore stay inside the deployment.

Agents then read and write inside that directory. Two traps:

- `save_text` writes relative to the **run's** working directory (the target
  project), not the pipeline folder.
- `working_dir` resolves differently per node kind: on a model call it resolves
  against the workflow file; on a `shell` step it goes straight to the
  subprocess and resolves against the cwd you ran from.

## Declare what the environment must supply

If a node checks its claims against a tool, declare it. A missing tool does not
crash the step — the step runs, the lookup fails, and the model reports the
thing as absent. That is a confident wrong answer at full price.

```python
from ictus import Executable

pipeline.require_executable(
    Executable(
        name="conductor",
        purpose="the schema every claim about the engine is checked against",
        probe=("--version",),
        setup_hint="curl -sSfL https://aka.ms/conductor/install.sh | sh",
    )
)
```

`ictus preflight` then refuses the launch instead. Same for `EnvVar` and
`McpServer` (secrets are emitted as `${VAR}` references, never values).

