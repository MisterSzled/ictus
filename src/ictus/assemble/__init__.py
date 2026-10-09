"""Nodes the author did not write, inserted before the graph is compiled.

Both turn a line of policy into graph structure: ``start_gate: true`` becomes
a human gate in front of the entry point, and an attached integration becomes
announcements in front of every gate and every ending.

At load, after policy is read and before emission, so the committed YAML
contains everything policy added and deleting the line deletes it all.
"""

from __future__ import annotations

from ictus.assemble.announcements import OPENER_ID, Attached, apply_integrations
from ictus.assemble.start_gate import CANCELLED_ID, GATE_ID, add_start_gate, attach_start_herald

__all__ = [
    "CANCELLED_ID",
    "GATE_ID",
    "OPENER_ID",
    "Attached",
    "add_start_gate",
    "apply_integrations",
    "attach_start_herald",
]
