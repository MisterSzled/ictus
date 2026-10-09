"""What Slack can refuse, and which refusals are worth retrying.

The package's shared vocabulary: ``watch`` raises the same two errors over a
different transport, and used to import them from ``listen`` — a module named
for the other way in.
"""

from __future__ import annotations

from ictus.errors import IctusError

__all__ = ["SlackError", "SlackUnreachableError", "refused"]


class SlackError(IctusError):
    """Slack refused the connection: the app-level token, or its scope."""


class SlackUnreachableError(SlackError):
    """Slack could not be reached, or asked to be tried later."""


#: Slack refusing the credential itself. Dialling again changes nothing.
_FATAL = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "not_allowed_token_type",
        "missing_scope",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "invalid_token",
    }
)


def refused(error: str) -> bool:
    """Whether Slack refused the credential itself, so asking again is pointless."""
    return error in _FATAL
