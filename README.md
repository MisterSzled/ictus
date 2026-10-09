# ictus

Typed composition, validation and linting for multi-stage agent pipelines.
Pipelines are authored as Python, checked by mypy and by composition lints,
emitted as Conductor YAML. Conductor executes.

Free software under the **GNU General Public License v3 or later** — see
[LICENSE](LICENSE). It comes with no warranty, to the extent permitted by law.
Pre-1.0: the stdlib's constructors are the API and they still move, so every
rename and removal is in [CHANGELOG.md](CHANGELOG.md).

## A pipeline

A pipeline is a folder, and `pipeline.py` is the graph in it:

```python
from ictus import AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl
from ictus.stdlib import approval_gate, succeed

STR = PortType.STRING

pipeline = Pipeline(pipeline_id="review", description="Summarise a diff, then ask a person")

read = pipeline.add(
    AgentNode(
        node_id="read_diff",
        prompt="Summarise the risk in the staged diff, in five lines.",
        declared_outputs=(OutputPort("summary", STR, "What changed, and the risk"),),
    )
)
gate = pipeline.add(
    approval_gate(
        node_id="ship_it",
        prompt=tpl("Ship this?\n\n", read.ref("summary")),
        inputs=(InputPort("summary", STR),),
    )
)
shipped = pipeline.add(succeed(node_id="shipped", reason="approved"))
held = pipeline.add(succeed(node_id="held", reason="a person said no"))

pipeline.set_entry(read)
pipeline.connect(read, "summary", gate, "summary")
pipeline.branch(gate, {"approved": shipped, "rejected": held})
```

Beside it, `config.yaml` says how it runs (`provider: claude-agent-sdk`),
`input.md` carries this run's values, and `build/` holds the emitted YAML —
committed, so a diff shows what will actually execute.

```sh
ictus emit   pipelines/review     # compile to Conductor YAML
ictus lint   pipelines/review     # composition rules, writes nothing
ictus run    pipelines/review     # re-emit, preflight, then launch
```

`read.ref("summary")` is checked when it is written: misname the port and the
line raises, rather than the run producing a prompt with a hole in it.
`branch` refuses to leave a gate choice unrouted. Both cost nothing; a live run
costs money and minutes.

## What is already built for you

**[STDLIB.md](STDLIB.md) is the catalogue** — every ready-made constructor, what
it is for, its options, and what it produces. It is checked against the code by
the test suite, so it describes the stdlib that exists rather than the one that
used to. `ictus stdlib` prints the same list from the installed library, and
`ictus stdlib <term>` searches it by name or by what a thing does.

| You want | Reach for |
|---|---|
| a person to approve, choose, or answer questions | `approval_gate`, `choice_gate`, `ask_human`, `ask_human_for` |
| a step that calls no model | `constant`, `bindings`, `counter`, `wait`, `shell`, `save_text` |
| to report to a channel, or comment on a ticket | `announce`, `comment` — through an `Integration` built under `ictus.notify` |
| to read a database or a ticket | `query`, `fetch` — through a `Datasource` built under `ictus.sources` |
| to end the run, distinguishably | `succeed`, `fail` |
| a command whose failure you route on | `try_shell` |
| to try until something is good enough | `converge` |
| to look something up until the question is answered | `investigate`, `read_ticket` |
| several models to argue until they agree | `council` (they poll), `roundtable` (they take turns) |

`from ictus.stdlib import ...` for all of it. The folders under
`src/ictus/stdlib/` group these by what they *are*, which matters when adding
one and not when using one — [source-layout.md](docs/source-layout.md) draws
them.

## Three tiers

| Tier | You write | Conductor gets |
|---|---|---|
| **Node** | `AgentNode`, `GateNode`, `QuestionsNode`, `ScriptNode`, `ComputeNode`, `WaitNode`, `TerminateNode` | one entry in the flat `agents:` list |
| **Stage** | `Stage`, with its own graph and an input/output contract | its own YAML file plus a `type: workflow` agent in the parent |
| **Scope** | `Scope` — a stage whose every exit is an outcome the caller routes on | the same, plus a closed vocabulary the parent must route exhaustively |
| **Pipeline** | `Pipeline` | the `WorkflowConfig` envelope |

Conductor has no nested-step construct inside an agent. `type: workflow` is its
only nesting, which is what a stage compiles to — its own entry point, its own
loops and gates, and one iteration of the parent's budget.

A stage and a scope cost the same. What a scope adds is that a bad ending is
something you handle rather than something that kills the run, which is why it
is a folder and not a return annotation.

## The engine boundary

`ictus.interfaces` is the only place allowed to know an engine's spelling — its
field names, template dialect, iteration accounting, CLI. `graph/` contains zero
Conductor strings, and that is checkable rather than aspirational:
`tests/test_boundaries.py` tokenises each module and fails on the engine's
vocabulary reaching one above `interfaces/`.

A `Backend` supplies what it can express (`Capabilities`), how to render a
graph (`compile`), its own extra lint rules, what this machine must provide
before a launch (`preflight`), and how to validate and run what it produced.
Rules like "a route list with no catch-all raises at run time" belong to a
backend, not to graphs in general, so `lint_pipeline(p)` without a backend
reports only what is true anywhere.

Not a plan to leave Conductor — a way to keep the coupling countable.

## Composition is checked where it is written

Node references are objects, never strings. A target that is not in the graph is
rejected by `connect`, so `to("plan_gaet")` is not expressible — and neither is a
node borrowed from a different pipeline.

```python
p.connect(plan, "plan", breakdown, "plan")  # port types must match
p.branch(gate, {"approved": execution, "rejected": breakdown})
```

References into prompts are typed too. `plan.ref("plan")` fails if that port is
not declared, and carries its type with it, so the reference is checked where it
is written rather than recovered from prompt text by a regular expression:

```python
prompt = tpl(
    "Break this plan into steps.\n\n",
    plan.ref("plan"),
    optional("Address these notes:\n", ref_to("review", "notes", STR)),
)
```

`ref_to` names a node that does not exist yet — a loop's back-edge — and is
resolved against the finished graph by the lint. The guard such a reference
needs on the first pass is emitted by the compiler, which already knows the
reference is deferred. A live run once died on exactly that omission.

`connect` wires control **and** data. Conductor keeps those separate — `routes:`
decides what runs next, `input:` decides what a node may read — so when they
diverge, say them separately:

```python
p.route(a, b)  # control only
p.feed(breakdown, "steps", execution, "steps")  # data only, across a gate
```

Every rejection happens at the call that introduces it: a port type mismatch, a
foreign node, an unrouted gate choice, a second unconditional route from one
node (Conductor takes the first match, so the second would be silently dropped).

What cannot be refused at the call is refused by `ictus lint`, which is why both
exist. `conductor validate` checks every reference it can see; the ones it
cannot — an unreachable agent, routes with no catch-all, a reference to an
output field that was never declared, a stage whose contract has drifted — are
silent until a run is in flight. Each rule names itself at the line that writes
it, so run it rather than read a list of them here.

## Where to go next

`STDLIB.md` is the catalogue, and `ictus stdlib <term>` searches it from the
installed library. Each page below answers one question and says at the top when
to read it.

| | |
| --- | --- |
| [running a pipeline](docs/running-a-pipeline.md) | the folder, the commands, what a person is asked |
| [configuration.md](docs/configuration.md) | every `config.yaml` setting |
| [wiring.md](docs/wiring.md) | `connect`, `route`, `feed`, and conditions |
| [scopes.md](docs/scopes.md) | outcomes, and the two scopes with a trap |
| [deliberation.md](docs/deliberation.md) | `council` or `roundtable`, and what the knobs cost |
| [preflight.md](docs/preflight.md) | what is checked before anything is spent |
| [reporting.md](docs/reporting.md) | getting a run's progress out to people |
| [run-events.md](docs/run-events.md) | attaching to a live run |
| [starting-from-a-channel.md](docs/starting-from-a-channel.md) | a message in a channel starting one |
| [reaching-the-project.md](docs/reaching-the-project.md) | what a step can see, and how to widen it |
| [gotchas.md](docs/gotchas.md) | behaviour the graph did not predict |
| [source-layout.md](docs/source-layout.md) | where everything is, for working on ictus |

`AGENTS.md` is the contributor's page: the environment, the layer rules and the
conventions nothing enforces.
