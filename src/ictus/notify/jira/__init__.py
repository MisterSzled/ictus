"""Jira, as a place a run can leave what it worked out.

A thin re-export: ``send`` holds the spelling, this names what a pipeline
imports.

Not an audience a run reports at, but a set of items a step comments on, which
is why ``jira_cloud`` sets ``comments`` and not ``announces``.
"""

from __future__ import annotations

from ictus.notify.jira.send import jira_cloud

__all__ = ["jira_cloud"]
