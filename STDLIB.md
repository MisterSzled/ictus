# ictus stdlib

Ready-made pieces, all built from the public API. `from ictus.stdlib import ...`

Every constructor also takes `description`, and most take `inputs`. Options below
are the rest.

| Tier | Placed with | Costs the caller |
| --- | --- | --- |
| **Node** | `pipeline.add(...)` | 1 iteration |
| **Stage** | `stage.instantiate(parent)` | 1 iteration, whatever it contains |
| **Scope** | `scope.instantiate(parent)` + `parent.branch_on_outcome(...)` | 1 iteration |

A **Scope** is a stage whose every exit is an outcome you route on, instead of a
failure that kills the caller.

## Model calls

| Constructor | Use | Options | Produces |
| --- | --- | --- | --- |
| `briefing` | Summarise upstream output for a person to decide on | `subject`, `source`, `output_name` | `summary: string` |
| `verdict` | Answer a yes/no question as a boolean a route can test | `question`, `source`, `output_name` | `verdict: boolean`, `rationale: string` |
| `voice` | One standpoint's assessment — persona plus focus | `persona`, `focus`, `subject`, `intent`, `prior`, `direction`, `tools`, `max_turns` | `satisfied: boolean`, `position: string`, `concerns: string`, `unchecked: string` |
| `validate_mcp` | Prove an MCP server is reachable by calling a read-only tool | `server` | `available: boolean`, `detail: string` |
| `remediate` | Work through a blockage with the person at the terminal | `problem`, `subject` | `resolved: boolean`, `summary: string` |

## Human decisions

| Constructor | Use | Options | Produces |
| --- | --- | --- | --- |
| `approval_gate` | Approve or reject, free text on reject | `prompt`, `approve_label`, `reject_label`, `notes_field` | `selected: string`, `notes: string` |
| `choice_gate` | Arbitrary `(value, label)` options | `prompt`, `choices` | `selected: string` |
| `ask_human` | Ask questions known at composition time | `questions`, `allow_abort`, `allow_skip` | `answers: object`, `transcript`, `answered_count`, `outcome`, one port per question id |
| `ask_human_for` | Ask questions an earlier node produced | `source`, `allow_abort` | `answers: object`, `transcript`, `answered_count`, `outcome` |

Route with `pipeline.branch(gate, {...})`. `Question(text, id=, choices=, required=)`.

## Steps without a model

No provider call, still 1 iteration each.

| Constructor | Use | Options | Produces |
| --- | --- | --- | --- |
| `constant` | One computed value | `value`, `output_type` | `value`, typed as `output_type` |
| `bindings` | Several named values at once | `values`, `outputs` | one port per declared output |
| `counter` | Count passes through a point, from one | — | `value: number` |
| `announce` | Report to an integration from inside the graph | `text`, `to`, `thread`, `answers`, `inputs`, `timeout` | `thread`, `posted` |
| `comment` | Add a remark to an item an integration names — a ticket, an issue | `on`, `body`, `to`, `inputs`, `timeout` | `issue`, `posted`, `why` |
| `fetch` | Ask a read-only datasource for one named thing — a ticket, by key or link | `what`, `against`, `inputs`, `timeout` | `found`, `got`, `why` |
| `query` | Ask a read-only datasource one statement | `sql`, `against`, `environment`, `limit`, `inputs`, `timeout` | `rows`, `count`, `ran`, `why` |
| `save_text` | Write a value another step produced to a file | `text`, `to`, `append`, `working_dir` | `path: string` |
| `shell` | Run a command | `command`, `args`, `outputs`, `stdin`, `timeout`, `working_dir`, `enforce_outputs` | whatever `outputs` declares |
| `wait` | Pause | `seconds`, `reason` | — |

Most pipelines never call `announce`. `pipeline.integrate(service)` attaches a
destination when the pipeline is loaded — one line to add a service, one line to
remove it, and nothing about tokens or threads in the composition. Reach for
`announce` when one point deserves one particular sentence.

What `integrate` attaches, after the start policy so the start gate is included:

- an opener, for a service that threads, which the whole run hangs under;
- an announcement in front of every gate and every question, in the pipeline
  and in every stage nested in it — a gate's own choices as its buttons;
- an announcement in front of every way the graph ends: each explicit exit, and
  every route to `END`.

Each announcement reads what the node it stands in front of reads, so a prompt
renders in the channel as it does in the dashboard. A stage gets the thread as
an optional parameter, added when it has something to announce. An explicit
`max_iterations` is raised by exactly the steps added; a loop with no
`loop_passes` to price them by is refused at load.

`to` is an `Integration`, built by a constructor in `ictus.notify` —
`slack_channel` or `slack_webhook` today. Nothing in `graph/` or `stdlib/` knows
which service it is: the integration carries an opaque program that sends one
report, and a second destination is a new module under `notify` and no change
anywhere else. `test_no_service_is_named_above_the_notify_boundary` is what
keeps that true.

A report never fails the run. The step always succeeds; one that could not
deliver says `posted: "false"` and leaves its reason on stderr, where the
dashboard and `ictus trace` show it. A channel being down says nothing about
whether the work succeeded. What a step cannot report is what no step can see —
a budget tripping, a step failing, the engine being killed — which is what
`ictus watch` is for, and preflight says so to an integration that asks for one.

`answers=<gate>` puts that gate's choices in as buttons, read off the gate: a
renamed option cannot leave a button that answers nothing, and no button can
carry a value the gate does not offer. Buttons and threads both need a service
that can carry an answer back; `slack_webhook` cannot, and says so at
composition.

## Terminals

| Constructor | Use | Options |
| --- | --- | --- |
| `succeed` | End the run successfully | `reason`, `result` |
| `fail` | End as failed, non-zero exit | `reason`, `result` |

## Stages

| Constructor | Use | Options | Contract |
| --- | --- | --- | --- |
| `briefing_gate` | Turn data into a human decision and report which was taken | `subject`, `question`, `data_type`, `options` | in `data` → out `decision`, `summary`, `notes` |
| `resolve_unknowns` | Work out what is missing, ask only about that | `subject`, `needs` | in `brief` → out `known: object`, `answers: object` |
| `script_sequence` | Chain commands, threading each output into the next | `steps`, `parameter`, `working_dir` | in `<parameter>` → out `result` |
| `validate_mcps` | Prove every declared MCP server is reachable, with a fix-it loop | `servers`, `retries` | out `report` |

`ReviewOption(value, label, ask_for_notes=)`; default pair is `APPROVE_OR_REJECT`.
`ScriptStep(node_id, command, args, output, receives_previous=)`.

## Scopes

| Constructor | Use | Options | Outcomes | Carries |
| --- | --- | --- | --- | --- |
| `try_shell` | Run one command whose failure the caller routes on | `command`, `args`, `parameter`, `outputs`, `stdin`, `timeout`, `working_dir`, `node_id` | `ok`, `failed` | `stdout`, `stderr`, `exit_code` |
| `converge` | Bounded try/judge loop; running out is a value, not a crash | `attempt`, `judge`, `judge_prompt`, `verdict_port`, `passes`, `pause_between` | `converged`, `exhausted` | the attempt's outputs, `feedback`, `passes` |
| `read_ticket` | Read one ticket through a read-only source, holding its credential inside the stage | `against`, `timeout`, `subject` | `read`, `missing` | `ticket`, `why` |
| `investigate` | Bounded look/ask loop against a read-only datasource; the thinking step has no tools and can only request a statement | `against`, `looks`, `reasoning`, `model`, `max_turns`, `limit`, `timeout`, `remember`, `may_read_files`, `environment`, `subject` | `answered`, `exhausted` | `answer`, `looks`, `last_sql` |
| `roundtable` | Several people taking turns, in order, until they agree | `speakers`, `subject`, `charge`, `rounds`, `study`, `interject`, `remember`, `closing` | `agreed`, `unresolved`, `halted` (with `interject`) | `minutes`, `dissent`, `rounds` |
| `council` | Several standpoints deliberating until they agree on a report | `voices`, `subject`, `charge`, `rounds`, `interject`, `deliberate`, `verify`, `verify_each`, `verify_turns`, `remember`, `synthesis` | `agreed`, `unresolved`, `halted` (with `interject`) | `report`, `dissent`, `unverified`, `rounds`, `corrections` |

`Attempt(node_id, prompt, produces)` — a sequence becomes a chain, each step
reading the last. `Voice(node_id, persona, focus, tools=, max_turns=)`.
`Speaker(node_id, persona, focus, tools=, max_turns=)`.

- **`try_shell` is the only way to route on a command that failed**, and it
  costs you the stdout contract to get there. A script step's non-zero exit is
  not a failure to Conductor: `exit_code` comes back beside `stdout` and
  `stderr`, nothing branches on it, and the next step runs — so a restore that
  fails is followed by the configuration queries that were meant to land on
  what it restored. Declaring `outputs` looks like the fix and is the opposite
  one: the engine then parses stdout as JSON and *raises* when it cannot, and
  that raise lands before routes are evaluated, so the branch written for the
  failure can never be taken. `try_shell` emits the ports without the schema
  (`shell(enforce_outputs=False)`), so nothing is checked, nothing raises, and
  `exit_code` decides the exit. Both outcomes carry `stdout`, `stderr` and
  `exit_code`, so the failure branch can say what went wrong.
- **What `try_shell` does not convert: a command that never started.** A
  missing binary and a `timeout` both leave the executor as an `ExecutionError`,
  which is not a value and not routable. Declare the tool with
  `require_executable` so `ictus preflight` refuses the launch instead.
- **`outputs` on a `try_shell` must be printed on every zero exit.** Conductor
  renders with `StrictUndefined`, so a declared field the command omitted raises
  at the reference — reinstating, on the success path only, the crash the scope
  removes. The `failed` exit does not read them; they arrive there as empty
  values of their declared type.

- **A context ceiling is per *stage*, and only one strategy survives a loop.**
  `Pipeline(context_max_tokens=, context_trim=)` emits `workflow.context`, and
  a stage is its own workflow file — so a long council can be bounded without
  bounding its caller. There is no per-node equivalent; the engine has no
  per-agent context config. Trimming is the only thing that removes a step's
  output from a run, and a loop reads the previous pass through exactly those
  entries: once one is deleted the reference renders empty and is
  indistinguishable from a first pass, so the loop keeps going and stops
  deliberating. `TrimStrategy.TRUNCATE` shortens fields in place and leaves
  every reference resolvable; the lint refuses `DROP_OLDEST` and `SUMMARIZE` on
  a graph that loops, and refuses a ceiling with no strategy at all — the engine
  does not leave that unset, it uses `drop_oldest`.
- **A `roundtable` without `study` is anchored by construction.** Turns are
  sequential, so only the first speaker ever states a view nobody influenced;
  everyone after it speaks into a frame somebody else set, and four people
  agreeing means one person plus three confirmations. `study=` makes each person
  commit to a public `opening` before anybody speaks, so the minutes are handed
  both ends and can say which argument moved whom — convergence and capitulation
  are identical in the final positions and only distinguishable against the
  openings.

**`council` polls, `roundtable` talks.** A council's voices run at once, so none
has heard the others when it speaks and a synthesis step has to write each round
up for the next one; it converges on a *record*. A roundtable's speakers take
turns, so the second has heard the first *this* round and there is no lag inside
a round at all — they answer each other directly, and the minutes are written
once at the end rather than once a round. The cost is wall-clock: a round takes
the sum of its turns rather than the longest of them. Reach for `council` when
the standpoints are independent and you want breadth; reach for `roundtable`
when you want them to actually argue. Order is part of the design — whoever
speaks last has heard everyone.

`deliberate=` (on by default) hands every voice the others' positions and
concerns from the last round, verbatim and attributed, and asks it to answer
them by name. Off, a voice sees only the synthesis — one more agent's
compression of what everybody said — so it can restate its position but cannot
disagree with anyone in particular, and the council discovers and asserts round
after round without converging. Costs prompt tokens and no extra model calls.

`verify_each=` puts a checker behind every voice, all running at once, before
the round is written up. Off by default — it doubles the model calls in a round.
It earns that when one checker facing the finished report would have to triage:
thirty claims and a fixed budget buys about a lookup each, which reaches the
docstring and not the code under it. A per-voice checker has the same budget for
a quarter of the material. With it on, a voice reads *its own* checker's
corrections next round rather than the group's — a voice can act on "this claim
of yours did not hold" and can only nod at one aimed at the synthesis.

`verify=` adds a step that tries to **refute** each round's report against the
thing it describes, and gates agreement on the result. Without it the exit
condition is "all voices satisfied", which measures convergence between them and
nothing else — four models given the same wrong material agree sooner, not later.

`judge=` picks who decides: `"model"` (an agent emits `approved` + `notes`),
`"human"` (an approval gate), `"self"` (the attempt declares the verdict itself —
the polling shape, usually with `pause_between`).

## Wiring

| Call | Carries | Use when |
| --- | --- | --- |
| `pipeline.connect(src, port, dst, port)` | control + data | the target runs next and reads the value |
| `pipeline.route(src, dst, when=)` | control only | the target needs nothing from the source |
| `pipeline.feed(src, port, dst, port)` | data only | the value crosses a gate or a branch |
| `pipeline.connect_input(param, dst, port)` | a workflow input | binding the pipeline's own parameters |

## Conditions

Built from references so they stay correct when what they were derived from changes.

`equals` refuses a value whose Python type does not match the port's, because
the condition it would render is well-formed and never true. A route is tested
against the value the engine stored, not its rendered text, so `exit_code` is a
real `0` and a verdict is a real `True` — `== '0'` and `== 'true'` both send
every run down the catch-all with nothing to see. An `ARRAY` or `OBJECT` port
cannot be compared at all; route on a scalar the step also declares.

| Helper | Renders |
| --- | --- |
| `equals(ref, value)` / `not_equals` | `{{ x == 'value' }}` for a string, `{{ x \| int == 0 }}` for an int, `{{ x == true }}` for a bool — the value's type must match the port's |
| `every(*refs)` / `not_every` | `{{ a and b and c }}` |
| `at_least(ref, n)` | `{{ x \| int >= n }}` |
| `tpl(...)`, `optional(...)`, `ref_to(id, port, type)` | prompt text with typed references |

## Running

A pipeline is a folder. Three files, three questions.

```
pipelines/needs-council/
  pipeline.py     what the graph is
  config.yaml     how it runs
  input.md        what to run it on
  build/          emitted YAML, committed
```

`config.yaml` is required; the minimal one is a line. `provider` has no default
because Conductor's is `copilot`, and inheriting that silently is a real bug this
project has already shipped once.

| Setting | Default | |
| --- | --- | --- |
| `provider` | none — required | who answers the model calls: `claude-agent-sdk`, `claude`, `copilot`, `openai`, `hermes`, `aca` |
| `default_model` | the provider's | the model every step uses unless it says otherwise |
| `system_prompt` | ictus's baseline | what every model call is told about how to work; a path, literal text, or `none` |
| `instructions` | none | project context prepended to every prompt; paths relative to the folder |
| `start_gate` | `true` | hold at a confirmation gate before anything runs |
| `budget_usd` / `budget_mode` | none / `audit` | |
| `timeout_seconds` | none — unlimited | wall-clock ceiling on the whole run; the only setting that bounds elapsed time |
| `max_iterations` | derived from the graph | |
| `dashboard` | `true` | serve the web UI on `ictus run` |

Override the model for one step with `AgentNode(model=..., provider=...)` — a
cheap model for triage, an expensive one for the hard step.

`input.md` is YAML frontmatter over a body; the body feeds whichever input the
pipeline declares with `prose=True`.

```markdown
---
scope: the stdlib and the CLI      # a declared input
repo: ../../some-project           # optional; relative to this file
---
The body feeds the input declared with prose=True.
```

The run works in the directory you invoke it from unless `repo:` or `--repo`
says otherwise.

Every pipeline gets a **start gate** unless `config.yaml` turns it off: the run
loads, the dashboard comes up with the whole graph in it showing the actual input
values, and nothing happens until you choose. Conductor's dashboard has stop,
kill and resume but no start, so this is the only way to look before you leap.

```sh
ictus init pipelines/my-thing               # scaffold the three files

cd ~/work/my-service
ictus run ~/pipelines/needs-council          # input.md supplies the rest
ictus run ~/pipelines/needs-council -i scope='the stdlib'   # override one key
ictus run ~/pipelines/needs-council -f other-input.md

ictus trace pipelines/my-thing # what each step of the last run actually did
ictus lint pipelines/          # composition problems
ictus emit pipelines/          # each folder's build/
ictus validate pipelines/      # Conductor's own validator
ictus preflight pipelines/     # MCP servers, env vars, tokens
```

Run both `lint` and `validate` — neither is sufficient alone.

`trace` reads the engine's event log and reports, per step, how many tool calls
it made that were not just emitting its answer. A step that assessed something
it was only shown a summary of, without opening anything, shows up as a `looked`
count of zero — which is the difference between a considered answer and a
plausible one.

## Gotchas

- **Never `fail` inside a stage or scope.** A failed terminal raises past every
  route its caller declared. Use a scope outcome.
- **`voice` has no tools by default.** Four voices with tools is four agents
  hunting the same file. Pass `tools=None` for the workflow default — and then
  `max_turns` too, which `voice` refuses to be given tools without.
- **A voice reports what it could not check in `unchecked`, not in `concerns`.**
  A lookup that failed is not evidence about the thing being looked for, and a
  blocked voice with nowhere to say so writes a confident recommendation
  instead. The report collects them into `unverified`; `verify` starts there.
- **A step is not a Claude Code session, and the gap is mostly context.** The
  provider pins `setting_sources=[]`, so no `CLAUDE.md`, no settings, no ambient
  skills. `ictus run` passes `--workspace-instructions` by default, which reads
  the *target project's* `AGENTS.md` / `CLAUDE.md` / `.github/instructions/`
  walking up to its git root. `workspace_instructions: false` in `config.yaml`
  turns it off for a run that must behave identically against any checkout.
  What still cannot be recovered is Claude Code's own system prompt — Conductor
  types `system_prompt` as `str | None` and the SDK needs a mapping to name the
  preset, so `ictus.baseline.AGENT_BASELINE` stands in for it.
- **A field the provider ignores is refused, not emitted.** Conductor's schema
  accepts these on any agent; only some providers read them. There is no
  engine-side check, so a workflow setting one on the wrong provider loads,
  validates, runs and does nothing. `lints.HONOURED_BY` is the table and the
  conductor lint refuses the combination, naming who would honour it.

  | Field | Honoured by |
  | --- | --- |
  | `retry` | aca, claude, copilot, hermes, openai |
  | `reasoning` | aca, claude, copilot, hermes, openai |
  | `context_tier` | aca, copilot |
  | `working_dir` | claude-agent-sdk, claude, copilot, openai |
  | `skills` | claude-agent-sdk, claude, copilot, hermes, openai |
  | `plugins` | claude-agent-sdk, copilot |
  | `output_mode` | copilot |

  `timeout_seconds` and `validator` are engine-level and `max_session_seconds`
  is declared by every provider, so all three hold anywhere — `HONOURED_EVERYWHERE`
  records that so nobody re-derives it, and all three are wired.

  `reasoning` and `sandbox` are restricted too, but **Conductor refuses them
  itself** with a message naming the provider and the levels it would accept.
  They live in `VALIDATED_UPSTREAM` and are deliberately *not* linted: repeating
  an upstream check is how you end up maintaining a per-provider level table
  that drifts. The entry criterion for `HONOURED_BY` is that the failure is
  **silent**, not merely that the field is restricted.
- **Three different ways for a step to be bounded, and they are not shades of
  one thing.** `max_turns` counts tool-use rounds; `timeout_seconds` is the
  engine cancelling from outside on wall-clock; `max_session_seconds` asks the
  provider to bound its own session. Setting the last at or above the middle one
  is refused, because the engine gets there first and the number could never
  fire.
- **`reasoning` is per node, and each level is a bill.** `low`/`medium`/`high`/
  `xhigh`/`max` map to roughly 2k/8k/16k/32k/60k thinking tokens *per call*,
  charged whether the step needed them. Per node rather than per workflow
  because the step that synthesises usually needs it and the ones either side
  usually do not. **`claude-agent-sdk` does not support it yet** — Conductor
  refuses the workflow at `ictus validate`, naming the provider, so you find out
  before spending anything. The gap is one assignment upstream: the CLI takes
  `--effort`, the SDK exposes `ClaudeAgentOptions.effort` and maps it to that
  flag, and Conductor's provider reads neither.
- **`validator` catches a wrong answer, not a failed call.** `declared_outputs`
  fixes the shape and `retry` covers a call that fell over; this is the one for
  output that is well-formed, delivered, and wrong. Budget two model calls per
  step where you set it and three where the revision fires. `revise=False` runs
  the check and reports without acting.

  **`claude-agent-sdk` — every demo pipeline's provider — drops `retry` and
  `reasoning`,** the two most-requested items on the wiring backlog. It takes
  `working_dir`, `skills` and `plugins`, which is why those three are the ones
  `AgentNode` exposes.
- **`skills` and `plugins` are tri-state, like `tools`.** Unset takes the
  workflow's default set, `()` denies every one, and a non-empty tuple names
  exactly what to load. An omitted key and `[]` are different instructions.
- **A relative path on an *agent* resolves against `build/`.** `working_dir`,
  and any path entry in `skills` or `plugins`, are resolved against the emitted
  workflow's own directory — which ictus rewrites and prunes. The lint refuses
  them; use an absolute path, a `~/` one, or a template resolved at run time.
  A `shell` step is the exception and keeps its relative `working_dir`: that one
  goes straight to the subprocess and resolves against the directory you
  launched from.
- **A skill on `claude-agent-sdk` must come from a plugin.** The provider raises
  on a skill that lives under no plugin root, so a bare `.claude/skills/x` fails
  at run time. Name it through `plugins` instead.
- **`retry` is for a failed *call*, not a wrong answer.** Re-running a step that
  answered badly buys the same answer twice at full price; that is what
  `converge` is for.
- **Declare the tools a step checks against.** `pipeline.require_executable(
  Executable(name=, purpose=, probe=, setup_hint=))` makes `ictus preflight`
  refuse the launch. A missing reference tool does not crash a step — the step
  concludes the thing is absent, at full price.
- **Set `constant`'s `output_type`** unless the value is free text — `"no"` comes
  back `False`, `"3"` an integer.
- **`bindings` types are ictus's, not the engine's.** Each binding's runtime type
  comes from a YAML load of its rendered text. Use `constant` where it must hold.
- **A `shell` step that declares `outputs` must print a JSON object.** Stdout is
  a contract, not a log; leave `outputs` empty for a command that prints prose.
- **`save_text` writes relative to the run's working directory** — the project
  you launched against, not the pipeline folder.
- **`working_dir` resolves differently for scripts and agents.** A relative one
  on a model call is resolved against the workflow file; on a `shell` step it
  goes straight to the subprocess, so it resolves against the cwd you ran from.
- **Running out of turns kills the run.** The engine allows 50 tool-use rounds
  and then *raises* rather than returning what the step had — and that error is
  not one a scope can turn into an outcome. Set `AgentNode(max_turns=...)` on any
  step whose job is to go and look. `ictus trace` flags a step that hit it.
- **An unset `system_prompt` is an *empty* one, not a default one.** Conductor
  forwards `None` and the SDK sends `--system-prompt ""`, so ictus supplies a
  baseline instead. Set `none` only if you mean a step with no discipline at all.
- **A parallel group's members read each other a round behind, or not at all.**
  Two members run at once, so a sibling's output is not addressable while the
  reader runs — `feed` refuses it. Inside a loop it *is* addressable, one pass
  back: the engine keys a group's result by the group's name and overwrites it
  only when the group next finishes. `feed(..., previous_pass=True)` says you
  mean that, and the lint refuses the flag on a graph with no loop, where the
  reference would render empty every time.
- **A council without `verify=` can only measure consensus.** Its voices read
  what you hand them; if that is a summary, they review the summary.
- **`council` needs `rounds >= 2`.** A voice is satisfied when the report states
  its position; the first round has no report to accept.
- **A gate's free-text field only exists on the branch that asked for one.** ictus
  guards it for you; do not hand-write the reference.
