"""Jira, as a place a run can leave what it worked out.

A thin re-export, the same shape as ``ictus.notify.slack``: the module below
holds the spelling, this names what a pipeline imports.

Unlike a channel, Jira is not an audience a run reports *at* — there is nothing
to subscribe to a signal. It is a set of items a step comments *on*, addressed
by something the graph carries, which is why ``jira_cloud`` sets ``comments``
and not ``announces``.
"""

from __future__ import annotations

from ictus.notify.jira.send import jira_cloud

__all__ = ["jira_cloud"]
