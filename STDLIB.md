# ictus stdlib

Ready-made pieces, all built from the public API. `from ictus.stdlib import ...`

**This page is the index; `ictus stdlib <name>` is the entry.** The command reads
`__all__`, `inspect.signature` and the docstrings, so every parameter and every
declared output it prints comes from the installed library and cannot drift from
it. What a table does that a command cannot is let you scan, so that is all these
are. `ictus stdlib <term>` searches by name or by what a thing does.

Laid out by what each thing *is*, in `graph.NodeKind`'s own vocabulary — gates,
model calls, steps, exits, stages, scopes. Each of these headings is
a folder under `src/ictus/stdlib/`.

| Tier | Placed with | Costs the caller |
| --- | --- | --- |
| **Node** | `pipeline.add(...)` | 1 iteration |
| **Stage** | `stage.instantiate(parent)` | 1 iteration, whatever it contains |
| **Scope** | `scope.instantiate(parent)` + `parent.branch_on_outcome(...)` | 1 iteration |

A **Scope** is a stage whose every exit is an outcome you route on, instead of a
failure that kills the caller.

## Model calls

`from ictus.stdlib.llm import ...` — not the flat namespace. Each exists because
a stage below needed it; reaching for one directly is deliberate enough to name
where it came from.

| Constructor | Use |
| --- | --- |
| `briefing` | Summarise upstream output for a person to decide on |
| `voice` | One standpoint's assessment — persona plus focus |
| `validate_mcp` | Prove an MCP server is reachable by calling a read-only tool |
| `remediate` | Work through a blockage with the person waiting at the gate |

## Human decisions

| Constructor | Use |
| --- | --- |
| `approval_gate` | Approve or reject, free text on reject |
| `choice_gate` | Arbitrary `(value, label)` options |
| `ask_human` | Ask questions known at composition time |
| `ask_human_for` | Ask questions an earlier node produced |

Route a gate with `pipeline.branch(gate, {...})`; `ask_human` and `ask_human_for`
collect values rather than offering a decision, so they route with `pipeline.route(...)`.
`Question(text, id=, choices=, required=)`.

## Steps without a model
| Constructor | Use |
| --- | --- |
| `constant` | One computed value |
| `bindings` | Several named values at once |
| `counter` | Count passes through a point, from one |
| `announce` | Report to an integration from inside the graph |
| `comment` | Add a remark to an item an integration names — a ticket, an issue |
| `fetch` | Ask a read-only datasource for one named thing — a ticket, by key or link |
| `query` | Ask a read-only datasource one statement |
| `save_text` | Write a value another step produced to a file |
| `shell` | Run a command |
| `wait` | Pause |

A run that has to tell somebody what it is doing is
[docs/reporting.md](docs/reporting.md).

## Exits

| Constructor | Use |
| --- | --- |
| `succeed` | End the run successfully |
| `fail` | End as failed, non-zero exit |

## Stages

| Constructor | Use |
| --- | --- |
| `briefing_gate` | Turn data into a human decision and report which was taken |
| `resolve_unknowns` | Work out what is missing, ask only about that |
| `script_sequence` | Chain commands, threading each output into the next |
| `validate_mcps` | Prove every declared MCP server is reachable, with a fix-it loop |

`ReviewOption(value, label, ask_for_notes=)`; default pair is `APPROVE_OR_REJECT`.
`ScriptStep(node_id, command, args, output, receives_previous=)`.

## Scopes
| Constructor | Use | Outcomes |
| --- | --- | --- |
| `try_shell` | Run one command whose failure the caller routes on | `ok`, `failed` |
| `converge` | Bounded try/judge loop; running out is a value, not a crash | `converged`, `exhausted` |
| `read_ticket` | Read one ticket through a read-only source, holding its credential inside the stage | `read`, `missing` |
| `investigate` | Bounded look/ask loop against a read-only datasource; the thinking step has no tools by default and can only request a statement | `answered`, `exhausted` |
| `roundtable` | Several people taking turns, in order, until they agree | `agreed`, `unresolved` |
| `council` | Several standpoints deliberating until they agree on a report | `agreed`, `unresolved` |

`interject=True` adds a third outcome, `halted`, to `council` and `roundtable`.
[docs/scopes.md](docs/scopes.md) has that and the rest of what holds for every
scope; [docs/deliberation.md](docs/deliberation.md) is for choosing between the
two and pricing their options.

## Where the rest of it went

Everything that was prose around these tables moved to a page named for the
question it answers, so you load the one you are asking.

| Read | When |
| --- | --- |
| [docs/wiring.md](docs/wiring.md) | connecting nodes — `connect` vs `route` vs `feed`, and conditions that are true at run time |
| [docs/scopes.md](docs/scopes.md) | the outcomes, spec types and rules shared by every scope |
| [docs/deliberation.md](docs/deliberation.md) | choosing between `council` and `roundtable`, and what a council's `verify` buys |
| [docs/reporting.md](docs/reporting.md) | a run has to tell somebody what it is doing |
| [docs/configuration.md](docs/configuration.md) | `config.yaml` — provider, budget, gates |
| [docs/preflight.md](docs/preflight.md) | what is checked before anything is spent |
| [docs/running-a-pipeline.md](docs/running-a-pipeline.md) | the folder contract and the start gate |
| [docs/reaching-the-project.md](docs/reaching-the-project.md) | a step cannot see the project it is meant to work on |
| [docs/gotchas.md](docs/gotchas.md) | a run behaved in a way the graph did not predict |
| [docs/source-layout.md](docs/source-layout.md) | the package tree, for working on ictus |
| `ictus stdlib <name>` | every parameter and output of one constructor, off the installed library |
