"""Model calls — the recurring shapes worth a prompt and a declared schema.

Ordinary ``LLM_CALL`` nodes, carrying a prompt and a declared output schema.

Not re-exported from ``ictus.stdlib``: each exists because a stage in this
library needed it.
"""

from __future__ import annotations

from ictus.stdlib.llm.briefing import briefing
from ictus.stdlib.llm.remediate import remediate
from ictus.stdlib.llm.validate_mcp import validate_mcp
from ictus.stdlib.llm.voice import SATISFIED, voice

__all__ = ["SATISFIED", "briefing", "remediate", "validate_mcp", "voice"]
