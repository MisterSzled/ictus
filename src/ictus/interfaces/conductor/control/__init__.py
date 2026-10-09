"""Driving and reading a run that is already happening.

Finding live runs, subscribing to their events, answering a gate, reading back
what each step did. None of it is reachable from the emit path — nothing in
``emit`` imports anything here, which is the property that made the split
obvious once somebody looked.

Everything here needs a machine with Conductor on it and a run in flight.
Everything in ``emit`` needs neither.
"""

from __future__ import annotations

__all__: list[str] = []
