"""A minimal RFC 6455 WebSocket client.

MicroPython has no websocket in its standard library and ``micropython-lib``'s
``websocket-client`` does not exist on every board, so this module implements
just enough of the protocol:

* client handshake with ``Sec-WebSocket-Key``,
* masked text frames (required from clients),
* fragmentation, ping/pong and close frames,
* a configurable read timeout for the heartbeat.

It deliberately avoids ``memoryview`` slicing quirks across ports and works on
both MicroPython and CPython, which lets the same firmware be exercised in CI.
"""

import binascii
import hashlib
import os

from .compat import asyncio, json as json_module, random_float, sleep_ms

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_DEFAULT_PORTS = {"ws": 80, "http": 80, "wss": 443, "https": 443}


def parse_url(url: str) -> tuple:
    """Split ``scheme://host[:port]/path?query`` without ``urllib``.

    MicroPython has no ``urllib.parse`` on most builds, and the parts we need
    are trivial to extract.
    """
    scheme = "ws"
    rest = url.strip()
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    scheme = scheme.lower()
    slash = rest.find("/")
    if slash < 0:
        authority, path = rest, "/"
    else:
        authority, path = rest[:slash], rest[slash:]
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    host = authority
    port = _DEFAULT_PORTS.get(scheme, 80)
    if authority.startswith("["):  # IPv6 literal
        end = authority.find("]")
        host = authority[1:end]
        remainder = authority[end + 1:]
        if remainder.startswith(":"):
            port = int(remainder[1:])
    elif ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        if port_text.isdigit():
            port = int(port_text)
    if not host:
        host = "127.0.0.1"
    if not path:
        path = "/"
    return scheme, host, int(port), path, query


class WebSocketError(Exception):
    """Protocol or transport failure."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (or it went away)."""


def _b64(data: bytes) -> str:
    return binascii.b2a_base64(data).strip().decode()


class WebsocketClient:
    """Blocking-free websocket connection built on asyncio streams."""

    def __init__(self, reader, writer, *, max_frame: int = 65536):
        self.reader = reader
        self.writer = writer
        self.max_frame = max_frame
        self._closed = False

    # -- handshake -------------------------------------------------------- #
    @classmethod
    async def connect(cls, url: str, *, timeout: float = 10.0, headers=None,
                      ssl_context=None, max_frame: int = 65536) -> "WebsocketClient":
        scheme, host, port, path, query = parse_url(url)
        secure = scheme in ("wss", "https")
        if query:
            path += "?" + query

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context if secure else None),
            timeout,
        )
        key = _b64(os.urandom(16))
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "Origin: http://%s:%d" % (host, port),
        ]
        for name, value in (headers or {}).items():
            lines.append("%s: %s" % (name, value))
        request = ("\r\n".join(lines) + "\r\n\r\n").encode()
        writer.write(request)
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout)
        if not status_line:
            raise WebSocketError("no handshake response")
        if b" 101 " not in status_line:
            detail = status_line.strip().decode(errors="replace")
            # Drain the headers so the socket is not left mid-response.
            for _ in range(16):
                line = await asyncio.wait_for(reader.readline(), timeout)
                if not line or line in (b"\r\n", b"\n"):
                    break
            raise WebSocketError("handshake rejected: %s" % detail)

        accept_header = b""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if not line or line in (b"\r\n", b"\n"):
                break
            if line.lower().startswith(b"sec-websocket-accept:"):
                accept_header = line.split(b":", 1)[1].strip()
        expected = _b64(hashlib.sha1((key + _GUID).encode()).digest()).encode()
        if accept_header and accept_header != expected:
            raise WebSocketError("bad Sec-WebSocket-Accept")
        return cls(reader, writer, max_frame=max_frame)

    # -- sending ---------------------------------------------------------- #
    def _write_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketClosed("socket closed")
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.append((length >> 8) & 0xFF)
            header.append(length & 0xFF)
        else:
            header.append(0x80 | 127)
            for shift in (56, 48, 40, 32, 24, 16, 8, 0):
                header.append((length >> shift) & 0xFF)
        mask = os.urandom(4)
        header.extend(mask)
        # Masking in place on a bytearray avoids allocating a second copy of the
        # payload, which matters on a board with ~100 KiB of free heap.
        if isinstance(payload, bytearray):
            masked = payload
        else:
            masked = bytearray(payload)
        for index in range(length):
            masked[index] ^= mask[index & 3]
        self.writer.write(bytes(header) + bytes(masked))

    async def send_text(self, text: str) -> None:
        await self.send_bytes(text.encode())

    async def send_bytes(self, data: bytes) -> None:
        if len(data) > self.max_frame:
            raise WebSocketError("frame too large")
        try:
            self._write_frame(OP_TEXT, data)
            await self.writer.drain()
        except OSError as exc:
            self._closed = True
            raise WebSocketClosed("send failed: %s" % (exc,))

    async def send_json(self, message) -> None:
        await self.send_text(json_module.dumps(message))

    # -- receiving -------------------------------------------------------- #
    async def _read_exactly(self, count: int) -> bytes:
        try:
            data = await self.reader.readexactly(count)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - IncompleteRead, reset, EOF
            self._closed = True
            raise WebSocketClosed("read failed: %s" % (exc,))
        if data is None or len(data) != count:
            self._closed = True
            raise WebSocketClosed("short read")
        return data

    @staticmethod
    def _unmask(payload: bytes, mask: bytes) -> bytes:
        """XOR the payload with the 4 byte mask (works on bytes and bytearray)."""
        unmasked = bytearray(payload)
        for index in range(len(unmasked)):
            unmasked[index] ^= mask[index & 3]
        return bytes(unmasked)

    async def receive(self) -> "tuple[int, bytes]":
        """Read one complete message, answering pings transparently."""
        chunks = []
        message_opcode = None
        while True:
            try:
                head = await self._read_exactly(2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                raise
            except Exception as exc:
                self._closed = True
                raise WebSocketClosed("read failed: %s" % (exc,))
            first = head[0]
            second = head[1]
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                raw = await self._read_exactly(2)
                length = (raw[0] << 8) | raw[1]
            elif length == 127:
                raw = await self._read_exactly(8)
                length = 0
                for byte in raw:
                    length = (length << 8) | byte
            if length > self.max_frame:
                self._closed = True
                raise WebSocketError("frame too large: %d" % length)
            mask = await self._read_exactly(4) if masked else None
            payload = await self._read_exactly(length) if length else b""
            if mask:
                payload = self._unmask(payload, mask)

            if opcode == OP_CLOSE:
                self._closed = True
                raise WebSocketClosed("peer closed")
            if opcode == OP_PING:
                try:
                    self._write_frame(OP_PONG, payload)
                    await self.writer.drain()
                except OSError:
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                if message_opcode is None:
                    raise WebSocketError("unexpected continuation frame")
                chunks.append(payload)
            else:
                message_opcode = opcode
                chunks = [payload]
            if fin:
                return message_opcode, b"".join(chunks)

    async def receive_json(self):
        _, payload = await self.receive()
        try:
            return json_module.loads(payload.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebSocketError("invalid JSON from server: %s" % (exc,))

    # -- lifecycle -------------------------------------------------------- #
    async def ping(self, payload: bytes = b"") -> None:
        self._write_frame(OP_PING, payload)
        try:
            await self.writer.drain()
        except OSError:
            self._closed = True

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            payload = bytearray([(code >> 8) & 0xFF, code & 0xFF])
            self._write_frame(OP_CLOSE, bytes(payload))
            await self.writer.drain()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await sleep_ms(0)
        except Exception:  # noqa: BLE001
            pass

    @property
    def closed(self) -> bool:
        return self._closed


def jitter(base: float, spread: float = 0.25) -> float:
    """Randomised delay so a whole house of boards does not sync up.

    Uses the portable helper: MicroPython's ``random`` module often only has
    ``getrandbits`` and ``seed``.
    """
    if base <= 0:
        return 0.0
    return base * (1.0 + random_float() * spread)
