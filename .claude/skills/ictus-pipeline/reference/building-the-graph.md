# Building the graph

Loaded when wiring nodes, stages and scopes — what `connect`, `route`, `feed`
and `branch_on_outcome` each do, and which to reach for.

A whole pipeline — one input, one agent, one route to `END`:

```python
from ictus import END, AgentNode, InputPort, OutputPort, Pipeline, PortType, tpl

STR = PortType.STRING

pipeline = Pipeline(pipeline_id="my-pipeline", description="What this does")
brief = pipeline.declare_input("brief", STR, prose=True, description="What to work from")

work = pipeline.add(
    AgentNode(
        node_id="work",
        inputs=(InputPort("brief", STR),),
        prompt=tpl("Do the thing described below.\n\n", brief.ref()),
        declared_outputs=(OutputPort("result", STR, "What it produced"),),
        tools=None,
        max_turns=200,
    )
)
pipeline.set_entry(work)
pipeline.connect_input(brief, work, "brief")
pipeline.route(work, END)
```

Wiring — pick by what has to travel:

| Call | Carries | Use when |
| --- | --- | --- |
| `connect(src, port, dst, port)` | control + data | target runs next *and* reads the value |
| `route(src, dst, when=)` | control only | target needs nothing from the source |
| `feed(src, port, dst, port)` | data only | the value crosses a gate or a branch |
| `connect_input(param, dst, port)` | a workflow input | binding the pipeline's parameters |

References are objects, never strings. `node.ref("port")` fails at composition
if that port is not declared. `ref_to("id", "port", TYPE)` names a node that does
not exist yet — a loop's back-edge — and is resolved by the lint against the
finished graph. A reference that may not have resolved yet is guarded for you,
so on its own it renders to nothing on the first pass; what `optional(...)`
adds is that the prose introducing it goes with it, instead of a heading left
standing over nothing for the model to fill in.

Three tiers: a **Node** is one `agents:` entry; a **Stage** is its own YAML file
plus a `type: workflow` agent in the parent; a **Scope** is a stage whose
endings the caller routes on with `branch_on_outcome` (`STDLIB.md` defines all
three).

**Several agents on one question: `council` polls, `roundtable` talks** —
breadth against argument, paid for in wall-clock. Pass `study` or the first
speaker frames the table, and give `max_turns` to anyone given tools.
`docs/deliberation.md` is the page for choosing, and for what each option costs.

Reach for `ictus.stdlib` before hand-rolling. `ictus stdlib` prints the whole
catalogue from the installed library and cannot drift from it; some of what is
in there: `council`, `roundtable`, `converge`, `try_shell`, `briefing_gate`,
`resolve_unknowns`, `validate_mcps`, `script_sequence`, `approval_gate`,
`choice_gate`, `ask_human`, `shell`, `save_text`, `constant`, `counter`,
`wait`, `succeed`, `fail`.

