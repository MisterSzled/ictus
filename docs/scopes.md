# Working with scopes

Read this when a scope is in the graph and you need the detail the index does
not carry: the rules that hold for all of them, and the two with a trap in them.
`STDLIB.md` lists every scope and its outcomes; `ictus stdlib <name>` has the
parameters and the ports of one.

## Rules for every scope

`halted` exists on `council` and `roundtable` only with `interject=True`; without
it the scope has two endings, and `branch_on_outcome` refuses a route to one it
cannot reach.

`Attempt(node_id, prompt, produces)` — a sequence becomes a chain, each step
reading the last. `Voice(node_id, persona, focus, tools=, max_turns=)` and
`Speaker(node_id, persona, focus, tools=, max_turns=)`.

The outcome names are constants on `ictus.stdlib`: `AGREED`, `UNRESOLVED`,
`HALTED`, `CONVERGED`, `EXHAUSTED`, `ANSWERED`, `READ`, `MISSING`, `OK`,
`FAILED`. Each is defined once, in `stdlib/scopes/outcomes.py`, because several
scopes end the same ways — `council` and `roundtable` both agree or fail to,
`converge` and `investigate` both run out.

## `try_shell`, and the stdout contract

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
- **What `try_shell` does not convert: a command with no exit code.** A
  missing binary and a `timeout` both leave the executor as an `ExecutionError`,
  which is not a value and not routable. Declare the tool with
  `require_executable` so `ictus preflight` refuses the launch instead.
- **`outputs` on a `try_shell` must be printed on every zero exit.** Conductor
  renders with `StrictUndefined`, so a declared field the command omitted raises
  at the reference — reinstating, on the success path only, the crash the scope
  removes. The `failed` exit does not read them; they arrive there as empty
  values of their declared type.

## Bounding what a scope accumulates

- **A context ceiling is per *stage*, and only one strategy survives a loop.**
  `Pipeline(context_max_tokens=, context_trim=)` emits `workflow.context`, and
  a stage is its own workflow file — so a long council can be bounded without
  bounding its caller. There is no per-node
  equivalent: `AgentNode.context_tier` picks a window, not a
  trim point. Trimming is the only thing that removes a step's
  output from a run, and a loop reads the previous pass through exactly those
  entries: once one is deleted the reference renders empty and is
  indistinguishable from a first pass, so the loop keeps going and stops
  deliberating. `TrimStrategy.TRUNCATE` shortens fields in place and leaves
  every reference resolvable; the lint refuses `DROP_OLDEST` and `SUMMARIZE` on
  a graph that loops, and refuses a ceiling with no strategy at all — the engine
  does not leave that unset, it uses `drop_oldest`.
## `roundtable`, and who speaks first

- **A `roundtable` without `study` is anchored by construction.** Turns are
  sequential, so only the first speaker ever states a view nobody influenced;
  everyone after it speaks into a frame somebody else set, and four people
  agreeing means one person plus three confirmations. `study=` makes each person
  commit to a public `opening` before anybody speaks, so the minutes are handed
  both ends and can say which argument moved whom — convergence and capitulation
  are identical in the final positions and only distinguishable against the
  openings.
