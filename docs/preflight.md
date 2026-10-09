# Preflight

Read this when a run refused to start, or when you want to know what is checked
before anything is spent. Preflight is free; a run that discovers a missing
credential after a gate has already paid for the step in front of it.

## What is checked

Four kinds of requirement, each declared on the pipeline and each checked before
a launch. `ictus preflight` prints every one it found, then only the problems.

| Declared with | Checked by | `--probe` additionally |
| --- | --- | --- |
| `require_mcp` | the command exists, its env vars are set | opens the connection |
| `require_executable` | the command is on `PATH` | runs the tool's own `probe` |
| an `Integration` | its command is on `PATH`, its env vars are set, and something announces what it asked to hear | — |
| a `DataSource` | its env vars are set; its own commands are folded in as executables | — |

The rest of this page is the MCP case, which is the one with a remedy loop
inside the run as well as a check before it.

## Declaring a server

```python
pipeline.require_mcp(
    McpServer(
        name="github",
        purpose="Read the change set and comment on its PR",  # read by whoever configures it
        transport=McpTransport.STDIO,
        command="github-mcp-server",
        args=("stdio",),
        env=(EnvVar("GITHUB_TOKEN", "a token with repo:read and pull_request:write"),),
        setup_hint="Install github-mcp-server, then `export GITHUB_TOKEN=$(gh auth token)`",
    )
)
```

`ictus run` checks those before it launches anything, and refuses if they are
unmet — a database reset should not get halfway before discovering a token is
missing. `--probe` (on by default) additionally opens each connection, which
catches a rejected credential that a "is the variable set?" check cannot.
`--skip-preflight` overrides.

Preflight also exists *inside* a run. `validate_mcps` is a stage: one
`validate_mcp` agent per server, in a parallel group when there is more than
one, then a gate if any failed that loops back so the person can connect the
thing and retry without losing the run.

The in-workflow check earns its model call by answering a question the CLI
cannot: the command can be installed, the token set and the endpoint answering
while the server never reaches the model. Only asking the model to *use* it
proves the connection end to end — and being a node, it is visible in the
dashboard while it happens.

Conductor permits only model calls and computations inside a parallel group;
gates, questions, scripts, waits, sub-workflows and terminals are all rejected
as members.

The gate offers three ways out, not two:

```text
Help me fix it                → a helper that talks you through it
I have fixed it — check again → straight back to the checks
Abort the run                 → terminate, status: success
```

"Help me fix it" routes to a `remediate` node carrying Conductor's `dialog`, so
it opens a multi-turn conversation — in the dashboard when one is served, in the
terminal otherwise. Whatever it does, the route returns to the checks: a claimed
fix is only believed once it passes. (`dialog` is forbidden on a gate, so the
helper must be a separate node — which the node types already enforce, since
only a model call carries the field.)

That helper draws one hard line, and it is a security boundary rather than a
preference: **it never handles credentials.** It diagnoses, and repairs what
needs no secret. The moment a fix needs a token or an access change it stops and
hands over the exact command to run in your own shell. Nothing asks you to paste
a secret into a conversation with a model, and no secret value is printed back.

Two commands, two questions, deliberately not merged:

| | asks | must pass on a machine with no credentials |
|---|---|---|
| `ictus validate` | is this workflow well-formed? | **yes** — else CI cannot check the committed artifact |
| `ictus preflight` | can *this* machine run it? | no — that is the whole point |

Secrets are never emitted. ictus writes the reference `${GITHUB_TOKEN:-}` and
checks separately that the variable is set. The empty default is load-bearing: a
bare `${VAR}` that cannot be expanded is a hard error in Conductor's loader, so
committing one would make the artifact unvalidatable anywhere the secret is
absent.
