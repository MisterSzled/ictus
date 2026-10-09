# Changelog

What changed, for whoever has a pipeline written against the last version.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are [semantic](https://semver.org/), with the pre-1.0 caveat that
means: **the stdlib's constructors are the API and they still move.** Until
1.0 a minor bump may rename or remove one. Every such change is in the
**Changed** and **Removed** sections below, with the line to write instead.

`ictus.__version__` says which version is installed.

## [Unreleased]

### Added

- **`ictus-bridge overhear FOLDER --channel C0ABC123`** — starts runs from a
  channel read with your own credentials (`$SLACK_USER_TOKEN`, a user token
  with `channels:history`) rather than an app's. Nothing is installed in the
  workspace and nothing is invited to the channel, which is the reason it
  exists: a channel you cannot get a bot into can still start a run.
  **It cannot answer a gate** — an interaction reaches the app that posted the
  button, down a connection only an app-level token opens, so `overheard`
  returns `Asked` and nothing else. A run started this way waits at its gates
  for its dashboard, or for an `ictus-bridge listen` alongside. Listening
  starts at the newest message and a restart replays nothing; top-level
  messages only. A second command rather than a flag on `listen`, so no
  option is inert in half of its own command.
- **`ictus.bridge.slack.watch`** — `overheard`, `since`, `latest` and `Heard`,
  the polling half of the bridge.
- **`--pipeline <id>`** on `ictus-bridge overhear` and `listen` — point a
  listener at one of the manifests it found instead of all of them;
  repeatable. A name nothing claims stops it at startup and says what *was*
  found, rather than leaving it watching for a prefix nothing will send. It is
  also how two pipelines claiming one prefix is settled: on prefix alone the
  first in sorted-path order wins and the other never fires, silently.
- **`ictus.bridge.slack.request_in`** — the one recogniser both ways in call,
  so a prefix that starts a run over the socket starts the same one when the
  channel is read directly. `asked` is now a thin unwrapper over it.
- **`tests/test_doc_examples.py`** — every name a documented example imports
  from `ictus` has to exist. `test_every_python_example_parses` checks syntax
  and says so; `test_docs.py` checks `STDLIB.md`'s table. `from ictus import
  STR` is neither: it parses, is in no table, and is not a thing.
- **`tests/test_stand_ins.py`** — the production reader is run against
  `smoke/fake_channel.py`. `smoke/` was taken to be unrunnable by `pytest`
  because most of it drives a live engine; the stand-in for Slack is not that,
  and while nothing compared it with the service it stands in for it drifted
  into answering a shape ictus could parse and never act on.

### Changed

- **A run started from a channel works in its own pipeline folder.** It used
  to inherit the listener's working directory, so a relative path written by
  a step landed wherever `ictus-bridge` happened to be started — one level
  above a deployed pipeline. It is now the folder holding `build/` (or the
  workflow's own directory when it was built elsewhere), whatever directory the
  listener runs in, and the workflow path is resolved first so it still names
  the file from there. Workspace instructions are read walking up from that
  folder too.
- **`smoke_stop` says back what it heard instead of writing a file.** Its one
  step prints the text the run was handed, so the dashboard shows the
  channel's message as that step's output. It writes nothing to disk.
- **`listen_on`'s service argument is now optional, and omitting it is the
  ordinary case.** A pipeline that only needs *starting* no longer has to name
  a service, integrate it, and so declare a credential it never uses — which
  `ictus preflight` and `trigger.missing()` then both refused to proceed
  without. Being startable is not a reason to hold a credential: the listener
  reads the channel with its own. Naming a service still works unchanged, and
  the conversation still comes from that service's `integrate(thread=)` rather
  than a second argument, so the two can never disagree about where a run
  answers. Without a service there is no thread, because there is nothing to
  answer under. `Listener.service` is now `Integration | None`, and the
  manifest records `""`.
- **A string input reaches a run as the text it was.** `conductor run -i` runs
  every value through a type-guessing heuristic, so a Slack conversation id of
  `1700000000.000200` arrived as a float and came back `1700000000.0002` — a
  timestamp no message has. Roughly one in ten ends in a zero, and the symptom
  was a run reporting at the top of the channel instead of in its thread, while
  the sending program blamed a deleted message. `launch_command` and
  `ConductorBackend.run` take `verbatim=`, naming the inputs that must not be
  guessed at; those go over Conductor's typed input transport. The listener
  passes both of its own, and `ictus run` passes whichever the pipeline
  declared `STRING`, so an `int` input still gets an int.
- **`Trigger.thread_input` defaults to `""`, not `"reply_to"`.** A manifest
  names the thread input exactly when one was declared, so the old default
  invented one: every run launched from a chat service was handed a value under
  a name it had never chosen. `start` now passes the thread only when the
  pipeline asked for one.
- The manifest shape and `slack_channel` are otherwise untouched: the manifest
  never named a transport, and `slack_channel`'s `token=` was already the name
  of a variable, so a pipeline can post with a user token without a new
  constructor.
- `ictus.bridge.slack.listen.presses` is unchanged; `ictus-bridge listen` keeps
  its argument optional, and `overhear` requires a folder — with no manifests
  `listen` still answers gates and `overhear` would have nothing left to do.
- `ictus.notify.slack.send.TIMEOUT_SECONDS` is now declared in `__all__`; the
  bridge reads it, and asks the same host the same questions in the other
  direction.

- **The composition records moved out of `ictus.graph.pipeline`.** That module
  is 1,400 lines of which the class is most, and ten of the eleven names other
  packages took from it were not the class. They are now in
  `ictus.graph.composition`; `Pipeline` has not moved, so every
  `from ictus.graph.pipeline import Pipeline` is untouched, and neither has
  anything exported from `ictus` itself. Flat siblings rather than a
  `pipeline/` package, because `tests/test_boundaries.py` exempts
  `__init__.py` from the two checks that state a module's public surface, and
  packaging the class would have dropped it out of both.

| Was | Now |
| --- | --- |
| `ictus.graph.pipeline.Edge` | `ictus.graph.composition` |
| `ictus.graph.pipeline.END` | `ictus.graph.composition` |
| `ictus.graph.pipeline.DataDep` | `ictus.graph.composition` |
| `ictus.graph.pipeline.ExposedOutput` | `ictus.graph.composition` |
| `ictus.graph.pipeline.FailureMode` | `ictus.graph.composition` |
| `ictus.graph.pipeline.Listener` | `ictus.graph.composition` |
| `ictus.graph.pipeline.ParallelGroup` | `ictus.graph.composition` |
| `ictus.graph.pipeline.WorkflowInput` | `ictus.graph.composition` |
| `ictus.graph.pipeline.RouteEnd` | `ictus.graph.composition` |
| `ictus.graph.pipeline.EdgeTarget` | `ictus.graph.composition` |
| `ictus.graph.pipeline.TrimStrategy` | `ictus.graph.composition` |
| `ictus.graph.pipeline.ContextMode` | `ictus.graph.composition` |
| `ictus.graph.pipeline.BudgetMode` | `ictus.graph.composition` |
| `ictus.graph.pipeline.NativeTools` | `ictus.graph.composition` |
| `ictus.graph.pipeline.Pipeline.ABORT_CASE` | `ictus.graph.composition` |

- **The graph analysis is functions in `ictus.graph.traversal`, not methods on
  `Pipeline`.** They only ever read a graph, and as methods each one could
  reach a private store without anything saying it should not — eight of them
  did. `tests/test_boundaries.py` now refuses a private attribute read in that
  module, so the rule holds rather than being a note. Write
  `budget_cost(pipeline)` where you wrote `pipeline.budget_cost()`. No
  forwarders were left behind: a one-line method that calls a function moves no
  failure earlier and costs a second name for the same thing. `Pipeline.entry`,
  `outgoing`, `group_of` and `map_of` are unchanged and still methods.

| Was | Now |
| --- | --- |
| `ictus.graph.pipeline.Pipeline.back_edges` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.budget_cost` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.has_cycle` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.longest_cycle_length` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.loop_cost` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.may_be_unresolved` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.reachable_from_entry` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.reaches` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.require_loop_bound` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.step_cost` | `ictus.graph.traversal` |
| `ictus.graph.pipeline.Pipeline.total_cost` | `ictus.graph.traversal` |

- **A node, a parallel group and a map group now really do share one routing
  keyspace.** All three registrars said so in an error message and none checked
  all three stores, so the same collision was refused in one order and accepted
  in the other: `parallel("p", ...)` followed by `add(node("p"))` left the graph
  holding both under one name, and every route to `"p"` then resolved to
  whichever the backend looked up first. Now a `CompositionError` at the second
  one, naming which kind of thing already holds the name.
- **`ScopeNode` refuses an outcome `json.loads` would coerce.** `Scope` has
  always refused `"true"`, `"3"` and anything opening a JSON container — a
  rendered output goes through `_maybe_parse_json`, so the name comes back a
  bool or a number and every `equals` against it fails silently. `ScopeNode` is
  exported from `ictus` and could be built directly, and did not check. The
  builder's check stays where it is: a `Scope` builds its node lazily, so
  relying on the node alone would move that failure later.
- **The modules below were split.** Nothing a pipeline imports changed, and
  `ictus`'s own surface is untouched.

| Was | Now |
| --- | --- |
| `ictus.notify.slack.send` | `ictus.notify.slack.declare` (the two constructors) |
| `ictus.notify.slack.send` | `ictus.notify.slack.program` (the subprocess sender) |
| `ictus.notify.slack.send` | `ictus.notify.slack.api` (the Web API client) |
| `ictus.cli.watching.trace` | `ictus.cli.tracing` |
| `ictus.interfaces.conductor.binary` | `ictus.interfaces.conductor.control.launch` |
| `ictus.interfaces.conductor.launch_env` | `ictus.interfaces.conductor.control.launch` |
| `ictus.interfaces.conductor.launch_command` | `ictus.interfaces.conductor.control.launch` |
| `ictus.interfaces.conductor.TYPED_INPUT_FLAG` | `ictus.interfaces.conductor.control.launch` |
| `ictus.bridge.slack.listen.SlackError` | `ictus.bridge.slack.errors` |
| `ictus.bridge.slack.listen.refused` | `ictus.bridge.slack.errors` |
| `ictus.bridge.slack.listen.request_in` | `ictus.bridge.slack.requests` |
| `ictus.bridge.slack.listen.asked` | `ictus.bridge.slack.requests` |
| `ictus.interfaces.conductor.emit.agents.route_entries` | `ictus.interfaces.conductor.emit.routes` |
| `ictus.interfaces.conductor.emit.agents.kind_fields` | `ictus.interfaces.conductor.emit.fields` |

`ictus.notify.slack` and `ictus.bridge.slack` re-export exactly what they did
before, so only code that reached past a package into `send` or `listen` is
affected.

### Removed

- **`Pipeline.has_gate()`** and **`Pipeline.inbound()`** — both were on the
  class's public surface and neither had a caller anywhere: not in `src`, the
  suite, `smoke/`, a demo pipeline or a documented example. Write
  `any(isinstance(n, GateNode) for n in pipeline.nodes)` and
  `[e for e in pipeline.edges if e.target is node]` if you need them; the
  one-line forms are what the methods were. Removed before the graph was split
  rather than after, so they were not carried into a new file and counted
  towards its size.

## [0.1.0] — 2026-10-08

The first version with a number. Everything before this was `0.0.0`, so this
entry is the backlog: what the package looked like when it stopped being
unversioned, and what moved to get there.

The compiler itself — `graph/`, the lints, the emitted YAML — is unchanged.
Every workflow and manifest this release emits is byte-identical to the one
before it. What moved is where things live and what they are called.

### Added

- **`ictus stdlib`** — lists every ready-made node, stage and scope, grouped,
  with a one-line summary and the import line for each. `ictus stdlib <term>`
  searches by name *or* by what a thing does, so `ictus stdlib approve` finds
  `approval_gate`. Read off `__all__` and the docstrings, so it cannot drift
  from the code. `STDLIB.md` remains the fuller catalogue.
- **`ictus adapters`** — lists the services this installation can report to and
  read from, including ones from other packages. `--check` imports each and
  says which will not work.
- **Entry-point groups `ictus.notify` and `ictus.sources`.** A third party can
  now ship an adapter without a pull request: declare the group in your
  `pyproject.toml` and `ictus adapters` finds it. ictus's own adapters register
  the same way. `ictus.plugins` is the API.
- **`ictus.stdlib.catalogue`** — the stdlib described from the stdlib, for
  anything that wants the list programmatically.
- **`ictus.runs.answer.Press`** — an engine- and service-neutral record of
  somebody choosing one of a gate's options. `resolve` and `submit` take one.
- **`ictus.stdlib.scopes.outcomes`** — every scope outcome name, defined once.
- **`ictus-bridge`** — a second console script, for the chat daemon.
- **`tests/test_boundaries.py`** — the layering rules are now asserted over
  every module rather than described in `AGENTS.md`: no service named outside an
  adapter, nothing in ictus importing the bridge, no adapter imported by name.

### Changed

- **Prompt text moved out of Python, and a module with prompts became a
  folder** — `stdlib/llm/voice/__init__.py` plus `stdlib/llm/voice/stance.md`,
  read through `ictus.prompting.prompt`. Import paths are unchanged. Fifteen files, about 9,000 characters that used to
  be backslash-continued string literals. No emitted workflow changed. The
  layout is enforced by `tests/test_prompt_layout.py` rather than described
  anywhere: inline prose over 60 characters in prompt position fails, as does a
  prompt file nothing reads, a request with no file, and a file missing from
  the built wheel.
- **`ictus.stdlib.prompts` is now `ictus.stdlib.baseline`.** Prompt text sits in
  the folder of the module that reads it, so there is no `prompts/` directory
  anywhere for the old name to describe.

Breaking, with what to write instead:

| Was | Now |
| --- | --- |
| `ictus listen` | `ictus-bridge listen` |
| `from ictus.stdlib import briefing, remediate, validate_mcp, voice` | `from ictus.stdlib.llm import ...` |
| `from ictus.sources import readonly_sqlite` | `from ictus.sources.sqlite import readonly_sqlite` (and so on per engine) |
| `ictus.config` | `ictus.runspec.config` |
| `ictus.runspec` (module) | `ictus.runspec.inputs` |
| `ictus.baseline` | `ictus.stdlib.baseline` (the prompt) and `ictus.runspec.config` (`NO_BASELINE`) |
| `ictus.scaffold` | `ictus.runspec.scaffold` |
| `ictus.gate` | `ictus.assemble.start_gate` |
| `ictus.integrate` | `ictus.assemble.announcements` |
| `ictus.answer` | `ictus.runs.answer` |
| `ictus.websocket` | `ictus.net.websocket` |
| `ictus.notify.slack.listen` | `ictus.bridge.slack.listen` |
| `ictus.notify.slack.trigger` | `ictus.runs.triggers` and `ictus.runs.launch`; the Slack half is in `ictus.bridge.slack.listen` |
| `ictus.interfaces.conductor.runs` | `ictus.interfaces.conductor.control.live` |
| `ictus.interfaces.conductor.agents` | `ictus.interfaces.conductor.emit.agents` |
| `ictus.interfaces.conductor.events` | `ictus.interfaces.conductor.control.events` |
| `ictus.interfaces.conductor.mcp` | `ictus.interfaces.conductor.preflight` for `preflight_issues`, `…emit.mcp` for `mcp_servers_block` |
| `Pipeline.add_subworkflow` / `widen_subworkflow` | `Pipeline.add_subgraph` / `widen_subgraph` |

The rest of `interfaces/conductor/` moved the same way: `workflow`, `templates`,
`serialize`, `mapping`, `parallel` and `manifest` into `emit/`; `live`,
`respond`, `trace` and `signals` into `control/`.

| Was | Now |
| --- | --- |
| `ictus.cli` (module) | `ictus.cli` (package: `app`, `building`, `running`, `watching`, `catalogue`) |

- **`resolve` and `submit` take a `Press`**, not a Slack `Click`/`Note`.
  `submit` takes the text as a second argument rather than inside the record.
- **The stdlib's folders are named for what things are**, in `graph.NodeKind`'s
  vocabulary rather than Conductor's step kinds: `agents/` → `llm/`,
  `terminals/` → `exits/`, and the six scopes moved out of `stages/` into
  `scopes/`. The flat `ictus.stdlib` namespace is unaffected except as noted
  above.
- **`ictus.notify` and `ictus.sources` re-export no service.** Both are boundary
  packages now held to the same rule, and a flat re-export puts every service's
  name in the one file whose job is not to have it.
- **The Slack bridge is `ictus.bridge`**, its own package with its own console
  script. It may import ictus; nothing in ictus may import it.

### Removed

- **`ictus.stdlib.verdict`.** It was exported and documented and called by
  nothing — no stage, no pipeline, no test. For a yes/no a route can test,
  declare a `BOOLEAN` output on the node that decides and branch on it.

### Fixed

- **`render_output_schema` was lowering output ports to Conductor's `output:`
  block from inside `graph/node.py`** — its docstring said so, and its only
  caller was always the Conductor backend. Moved to
  `interfaces/conductor/emit/agents.py`. `graph/` now builds no YAML shape at
  all.
- **`Pipeline.add_subworkflow` and `widen_subworkflow` used the engine's noun**
  for a thing the graph itself calls a sub-graph (`NodeKind.SUB_GRAPH`,
  `SubGraphNode`). Renamed to `add_subgraph` and `widen_subgraph`.
- **The engine boundary was prose and is now a test.** `test_boundaries.py`
  refuses Conductor's spelling, and any function returning `YamlDict`, in the
  five packages that model a pipeline without knowing what runs it. Both of the
  defects above are what it was written against.
- **The boundary tokeniser read f-string prose as identifiers.** Since 3.12 an
  f-string's text is `FSTRING_MIDDLE`, not `STRING`, so sentences inside one
  were being scanned for vendor names.
- **`interfaces/conductor/` was emission and live-run control in one package.**
  The five control modules were never imported by the emit path, which is what
  made the seam obvious. Now `emit/` is a pure function of a pipeline — no
  environment read, no process started, so `ictus emit` still works on a laptop
  with no credentials and no engine — `control/` only ever acts on a run that
  exists, and `mcp.py` split along the same line into `emit/mcp.py` and
  `preflight.py`.
- **`__all__` is now on every library module, and states the real surface.**
  Six lacked one entirely — `errors` and five of `graph`, the core public API —
  and eleven more understated it, declaring two names while another module
  imported five. Both are now checked.
- **`UnsupportedFeatureError` removed.** Defined and exported from
  `ictus.interfaces`, raised nowhere and caught nowhere; the lint layer refuses
  an unsupported feature with a `CompositionError` long before a backend sees
  it.
- **`notify/__init__.py` held 176 lines of implementation** — subprocess calls
  and credential masking in the file whose job is to be a boundary, and the only
  package init in the tree carrying one. Moved to `notify/deliver.py`.

- **Scope outcome constants were defined twice.** `AGREED`, `UNRESOLVED` and
  `HALTED` existed in both `council` and `roundtable`, and `EXHAUSTED` in both
  `converge` and `investigate`. The flat re-export shipped whichever module
  imported first, so one of each pair was reachable only by its module path.
  The values matched, so nothing was wrong — one edit to either and the export
  would have been a lie that read as true.
- **Four of the five example pipelines were never linted.** `ruff` honours
  `.gitignore`, which keeps all but one demo folder out of the repository, so
  `make soundcheck` checked one of them. `soundcheck` now names them, as it
  already did for mypy. Two real errors were sitting in them, including an
  unused import in a file people copy.
- **The README opened with the internal source tree.** It now opens with a
  working pipeline and a table of what is already built; the tree moved to the
  end, where it is contributor material.
