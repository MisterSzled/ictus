"""The websocket client both watchers stand on: handshake, framing, keepalive."""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
from typing import TYPE_CHECKING

import pytest

from ictus.net.websocket import HandshakeError, WebSocket, connect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ACCEPTED = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"


def _exactly(connection: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ConnectionError("peer went away")
        data += chunk
    return data


def _read_frame(connection: socket.socket) -> tuple[int, bytes]:
    """One client frame, unmasked, as a server reads it."""
    first, second = _exactly(connection, 2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", _exactly(connection, 2))[0]
    mask = _exactly(connection, 4) if second & 0x80 else b""
    payload = _exactly(connection, size) if size else b""
    if mask:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return first & 0x0F, payload


def _serve(behave: Callable[[socket.socket], None]) -> tuple[int, threading.Thread]:
    """A peer that accepts the handshake, then does whatever ``behave`` says."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def run() -> None:
        connection, _ = listener.accept()
        connection.settimeout(5.0)
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        connection.sendall(ACCEPTED)
        try:
            behave(connection)
        finally:
            connection.close()
            listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


@pytest.fixture
def server() -> Iterator[tuple[int, list[str]]]:
    """A socket that completes the handshake, sends a frame, then closes."""
    received: list[str] = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        received.append(request.decode(errors="replace"))
        connection.sendall(ACCEPTED)
        body = b'{"type":"workflow_completed","timestamp":1.0,"data":{}}'
        connection.sendall(bytes([0x81, len(body)]) + body)
        # Brief, and tolerant of nothing arriving: only one test sends a frame,
        # and a blocking recv here would hang the other until its read deadline.
        connection.settimeout(2.0)
        try:
            received.append(repr(connection.recv(4096)))
        except (TimeoutError, OSError):
            received.append("")
        connection.sendall(bytes([0x88, 0x00]))
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield port, received
    listener.close()


# --- the handshake -----------------------------------------------------------


def test_the_client_handshakes_and_reads_a_frame(server: tuple[int, list[str]]) -> None:
    port, received = server
    with WebSocket("127.0.0.1", port, "/ws", headers={"Authorization": "Bearer t"}) as ws:
        messages = list(ws.messages())
    assert json.loads(messages[0])["type"] == "workflow_completed"
    assert "GET /ws HTTP/1.1" in received[0]
    assert "Sec-WebSocket-Key:" in received[0]
    assert "Authorization: Bearer t" in received[0]


def _refuse() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port: int = listener.getsockname()[1]

    def refuse() -> None:
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        connection.close()
        listener.close()

    threading.Thread(target=refuse, daemon=True).start()
    return port


def test_a_refused_handshake_says_the_token_is_the_usual_cause() -> None:
    with pytest.raises(HandshakeError, match="403"):
        WebSocket("127.0.0.1", _refuse(), "/ws")


def test_a_refusal_never_repeats_the_query_string() -> None:
    """Slack's socket URL carries a single-use connection ticket in its query."""
    with pytest.raises(HandshakeError) as caught:
        connect(f"ws://127.0.0.1:{_refuse()}/link/?ticket=SECRET-TICKET&app_id=A1")
    assert "SECRET-TICKET" not in str(caught.value)
    assert "/link/" in str(caught.value)


def test_a_non_websocket_url_is_refused() -> None:
    with pytest.raises(HandshakeError, match="not a websocket scheme"):
        connect("https://slack.com/api/apps.connections.open")


def test_the_query_string_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack puts credentials in it, so dropping it refuses the handshake."""
    seen: dict[str, object] = {}

    class _Fake:
        def __init__(self, host: str, port: int, path: str, **kwargs: object) -> None:
            seen.update({"host": host, "port": port, "path": path, **kwargs})

    monkeypatch.setattr("ictus.net.websocket.WebSocket", _Fake)
    connect("wss://wss-primary.slack.com/link/?ticket=abc&app_id=A1")
    assert seen["host"] == "wss-primary.slack.com"
    assert seen["port"] == 443
    assert seen["tls"] is True
    assert seen["path"] == "/link/?ticket=abc&app_id=A1"


# --- framing -----------------------------------------------------------------


def test_a_sent_frame_is_masked(server: tuple[int, list[str]]) -> None:
    """A client frame must be masked; an unmasked one is a protocol error."""
    port, received = server
    with WebSocket("127.0.0.1", port, "/ws") as ws:
        ws.send('{"type":"gate_response"}')
        list(ws.messages())
    sent = received[1]
    assert "\\x81" in sent, sent
    assert "gate_response" not in sent, "an unmasked payload would be readable"


def test_a_character_split_across_fragments_survives() -> None:
    """Decoding each fragment on its own turns a split character into two marks."""
    text = "café ✓".encode()
    cut = text.index("é".encode()) + 1  # inside the two bytes of é

    def behave(connection: socket.socket) -> None:
        connection.sendall(bytes([0x01, cut]) + text[:cut])  # text, more to come
        connection.sendall(bytes([0x80, len(text) - cut]) + text[cut:])  # continuation, final
        connection.sendall(bytes([0x88, 0x00]))
        _read_frame(connection)

    port, thread = _serve(behave)
    with WebSocket("127.0.0.1", port, "/ws") as ws:
        messages = list(ws.messages())
    thread.join(timeout=5)
    assert messages == ["café ✓"]


# --- keepalive ---------------------------------------------------------------


def test_a_ping_is_answered_with_its_own_payload() -> None:
    """The dashboard drops a connection whose pong does not echo the ping.

    uvicorn's websockets implementation pings every 20 s with four random
    bytes and accepts only a pong carrying those bytes back.
    """
    seen: list[tuple[int, bytes]] = []

    def behave(connection: socket.socket) -> None:
        connection.sendall(bytes([0x89, 4]) + b"\x01\x02\x03\x04")  # ping, unmasked
        seen.append(_read_frame(connection))
        connection.sendall(bytes([0x88, 2]) + struct.pack("!H", 1000))
        seen.append(_read_frame(connection))

    port, thread = _serve(behave)
    with WebSocket("127.0.0.1", port, "/ws") as ws:
        assert list(ws.messages()) == []
    thread.join(timeout=5)
    assert seen[0] == (0xA, b"\x01\x02\x03\x04")
    assert seen[1] == (0x8, struct.pack("!H", 1000)), "the close is answered in kind"


def test_a_close_is_answered_once() -> None:
    """Answering the peer's close and then closing must not say goodbye twice."""
    frames: list[tuple[int, bytes]] = []

    def behave(connection: socket.socket) -> None:
        connection.sendall(bytes([0x88, 0x00]))
        frames.append(_read_frame(connection))
        connection.settimeout(0.5)
        with contextlib.suppress(TimeoutError, OSError):
            frames.append(_read_frame(connection))

    port, thread = _serve(behave)
    with WebSocket("127.0.0.1", port, "/ws") as ws:
        list(ws.messages())
    thread.join(timeout=5)
    assert [opcode for opcode, _ in frames] == [0x8]
