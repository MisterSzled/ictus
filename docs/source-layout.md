# Source layout

For working *on* ictus rather than with it. `AGENTS.md` has the rules each
layer is held to and the dependency direction; this is the tree and the
stdlib's grouping.

## Where things live

`tests/test_boundaries.py` enforces the layer rules `AGENTS.md` states.

`ictus/__init__.py` is the only public surface — `ictus.graph` deliberately
re-exports nothing, so there is one place a name is exported from rather than
two that can drift. Nothing in `src/` imports from `demo_work/`: the demos are
there to be read and run, and deleting them would not touch the library.

```text
src/ictus/
  __init__.py      the one public surface
  errors.py        what ictus raises — root vocabulary, like __init__
  graph/           the composition model — knows no engine
  stdlib/          ready-made nodes, stages and scopes
  lint/            composition rules true of any graph
  interfaces/      the boundary to whatever executes a graph
    conductor/     every Conductor-shaped thing, and nothing else
      emit/        a graph in, YAML out — no environment, no process
      control/     acting on a run that already exists
  runspec/         what specifies a run: the folder, and what it says
  assemble/        nodes policy inserts that the author did not write
  runs/            launching a run, and answering a gate on one
  notify/          where a run reports to — one folder per destination
  sources/         where a run reads from — one module per engine
  prompting/       reading prompt text that lives beside the code
  plugins/         which adapters this installation has
  net/             protocol, with no ictus in it
  cli/             the `ictus` command
  bridge/          NOT ictus: the chat daemon, its own console script
```

Packages, and the two modules beside them. Each package says what it is in its
own `__init__.py`, and that copy is the one that stays current — a file-by-file
gloss here named `stdlib/prompts.py` for a week after it became
`stdlib/baseline/`.

For adding to it. The README's
[What is already built for you](../README.md#what-is-already-built-for-you) is
the using half, and [STDLIB.md](../STDLIB.md) is the full catalogue.

One primitive per module, so the docstring beside a thing is about that thing.
Folders are named for what each thing is, in `graph.NodeKind`'s vocabulary. They
were once named after Conductor's step kinds, which put the engine's vocabulary
one layer above the only package allowed to know Conductor exists.

| Group | Kind | What's there |
|---|---|---|
| `gates/` | `HUMAN_DECISION`, `ASK` | `approval_gate`, `choice_gate`, `ask_human`, `ask_human_for` |
| `llm/` | `LLM_CALL` | `briefing`, `voice`, `validate_mcp`, `remediate` |
| `steps/` | `COMPUTATION`, `SUBPROCESS`, `DELAY` | `constant`, `bindings`, `counter`, `wait`, `shell`, `save_text`, `announce`, `comment`, `fetch`, `query` |
| `exits/` | `EXIT` | `succeed`, `fail` |
| `stages/` | `SUB_GRAPH` | `briefing_gate`, `resolve_unknowns`, `script_sequence`, `validate_mcps` |
| `scopes/` | `SUB_GRAPH` | `try_shell`, `converge`, `read_ticket`, `investigate`, `roundtable`, `council` |

A stage and a scope both cost their caller one iteration; the difference between
them is in the README's [Three tiers](../README.md#three-tiers), and it is why
`scopes/` is a folder rather than a return annotation.

`llm/` is the one group not re-exported from `ictus.stdlib`: each of those exists
because a stage here needed it, so reaching for one directly names
`ictus.stdlib.llm`.

`council` and `roundtable` look interchangeable in that row and are not:
[deliberation.md](deliberation.md) is the page for choosing between them.
