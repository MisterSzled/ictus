"""Just enough of RFC 6455 to hold one conversation open.

Two peers use it: a run's dashboard, which ``ictus watch`` reads, and Slack's
Socket Mode, which ``ictus-bridge listen`` reads. It knows neither; the
framing is the same.

The implemented subset is one connection, text frames, no extensions, no
compression negotiation, no fragmentation to originate. ``messages`` raises on
anything beyond it rather than guessing.

A pong must carry the ping's payload back (RFC 6455 §5.5.3): the dashboard's
server pings every 20 s and drops the connection 20 s later without one.
"""

from __future__ import annotations

import base64
import contextlib
import secrets
import socket
import ssl
import struct
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ictus.errors import IctusError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = ["HandshakeError", "WebSocket", "connect"]

_FIN = 0x80
_MASKED = 0x80

_CONTINUATION, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

#: Bounds reaching the peer and finishing the handshake, nothing after.
CONNECT_TIMEOUT_SECONDS = 15.0


class HandshakeError(IctusError):
    """The peer refused the connection, usually a credential it won't take."""


class WebSocket:
    """One text-frame conversation. Not thread-safe; one per thread."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = CONNECT_TIMEOUT_SECONDS,
        tls: bool = False,
    ) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            # The default context verifies the certificate and the hostname.
            self._sock = ssl.create_default_context().wrap_socket(self._sock, server_hostname=host)
        self._buffer = b""
        self._said_goodbye = False
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{name}: {value}" for name, value in (headers or {}).items()),
        ]
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status = self._until(b"\r\n\r\n").split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101" not in status:
            self.close()
            # The query string is left out: Slack's is a connection ticket.
            raise HandshakeError(
                f"{host}:{port}{path.split('?', 1)[0]} refused the connection: {status}. "
                "A 403 is the token; read-only routes take none, the socket does."
            )
        # Blocking from here on: a run parked at a gate emits nothing until
        # somebody answers, so any read deadline drops that connection. The
        # peer's pings keep it alive, and `messages` answers them.
        self._sock.settimeout(None)

    # -- framing ------------------------------------------------------------

    def _until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed during handshake")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(marker)
        return head

    def _exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed mid-frame")
            self._buffer += chunk
        taken, self._buffer = self._buffer[:count], self._buffer[count:]
        return taken

    def _frame(self, opcode: int, payload: bytes) -> None:
        """Send one final frame. A client frame must be masked; a server's is not."""
        header = bytearray([_FIN | opcode])
        size = len(payload)
        if size < 126:
            header.append(_MASKED | size)
        elif size < 1 << 16:
            header.append(_MASKED | 126)
            header += struct.pack("!H", size)
        else:
            header.append(_MASKED | 127)
            header += struct.pack("!Q", size)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._frame(_TEXT, text.encode())

    def messages(self) -> Iterator[str]:
        """Each text message, until the peer closes. Answers pings in passing."""
        pending = b""
        while True:
            first, second = self._exactly(2)
            final, opcode = bool(first & _FIN), first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._exactly(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exactly(8))[0]
            # A server must not mask; the key, when present, sits between the
            # length and the payload and is not counted in the length.
            mask = self._exactly(4) if second & _MASKED else b""
            payload = self._exactly(size) if size else b""
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))

            if opcode == _CLOSE:
                # Echo the status back, as the closing handshake asks.
                self._goodbye(payload[:2])
                return
            if opcode == _PING:
                self._frame(_PONG, payload)
                continue
            if opcode == _PONG:
                continue
            if opcode == _BINARY:
                raise NotImplementedError("the peer sent a binary frame; this client is text-only")
            if opcode not in (_TEXT, _CONTINUATION):
                raise NotImplementedError(f"unsupported websocket opcode {opcode:#x}")

            # Decoded once, at the end: a fragment boundary can fall inside a
            # multi-byte character.
            pending += payload
            if final:
                yield pending.decode(errors="replace")
                pending = b""

    def _goodbye(self, status: bytes) -> None:
        """Send the one close frame a connection gets."""
        if self._said_goodbye:
            return
        self._said_goodbye = True
        with contextlib.suppress(OSError):
            self._frame(_CLOSE, status)

    def close(self) -> None:
        """Say goodbye and hang up. Safe to call twice."""
        self._goodbye(b"")
        with contextlib.suppress(OSError):
            self._sock.close()

    def __enter__(self) -> WebSocket:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def connect(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> WebSocket:
    """Open ``ws://`` or ``wss://``, with the query string kept.

    Slack puts credentials in the query.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise HandshakeError(f"{parts.scheme!r} is not a websocket scheme; use ws or wss")
    tls = parts.scheme == "wss"
    port = parts.port or (443 if tls else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return WebSocket(
        parts.hostname or "127.0.0.1", port, path, headers=headers, timeout=timeout, tls=tls
    )
