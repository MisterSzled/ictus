"""Acting on a run that already exists, rather than composing one.

* ``triggers`` — what could be started, read from the manifests ``ictus emit``
  wrote beside the workflows; a launcher needs only this JSON.
* ``launch`` — something asked, so start one and say where to watch it.
* ``answer`` — somebody chose a waiting gate's option, so find the run that
  asked and give it the answer.

All service-neutral. What arrives here is a ``Press`` or an ``Asked``;
translating a service's envelope into one happens in ``bridge``.
"""

from __future__ import annotations

from ictus.runs.answer import Outcome, Press, resolve, submit
from ictus.runs.launch import Asked, Started, start
from ictus.runs.triggers import DEFAULT_PREFIX, Need, Trigger, triggers_in

__all__ = [
    "DEFAULT_PREFIX",
    "Asked",
    "Need",
    "Outcome",
    "Press",
    "Started",
    "Trigger",
    "resolve",
    "start",
    "submit",
    "triggers_in",
]
