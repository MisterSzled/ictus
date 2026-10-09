# Gotchas that have each cost a real run

Read this when a run behaved in a way the graph did not predict.

The first section is the short one on purpose: `ictus lint` already names each
of those at the line that writes it, with the node, the consequence and the fix.
Repeating the explanation here would be a second copy to go stale.

## Refused while you write it

Recognise the message; you do not have to memorise the rule.

- **A field the chosen provider ignores.** The lint names who would honour it;
  `lints.HONOURED_BY` is the table, in code, so it cannot drift from the check.
- **A skill not reached through `plugins`** on `claude-agent-sdk`. Caught by
  `ictus validate`, so before the launch rather than at the step.
- **A relative path on an agent** — `working_dir`, or a path in `skills` or
  `plugins`. It resolves against the emitted `build/`, which ictus rewrites. A
  `shell` step is the exception: its `working_dir` goes to the subprocess and
  resolves against the directory you launched from — or, for a run started
  from a channel, against the pipeline's own folder.
- **`${VAR}` in text a model reads.** Conductor expands it at load: unset
  refuses the workflow, set puts the *value* in the prompt. Tokens belong in an
  MCP header.
- **A `constant` whose value YAML retypes** — `"3"`, `"true"`, `"null"`.
- **`equals` against a literal of the wrong type.** A route tests the stored
  value, not its rendered text, so `exit_code` is a real `0`.
- **A council with `rounds < 2`.** A voice is satisfied when the report states
  its position, and the first round has no report to accept.
- **A step reaching something the pipeline never declared.** Declare it with
  `require_executable`, and `ictus preflight` refuses the launch. A missing
  reference tool does not crash a step — the step concludes the thing is
  absent, at full price.

## Shape of the graph

- **Never `fail` inside a stage or scope.** A failed terminal raises past every
  route its caller declared. Use a scope outcome.
- **A non-zero exit is not a failure, and declaring `outputs` inverts the
  bug.** Conductor returns `exit_code` beside `stdout` and routes on nothing,
  so the next step runs regardless; declare `outputs` and non-JSON stdout
  raises *before* routes are evaluated, so your failure branch never fires
  either way. Use `try_shell` — and keep `require_executable`, because a
  command that never started raises `ExecutionError`, which `try_shell` does
  not cover.
- **A `shell` step declaring `outputs` must print a JSON object.** Stdout is a
  contract, not a log.
- **A parallel group's members read each other a round behind, or not at all.**
  Siblings run at once, so an output is not addressable while the reader runs.
  Inside a loop it is, one pass back: the engine keys a group's result by the
  group name and overwrites it when the group next finishes.
  `feed(..., previous_pass=True)` says you mean that.
- **A gate's free-text field exists only on the branch that asked for one.**
  ictus guards the reference; do not hand-write it.
- **A loop needs `loop_passes`.** Conductor allows 10 step executions in total,
  so `Pipeline` requires the bound once the graph has a cycle. Each `wait`
  costs an iteration, so a poll of N checks needs roughly 2N.

## What a step costs, and what kills it

- **Running out of turns kills the run.** The engine allows 50 tool-use rounds
  then *raises* rather than returning what the step had — and no scope can turn
  that into an outcome. Set `max_turns` on any step whose job is to go and
  look. `ictus trace` flags a step that hit it.
- **Three ways to bound a step, and they are not shades of one thing.**
  `max_turns`, `timeout_seconds` and `max_session_seconds` bound different
  things; setting the last at or above the middle is refused at composition.
  [reaching-the-project.md](reaching-the-project.md) has the table.
- **`reasoning` is per node, and each level is a bill.**
  `low`/`medium`/`high`/`xhigh`/`max` are roughly 2k/8k/16k/32k/60k thinking
  tokens *per call*, charged whether the step needed them. Per node because the
  step that synthesises usually needs it and the ones either side do not.
  `ictus validate` refuses a level the chosen provider does not offer — `openai`
  stops at `high`, `claude-agent-sdk` takes all five — before you spend
  anything.
- **`retry` is for a failed *call*; `validator` is for a wrong answer.**
  Re-running a step that answered badly buys the same answer twice at full
  price — that is what `converge` is for. `validator` is the one for output
  that is well-formed, delivered and wrong: budget two model calls where you
  set it and three where the revision fires, or `revise=False` to report
  without acting.
- **A step is not a Claude Code session, and the gap is mostly context.** The
  provider pins `setting_sources=[]` — no `CLAUDE.md`, no settings, no ambient
  skills. What you can get back, and how, is in
  [reaching-the-project.md](reaching-the-project.md).
- **An unset `system_prompt` is an *empty* one, not a default.** Conductor
  forwards `None` and the SDK sends `--system-prompt ""`. Set `none` only if
  you mean a step with no discipline at all.
- **`skills` and `plugins` are tri-state; `tools` has only two.** Unset takes
  the workflow default, `()` denies every one, a non-empty tuple names exactly
  what to load — but a named `tools` list is refused, since Conductor's are
  workflow tool names. An omitted key and `[]` are different instructions.
- **`bindings` types are ictus's, not the engine's.** Each binding's runtime
  type comes from a YAML load of its rendered text. Use `constant` where it
  must hold.
- **`save_text` writes relative to the run's working directory** — the project
  you launched against, not the pipeline folder.

## Deliberation

See [deliberation.md](deliberation.md) for choosing between `council` and
`roundtable`.

- **`voice` has no tools by default.** Four voices with tools is four agents
  hunting the same file. `tools=None` takes the workflow default, and that is
  the one case `voice` refuses without `max_turns`: it cannot see how wide the
  default set is. A named list composes without one, so bound it yourself.
- **A voice reports what it could not check in `unchecked`, not `concerns`.** A
  failed lookup is not evidence about the thing looked for, and a blocked voice
  with nowhere to say so writes a confident recommendation instead. The report
  collects them into `unverified`; `verify` starts there.
- **A council without `verify=` measures consensus, not correctness.** Its
  voices read what you hand them; if that is a summary, they review the summary.

## Defaults ictus overrides for you

Nothing to do about these. They are here so a workflow diff reads as
deliberate rather than accidental.

- **`context.mode`** defaults to `accumulate`, under which `input:` is parsed
  and never consulted. ictus emits `explicit`, which is what makes the port
  graph mean anything at run time.
- **`runtime.provider`** defaults to copilot. ictus always emits it, so the
  choice is in the diff rather than discovered on a failed run.
- **Checkpointing** is failure-only, which covers the crash that raises and
  none of the ones that do not — a hung provider, a killed process, a closed
  laptop. ictus emits `checkpoint.every_agent` so `conductor resume` always has
  a point to go back to.
- **`limits.timeout_seconds`** is unset, so nothing bounds elapsed time: a
  budget bounds spend, `max_iterations` bounds step count, and a run can sit for
  hours moving neither. `timeout_seconds` in `config.yaml` sets the ceiling.
