---
name: ictus-pipeline
description: Author, lint and run an ictus pipeline — typed Python compiled to Conductor workflow YAML. Use when creating or editing a pipeline, adding a stage, council or gate, debugging an emit, lint, validate or preflight failure, or pointing a run at a target project.
---

# Authoring an ictus pipeline

ictus compiles typed Python into Conductor workflow YAML. Conductor executes it.
Mistakes are meant to surface while the pipeline is written: a live run costs
money and minutes, a composition error costs nothing.

**Let the tools tell you.** Almost every rule in this project is refused by
something that runs in under a second — the lint rules, the composition errors
raised at the line that writes them, and Conductor's own loader. Write the
graph, run the loop below, read what it says. Do not try to hold the rules in
your head: they are enforced, and the enforcement is more current than any
instruction here.

## The one thing the tools cannot tell you

**Conductor is a `uv tool install` in its own virtualenv. It is NOT importable
from this project's `.venv` or from system python.**

```sh
# WRONG — ModuleNotFoundError from every interpreter you have
python -c "import conductor"

# RIGHT — resolve through the console script
"$(dirname "$(readlink -f "$(command -v conductor)")")/python" \
  -c "import conductor, pathlib; print(pathlib.Path(conductor.__file__).parent)"
```

A `ModuleNotFoundError` here is a fact about which interpreter you asked, **not
about Conductor**. Before writing "ictus cannot express X", grep
`config/schema.py` — `AGENTS.md` has the rest of the ground truth, including
which file settles what and the three ways this check has been got wrong.

## The build loop

```sh
ictus init      pipelines/<name>    # writes CHANGE-ME where a decision goes
ictus lint      pipelines/<name>    # composition rules — free, run constantly
ictus emit      pipelines/<name>    # write build/*.yaml
ictus validate  pipelines/<name>    # Conductor's own loader
ictus preflight pipelines/<name>    # can THIS machine run it?
ictus run       pipelines/<name> --repo ~/work/target
ictus trace     pipelines/<name>    # what each step actually did
```

Run `lint` **and** `validate`; neither is sufficient. `lint` catches what
Conductor's loader cannot see — unreachable nodes, drifted stage contracts,
forward references into a loop. `validate` catches what ictus does not model.

`ictus stdlib` lists every ready-made node, stage and scope; `ictus stdlib
<term>` searches by name or by what a thing does. Look there before writing a
node by hand.

**Done means `lint`, `validate` and `preflight` are clean and `build/` is
committed.** There is nothing here to judge by eye.

## A pipeline is a folder

```text
pipelines/<name>/
  pipeline.py     the graph
  config.yaml     policy: provider, budget, gates
  input.md        this run's values (YAML frontmatter + prose body)
  build/          emitted YAML, committed so a diff shows what runs
```

Because `build/` is committed, any change reaching YAML leaves the tree stale
until you re-emit. `test_committed_yaml_matches_a_fresh_emit` catches it for
this repository's own folders under `demo_work/pipelines/`.

## When you need more

Read these only when the situation calls for one:

- **`docs/reaching-the-project.md`** — a step cannot see the project it is
  meant to work on, or `lint`/`preflight` rejected its tools, bounds, turn
  budget or declarations. In the repository rather than in here, because
  `docs/gotchas.md` points at the same material and two copies drift.
- **[reference/building-the-graph.md](reference/building-the-graph.md)** —
  wiring: `connect` vs `route` vs `feed`, stages, scopes, `branch_on_outcome`.
- **`docs/starting-from-a-channel.md`** — the pipeline should be startable by
  somebody who is not at a terminal. `listen_on`'s service argument is optional
  and usually omitted: naming one makes the *run* report back, which makes it
  declare a credential that `preflight` then refuses to proceed without.
- **`STDLIB.md`, the Gotchas section** — a run behaved in a way the graph did
  not predict. Each one has cost a real run. It is in the
  repository rather than in here so there is one list, not two that disagree.

`STDLIB.md` at the repo root is the full catalogue. `AGENTS.md` is for changing
ictus itself, not for writing a pipeline with it.
