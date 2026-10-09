"""Reading prompt text that lives beside the code instead of inside it.

Named for the act rather than the things: a module with prompt text is a folder
holding its own ``.md`` files, so there is no ``prompts`` directory anywhere for
this to be confused with.
"""

from __future__ import annotations

from ictus.prompting.loader import SUFFIX, PromptMissingError, filename_for, prompt

__all__ = ["SUFFIX", "PromptMissingError", "filename_for", "prompt"]
