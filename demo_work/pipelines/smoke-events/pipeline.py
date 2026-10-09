"""A run that exercises the event surface and costs nothing.

Two gates and one `set` step. No node emits `type: agent`, so no provider is
called and no money is spent, while the run still produces `gate_presented`,
`gate_resolved`, `route_taken`, `set_*` and `workflow_completed` — every event
the subscriber needs to prove itself against.

The only pipeline committed to `demo_work/`: a fixture for checking the engine
surface, not a pipeline anyone should run for its output.
"""

from __future__ import annotations

from ictus import Pipeline
from ictus.stdlib import approval_gate, constant, succeed

pipeline = Pipeline(
    pipeline_id="smoke-events",
    description="Exercise the event surface without calling a provider",
)

gate = pipeline.add(
    approval_gate(
        node_id="smoke_gate",
        description="Answered over the websocket by smoke/subscribe.py",
        prompt="Approve to continue, or reject and leave a note.",
    )
)
noted = pipeline.add(
    constant(node_id="noted", value="approved", description="A step that calls no model")
)
done = pipeline.add(succeed(node_id="done", reason="smoke finished"))

pipeline.set_entry(gate)
pipeline.branch(gate, {"approved": noted, "rejected": done})
pipeline.route(noted, done)
