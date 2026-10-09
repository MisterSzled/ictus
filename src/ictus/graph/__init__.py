"""The composition model — what a pipeline is made of.

No re-exports: ``ictus/__init__.py`` is the one public surface. Inside the
library, import the module you mean.

    values.py       the value domain that crosses a node boundary
    ports.py        typed inputs and outputs
    signals.py      the run signals a step can stand in front of
    ref.py          references, templates, and the conditions written over them
    node.py         one class per Conductor agent type
    mapping.py      run-time fan-out: one body, run once per item
    requirements.py what the machine must provide before a run starts
    composition.py  the records a graph is built from: edges, groups,
                    parameters, listeners, and the words a run's settings use
    pipeline.py     the graph: the stores those records are kept in, and the
                    refusal that guards each at the call that writes it
    traversal.py    walking a finished graph — what reaches what, where it
                    loops, what it costs
    stage.py        a reusable sub-graph, compiled as a nested workflow
    scope.py        a stage whose failures are values rather than exceptions
"""

from __future__ import annotations
