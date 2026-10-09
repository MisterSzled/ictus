# config.yaml

Read this when deciding how a pipeline runs, as against what it is: which
provider answers the model calls, what it may spend, whether a person confirms
first. `pipeline.py` changes when the work changes; this file changes when the
budget or the provider does.

A pipeline is a folder of three files, drawn in
[running-a-pipeline.md](running-a-pipeline.md). This page is about the second of
them.

`config.yaml` is required; the minimal one is a line. `provider` has no default
because Conductor's is `copilot`, and inheriting that silently is a real bug this
project has already shipped once.

| Setting | Default | |
| --- | --- | --- |
| `provider` | none — required | who answers the model calls: `claude-agent-sdk`, `claude`, `copilot`, `openai`, `hermes`, `aca` |
| `default_model` | the provider's | the model every step uses unless it says otherwise |
| `system_prompt` | ictus's baseline | what every model call is told about how to work; a path, literal text, or `none` |
| `instructions` | none | project context prepended to every prompt; paths relative to the folder |
| `native_tools` | `none` | whether a step that names no tools may read files, run commands or fetch: `none`, `claude_code`, or a list such as `[Read, Grep, Glob]`. Denied, a step answers from memory rather than failing |
| `workspace_instructions` | `true` | whether a run reads the target project's own `AGENTS.md`, `CLAUDE.md` and copilot instructions, walking up to the git root. The provider pins `setting_sources=[]`, so this flag is the only route |
| `start_gate` | `true` | hold at a confirmation gate before anything runs |
| `budget_usd` / `budget_mode` | none / `audit` | what the run may spend; `audit` records the spend, `enforce` stops the run. `budget_mode` without `budget_usd` never reaches the YAML |
| `timeout_seconds` | none — unlimited | wall-clock ceiling on the whole run; the only setting that bounds elapsed time |
| `max_iterations` | derived from the graph | every step execution the run may make, loops included; set it only to override what the graph prices |
| `dashboard` | `true` | accepted but not yet wired; `ictus run --no-web --foreground` is what stops the web UI |

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
ictus preflight pipelines/     # can THIS machine supply what is declared?
```


`trace` reads the engine's event log and reports, per step, how many tool calls
it made that were not just emitting its answer. A step that assessed something
it was only shown a summary of, without opening anything, shows up as a `looked`
count of zero — which is the difference between a considered answer and a
plausible one.
