# Working in this repository

ictus compiles typed Python into Conductor workflow YAML. Conductor executes it.
Mistakes are meant to surface while a pipeline is written: a live run costs money
and minutes, a composition error costs nothing. **An abstraction earns its place
here only by moving a failure earlier** — one that merely moves a failure
somewhere else is worse than none.

**Let the checks tell you.** The test suite, the lint rules and the composition
errors raised at the line that writes them already refuse most of what this file
could say. Prose that restates an enforced rule goes stale; the enforcement does
not, and a count written down here would be the first thing to rot. What follows
is the part no check can carry: the environment, the reasons, and the
conventions nothing holds but you.

```sh
make soundcheck          # ruff, format, mypy strict, pytest, emit, validate
uv run ictus lint      demo_work/pipelines
uv run ictus emit      demo_work/pipelines   # after ANY change that reaches YAML
uv run ictus validate  demo_work/pipelines   # Conductor's own loader
uv run ictus trace     <pipeline>            # what each step actually did
```

`build/` is committed, so any change reaching YAML leaves the tree stale until
you re-emit. Run `lint` **and** `validate`; neither is sufficient alone.

## Where the ground truth is

Conductor lives in its own virtualenv — a **uv tool install**, or a sibling
venv whose console script is linked in. Either way it is *not* importable
from this project's `.venv` or from system python.

```sh
"$(dirname "$(readlink -f "$(command -v conductor)")")/python" \
  -c "import conductor, pathlib; print(pathlib.Path(conductor.__file__).parent)"
```

| File | Settles |
| --- | --- |
| `config/schema.py` | every field a workflow may contain |
| `engine/workflow.py` | what actually executes |
| `providers/` | how each provider is driven |
| `executor/` | what a step does at run time, as against what it accepts |
| `gates/`, `interrupt/` | human gates, and the pause / skip / stop / guidance API |

**`import conductor` failing is a fact about your shell, not about Conductor.**
A council once reported that retry, per-agent timeouts, reasoning effort and
skills all needed engine changes; all four were already fields in
`config/schema.py`, and every voice had given up after one failed import.

**Check the case you are actually claiming.** Three mistakes made twice each:

- *Schema vs executor.* `working_dir` is one spelling on two step kinds that
  behave differently — `ScriptStepDef`'s becomes the subprocess cwd
  (`execution/local.py`, `cwd=spec.working_dir`), `AgentDef`'s is resolved
  against the workflow file (`engine/workflow.py`,
  `_resolve_agent_working_dir`). A docstring covering one case is not evidence
  about the other.
- *ictus vs Conductor.* "The engine supports it" and "ictus exposes it" are
  different questions. A field in `config/schema.py` and absent from `src/ictus`
  is not a closed finding — it is the cheapest kind of open one.
- *Schema vs provider.* `config/schema.py` says what a workflow may **say**;
  `providers/<name>.py` says what your provider actually **reads**. `retry` and
  `context_tier` are on every `AgentDef` and `claude-agent-sdk` reads neither.
  Check `providers/capabilities.py`, then the code that consumes the value.
  `src/ictus/interfaces/conductor/lints.py` sorts the answers by *who notices*:
  `HONOURED_BY` (only the ictus lint guards it), `VALIDATED_UPSTREAM`
  (`conductor validate` already refuses it), `HONOURED_EVERYWHERE`. Before
  adding an entry, emit a workflow setting the field on a provider that ignores
  it and run `conductor validate` — that command decides which set it is in.

## Layers

ictus builds Conductor pipelines. That is the whole job. Anything else is either
an adapter — data a pipeline declares — or not ictus at all.

Each package says what it is in its own `__init__.py`; read those rather than a
second copy here. What is not in any one of them is the direction:

```text
errors ← graph ← stdlib ← assemble      interfaces/conductor/
   prompting ← stdlib  ← runspec           emit/      a pure function of a pipeline
              ← lint ← interfaces         control/   acts on a run that exists
                     ← runs ← bridge      preflight  asks about this machine
notify, sources, plugins   adapters, pure data   net  protocol with no ictus in it
                      everything ← cli   which nothing imports back
```

Three rules hold it, and `tests/test_boundaries.py` states each precisely and
explains itself on failure: **no Conductor spelling above the backend** (nor any
function returning `YamlDict`, since building the engine's document shape *is*
lowering); **no service named outside its own adapter folder or `bridge/`**;
**nothing in `ictus` imports `ictus.bridge` or an adapter constructor by name.**

Why they exist is the part the tests cannot say. Each had been written down
somewhere and held by nothing — which is how the CLI grew a Slack bot, how
`answer.py` came to be typed on a Slack dataclass, and how the one module in the
audience boundary that knew the engine ended up filed under a vendor's name.
`notify` and `interfaces` are different axes: who hears about a run, and what
executes it. A run on any engine can report to any audience.

## Conventions nothing enforces

- **Tests assert behaviour at the public boundary.** A test that would still
  pass with the implementation deleted is not a test. Reproduce a bug with a
  failing test before fixing it.
- **Make invalid states unrepresentable** before adding a runtime check. Prefer
  a `CompositionError` where it is written, over a lint, over a run-time
  failure.
- **Errors carry context** about what was being attempted. Never swallow one to
  simplify a signature.
- **Comment the non-obvious decision**, never restate the code. Most comments
  here record why an alternative was rejected, usually because it failed on a
  live run.
- **Pin versions.** Prefer the standard library; justify each dependency.
- Do not commit, push or rewrite history unless asked.

## Reference

`README.md` the architecture · `docs/` a page per decision, indexed from
`STDLIB.md` · `STDLIB.md` the catalogue, or `ictus stdlib` for
the same from live code · `CHANGELOG.md` what moved and what to write instead ·
`.claude/skills/ictus-pipeline/` authoring a pipeline ·
`.claude/skills/skill-writer/` writing a skill.

GPL-3.0-or-later, in `LICENSE`. Contributions are under the same terms; a new
dependency needs a compatible licence.

## What this is for

Not to replace an interactive coding agent. Those are better at open-ended work
done once, because they accumulate context, iterate against reality and take
correction mid-flight. This is for work you do repeatedly with a shape you have
already learned: the same review every change, the same provisioning every
deploy, gates in known places, a cost ceiling, and a run a second person can
read afterwards.
