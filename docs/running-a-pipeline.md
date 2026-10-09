# Running a pipeline

Read this for what a pipeline folder has to contain, the gate that stands in
front of every run, and how a step asks a person for something.

- [preflight.md](preflight.md) — what is checked before anything is spent.
- [configuration.md](configuration.md) — `config.yaml`: provider, budget, gates.

## The running contract

A pipeline is a folder, not a module — three files, three questions:

    pipelines/needs-council/
      pipeline.py            what the graph is       (composition)
      config.yaml            how it runs             (policy)
      input.md               what to run it on       (this run's values)
      build/                 emitted YAML, committed

`ictus init <folder>` writes all three. `config.yaml` is required and the minimal
one is a line:

    provider: claude-agent-sdk

`provider` has no default because Conductor's is `copilot`, and a pipeline that
silently inherited it is how four emitted workflows once ran somewhere nobody
chose. Policy lives here rather than in the composition so a pipeline moves
between providers without editing Python, and so a value set in both places and
set differently is refused instead of silently resolved.

`input.md` is YAML frontmatter over a Markdown body — the shape Conductor
already uses for `SKILL.md` and plugin agents, so it is one convention across
both tools. Frontmatter holds the short values; the body is the long one, and
which input it feeds is declared once with `declare_input(..., prose=True)`.

    ---
    scope: the stdlib and the CLI      # a declared input
    repo: ../../some-project           # optional; relative to this file
    ---
    The body feeds the input declared with prose=True.

`repo:` is the one reserved key; every other key must be a declared input, and
one matching nothing is refused rather than ignored: `scpoe:` doing nothing
quietly is how a run does the default thing and nobody notices until the output
is wrong.

The work happens in the directory you invoke it from, so `cd` to a project and
go. `repo:` — or `--repo` — overrides that, which is how a run that touches a
particular checkout says so in something you can commit.

## Nothing starts without a person

Every pipeline gets a confirmation gate at its entry point, unless its
`config.yaml` says `start_gate: false`. The run loads, the dashboard comes up
with the whole graph in it and the actual input values rendered in the prompt,
and nothing happens until someone chooses.

This exists because Conductor's dashboard is a view onto a live engine, not a
launcher: the web API has `stop`, `kill` and `resume` but no start, and
`--dry-run` returns before the dashboard is built. A human gate costs no provider
call and no money, so it is the one mechanism that can hold a run open for
inspection. It is added at emit time, so what is committed in `build/` is what
runs — a confirmation step that only appeared at launch would make the artifact
a lie.

Declining is a *success*: nothing was attempted, so nothing failed, and a caller
should not have to treat "a person looked at it and said no" as an error.

## Asking a person for something

A gate offers a decision among known options. `ask_human` collects *values* — a
path, an id, a name — and all of them cost one iteration together, not one each:

```python
ask_human(
    questions=(
        Question(id="api_path", text="Where is the API repo checked out?", required=True),
        Question(id="env", text="Which environment?", choices=("staging", "prod")),
    )
)
```

Each named question becomes a typed output port, so `ask.ref("api_path")` is
checked and renders `{{ ask.output.answers.api_path }}`.

Some questions cannot be written in advance — a ticket touching an unknown
number of repositories has an unknown number of questions. `ask_human_for`
takes them from an upstream node instead, and `resolve_unknowns` is the whole
pattern: work out what is missing, ask **only** about that, carry the answers
out.

Asking unconditionally is the easy version and the wrong one. A pipeline that
stops to ask about things it already knows gets skipped past, and then the one
time it mattered nobody read it either.
