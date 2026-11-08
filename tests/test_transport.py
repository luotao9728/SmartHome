"""Transport tests: URL parsing, frame masking and the MicroPython handshake.

The node's WebSocket client is hand written (MicroPython has no standard one),
so its framing gets direct tests against a real socket rather than only being
exercised indirectly through the end-to-end suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sys
import time
import threading
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parent.parent / "micropython"
if str(FIRMWARE) not in sys.path:
    sys.path.insert(0, str(FIRMWARE))

from shnode.transport import (  # noqa: E402
    OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, WebSocketClosed, WebSocketError,
    WebsocketClient, jitter, parse_url,
)


# --------------------------------------------------------------------------- #
# URL parsing (no urllib on MicroPython)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://192.168.1.20:8080/ws/node?node=a&token=b",
         ("http", "192.168.1.20", 8080, "/ws/node", "node=a&token=b")),
        ("ws://10.0.0.5/ws/node", ("ws", "10.0.0.5", 80, "/ws/node", "")),
        ("wss://hub.local/ws/node?x=1", ("wss", "hub.local", 443, "/ws/node", "x=1")),
        ("https://hub.local:9000/x", ("https", "hub.local", 9000, "/x", "")),
        ("ws://host", ("ws", "host", 80, "/", "")),
        ("ws://host:81", ("ws", "host", 81, "/", "")),
        ("ws://[fe80::1]:9000/a?b=c", ("ws", "fe80::1", 9000, "/a", "b=c")),
        ("ws://[::1]/x", ("ws", "::1", 80, "/x", "")),
    ],
)
def test_parse_url(url, expected):
    assert parse_url(url) == expected


def test_parse_url_never_raises_on_garbage():
    for url in ("", "://", "ws://", "not a url", "ws://host:abc"):
        scheme, host, port, path, _ = parse_url(url)
        assert isinstance(scheme, str)
        assert isinstance(host, str) and host
        assert isinstance(port, int) and 0 < port < 65536
        assert path.startswith("/")


def test_jitter_is_bounded():
    for _ in range(200):
        value = jitter(10.0)
        assert 10.0 <= value <= 12.5
    assert jitter(0) == 0.0


# --------------------------------------------------------------------------- #
# masking
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [0, 1, 3, 4, 5, 125, 126, 127, 200, 1000, 65535])
def test_mask_round_trip(size):
    payload = bytes((index * 37) & 0xFF for index in range(size))
    mask = b"\xde\xad\xbe\xef"
    masked = bytearray(payload)
    for index in range(len(masked)):
        masked[index] ^= mask[index & 3]
    assert WebsocketClient._unmask(bytes(masked), mask) == payload


def test_unmask_handles_bytearray_input():
    assert WebsocketClient._unmask(bytearray(b"abc"), b"\x00\x00\x00\x00") == b"abc"


# --------------------------------------------------------------------------- #
# framing against a scripted server
# --------------------------------------------------------------------------- #
def frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """Build an unmasked (server -> client) frame."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend([(length >> 8) & 0xFF, length & 0xFF])
    else:
        header.append(127)
        for shift in (56, 48, 40, 32, 24, 16, 8, 0):
            header.append((length >> shift) & 0xFF)
    return bytes(header) + payload


def parse_client_frame(reader) -> tuple:
    """Parse a masked client frame as the hub would."""
    import base64

    head = reader(2)
    first, second = head[0], head[1]
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        raw = reader(2)
        length = (raw[0] << 8) | raw[1]
    elif length == 127:
        raw = reader(8)
        length = 0
        for byte in raw:
            length = (length << 8) | byte
    mask = reader(4)
    payload = bytearray(reader(length))
    for index in range(length):
        payload[index] ^= mask[index & 3]
    return opcode, bytes(payload)


class ScriptedServer:
    """A one-shot TCP server that speaks just enough WebSocket."""

    def __init__(self, script):
        self.script = script
        self.received = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.error = None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        try:
            connection, _ = self.sock.accept()
            connection.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data += chunk
            headers = {}
            for line in data.split(b"\r\n")[1:]:
                if b":" in line:
                    name, value = line.split(b":", 1)
                    headers[name.strip().lower()] = value.strip()
            key = headers.get(b"sec-websocket-key", b"")
            import base64

            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )

            def read_exactly(count):
                buffer = b""
                while len(buffer) < count:
                    chunk = connection.recv(count - len(buffer))
                    if not chunk:
                        raise ConnectionError("closed")
                    buffer += chunk
                return buffer

            self.script(self, connection, read_exactly)
        except Exception as exc:  # noqa: BLE001
            self.error = exc


async def test_handshake_and_text_message_round_trip():
    def script(server, connection, read_exactly):
        opcode, payload = parse_client_frame(read_exactly)
        server.received.append((opcode, payload))
        connection.sendall(frame(OP_TEXT, json.dumps({"t": "hello"}).encode()))

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect("ws://127.0.0.1:%d/ws/node?a=1" % server.port)
        await client.send_json({"t": "hello", "node": "x"})
        message = await asyncio.wait_for(client.receive_json(), 5)
        assert message == {"t": "hello"}
        await client.close()
        server.thread.join(timeout=5)
    assert server.error is None
    opcode, payload = server.received[0]
    assert opcode == OP_TEXT
    assert json.loads(payload) == {"t": "hello", "node": "x"}


async def test_client_frames_are_masked():
    def script(server, connection, read_exactly):
        # Re-read a raw frame to confirm the mask bit is set, then close
        # politely so the client sees a normal end of stream.
        head = read_exactly(2)
        server.received.append((head[0] & 0x0F, bool(head[1] & 0x80)))
        connection.sendall(frame(OP_CLOSE, b"\x03\xe8"))
        time.sleep(0.05)

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect("ws://127.0.0.1:%d/" % server.port)
        await client.send_json({})
        with pytest.raises(WebSocketClosed):
            await asyncio.wait_for(client.receive(), 5)
        server.thread.join(timeout=5)
    assert server.received[0] == (OP_TEXT, True), "client frames must be masked"


async def test_fragmented_message_is_reassembled():
    def script(server, connection, read_exactly):
        connection.sendall(frame(OP_TEXT, b'{"t":', fin=False))
        connection.sendall(frame(0, b'"state",', fin=False))
        connection.sendall(frame(0, b'"v":1}', fin=True))

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect("ws://127.0.0.1:%d/" % server.port)
        message = await asyncio.wait_for(client.receive_json(), 5)
        await client.close()
        server.thread.join(timeout=5)
    assert message == {"t": "state", "v": 1}


async def test_ping_is_answered_and_skipped():
    def script(server, connection, read_exactly):
        connection.sendall(frame(OP_PING, b"hi"))
        # Expect a pong from the client before the text frame.
        opcode, payload = parse_client_frame(read_exactly)
        server.received.append((opcode, payload))
        connection.sendall(frame(OP_TEXT, b'{"t":"ok"}'))

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect("ws://127.0.0.1:%d/" % server.port)
        message = await asyncio.wait_for(client.receive_json(), 5)
        await client.close()
        server.thread.join(timeout=5)
    assert message == {"t": "ok"}
    assert server.received[0] == (OP_PONG, b"hi")


async def test_close_frame_raises():
    def script(server, connection, read_exactly):
        connection.sendall(frame(OP_CLOSE, b"\x03\xe8"))

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect("ws://127.0.0.1:%d/" % server.port)
        with pytest.raises(WebSocketClosed):
            await asyncio.wait_for(client.receive(), 5)
    assert client.closed is True


async def test_oversized_frame_is_rejected():
    def script(server, connection, read_exactly):
        connection.sendall(frame(OP_TEXT, b"x" * 5000))

    with ScriptedServer(script) as server:
        client = await WebsocketClient.connect(
            "ws://127.0.0.1:%d/" % server.port, max_frame=1024
        )
        with pytest.raises((WebSocketError, WebSocketClosed)):
            await asyncio.wait_for(client.receive(), 5)


async def test_server_without_websocket_is_refused():
    """A plain HTTP 404 must surface as a WebSocketError, not a hang."""
    server_sock = socket.socket()
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    def serve():
        connection, _ = server_sock.accept()
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        connection.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with pytest.raises(WebSocketError):
            await asyncio.wait_for(
                WebsocketClient.connect("ws://127.0.0.1:%d/" % port), 10
            )
    finally:
        server_sock.close()


async def test_connect_to_a_closed_port_fails_quickly():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(Exception):
        await asyncio.wait_for(WebsocketClient.connect("ws://127.0.0.1:%d/" % port), 10)
