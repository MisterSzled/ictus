"""Protocol, with no ictus in it.

Here rather than beside either caller — a run's dashboard event stream and the
bridge's socket to Slack — because it belongs to neither.
"""

from __future__ import annotations

from ictus.net.websocket import HandshakeError, WebSocket, connect

__all__ = ["HandshakeError", "WebSocket", "connect"]
