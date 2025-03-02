"""
Data structures and utilities for handling HTTP requests and responses.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import asyncio
import hashlib
import json
from binascii import b2a_base64

HTTP_CODES = {
    101: b"Switching Protocols",
    200: b"OK",
    404: b"Not Found",
    500: b"Internal Server Error",
}

CRLF = b"\r\n"
COLON_SPACE = b": "
HTTP_VERSION = b"HTTP/1.1 "


class HTTPError(Exception):
    @property
    def status_code(self):
        return 500

    @property
    def ignore_keep_alive(self):
        return False


class HTTPNotFoundError(HTTPError):
    @property
    def status_code(self):
        return 404


class HTTPBadRequestError(HTTPError):
    @property
    def status_code(self):
        return 400

    @property
    def ignore_keep_alive(self):
        return True


class HTTPInternalServerError(HTTPError):
    @property
    def status_code(self):
        return 500

    @property
    def ignore_keep_alive(self):
        return True


class HTTPParseError(ValueError):
    pass


async def consume_stream(reader, buffer, chunk_size, max_buffer_size):
    chunk = await asyncio.wait_for(reader.read(chunk_size), timeout=5)
    if not len(chunk):
        raise HTTPParseError("Empty request")
    if len(buffer) + len(chunk) > max_buffer_size:
        raise HTTPParseError("Request header too large")
    buffer.extend(chunk)


class HTTPResponse:
    def __init__(self, status_code=200, headers=None, content=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content

    @property
    def is_connection_closing(self):
        return self.headers.get("Connection", "").lower() == "close"

    @is_connection_closing.setter
    def is_connection_closing(self, is_closing):
        if is_closing:
            self.headers["Connection"] = "close"
        elif (
            "Connection" in self.headers
            and self.headers["Connection"].lower() == "close"
        ):
            self.headers.pop("Connection", None)

    @property
    def is_connection_upgraded(self):
        return self.headers.get("Connection", "").lower() == "upgrade"

    async def write_body(self, writer: asyncio.StreamWriter):
        if self.content:
            writer.write(self.content)
            await writer.drain()

    async def write(self, writer):
        writer.write(HTTP_VERSION)
        writer.write(str(self.status_code).encode())
        if self.status_code in HTTP_CODES:
            writer.write(b" ")
            writer.write(HTTP_CODES[self.status_code])
        writer.write(CRLF)

        for key, value in self.headers.items():
            writer.write(key.encode())
            writer.write(COLON_SPACE)
            writer.write(str(value).encode())
            writer.write(CRLF)

        writer.write(CRLF)
        await writer.drain()
        await self.write_body(writer)

    @classmethod
    async def from_stream(
        cls, reader: asyncio.StreamReader, read_buffer_size: int, max_header_size: int
    ) -> "HTTPResponse":
        buffer = bytearray()
        headers = {}
        headers_parsed = False
        status_code = None

        while True:
            try:
                await consume_stream(
                    reader,
                    buffer=buffer,
                    chunk_size=read_buffer_size,
                    max_buffer_size=max_header_size,
                )
                if not status_code:
                    version, status_code, reason, buffer = parse_status_line(buffer)
                if not headers_parsed:
                    headers_parsed, buffer = parse_headers(buffer, headers)
            except HTTPParseError as e:
                raise HTTPBadRequestError(str(e))

            if status_code and headers_parsed:
                return cls(status_code=status_code, headers=headers)

    def __str__(self):
        return f"HTTPResponse(status_code={self.status_code}, headers={self.headers})"


class HTTPJSONResponse(HTTPResponse):
    def __init__(self, status_code=200, headers=None, data=None):
        super().__init__(status_code, headers=headers)
        self.data = json.dumps(data or {}).encode() + CRLF
        self.headers.update(
            {"Content-Type": "application/json", "Content-Length": str(len(self.data))}
        )

    async def write_body(self, writer):
        writer.write(self.data)
        await writer.drain()

    def __str__(self):
        return f"HTTPJSONResponse(status_code={self.status_code}, headers={self.headers}, data={self.data})"


class HTTPUpgradeResponse(HTTPResponse):
    async def handle_upgraded_connection(self, reader, writer):
        raise NotImplementedError()

    @property
    def upgrade_protocol(self):
        return self.headers.get("Upgrade", "").lower()


class WebsocketHandshakeResponse(HTTPUpgradeResponse, HTTPResponse):
    def __init__(self, sec_websocket_key: str, upgrade_handler, headers=None):
        super().__init__(headers=headers, status_code=101)
        self.sec_websocket_key = sec_websocket_key
        self._upgrade_handler = upgrade_handler
        self._get_websocket_headers()

    def _get_websocket_headers(self):
        accept_key = self.sec_websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept_key = b2a_base64(hashlib.sha1(accept_key.encode()).digest())[
            :-1
        ].decode()

        self.headers.update(
            {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Accept": accept_key,
            }
        )

    async def write_body(self, writer):
        pass


class HTTPRequest:
    def __init__(self, method, path, headers, reader=None):
        self.method = method
        self.path = path
        self.headers = headers
        self.reader = reader
        self._content_bytes_read = 0

    def __str__(self):
        return f"HTTPRequest[{self.method} {self.path}]"

    @property
    def is_keep_alive(self):
        return self.headers.get("Connection", "").lower() == "keep-alive"

    @property
    def is_websocket_upgrade(self):
        return self.headers.get("Upgrade", "").lower() == "websocket"

    @property
    def content_length(self):
        return int(self.headers.get("Content-Length", 0))

    def __iter__(self):
        yield f"{self.method} {self.path} HTTP/1.1\r\n".encode()
        for key, value in self.headers.items():
            yield f"{key}: {value}\r\n".encode()

        yield CRLF
        if self.reader:
            yield self.reader

    async def read(self, buffer_size: int = 0):
        if self.reader:
            buffer_size = buffer_size or self.content_length
            if self._content_bytes_read < self.content_length:
                buffer = await self.reader.read(buffer_size)
                self._content_bytes_read += len(buffer)
                return buffer

    async def drain_content(self):
        while self._content_bytes_read < self.content_length:
            await self.read()

    @classmethod
    async def from_stream(
        cls, reader: asyncio.StreamReader, read_buffer_size: int, max_header_size: int
    ) -> "HTTPRequest":
        buffer = bytearray()
        method, path, headers = None, None, {}
        headers_parsed = False

        while True:
            try:
                await consume_stream(
                    reader,
                    buffer=buffer,
                    chunk_size=read_buffer_size,
                    max_buffer_size=max_header_size,
                )
                if not method or not path:
                    method, path, buffer = parse_request_line(buffer)
                if not headers_parsed:
                    headers_parsed, buffer = parse_headers(buffer, headers)
            except HTTPParseError as e:
                raise HTTPBadRequestError(str(e))

            if method and path and headers_parsed:
                return cls(method=method, path=path, headers=headers, reader=reader)


def parse_request_line(buffer: bytearray):
    end_of_line = buffer.find(CRLF)
    if end_of_line == -1:
        return None, None, buffer

    parts = buffer[:end_of_line].split(b" ")
    if len(parts) != 3:
        raise HTTPParseError("Invalid request line")

    method, path, _ = parts[0].decode(), parts[1].decode(), parts[2].decode()
    return method, path, buffer[end_of_line + 2 :]


def parse_body(buffer: bytes, cursor: int, headers: dict):
    if "Content-Length" in headers:
        content_length = int(headers["Content-Length"])
        if cursor + content_length > len(buffer):
            return None
        else:
            return buffer[cursor : cursor + content_length]
    else:
        return buffer[cursor:]


def parse_status_line(buffer: bytearray):
    end_of_line = buffer.find(CRLF)
    if end_of_line == -1:
        return None, None, None, buffer

    parts = buffer[:end_of_line].split(b" ", 2)
    if len(parts) != 3:
        raise HTTPParseError("Invalid response line")

    version, status_code, reason = parts[0].decode(), int(parts[1]), parts[2].decode()
    return version, status_code, reason, buffer[end_of_line + 2 :]


def parse_headers(buffer: bytearray, headers: dict):
    cursor = 0
    done = False
    while cursor < len(buffer):
        end_of_line = buffer.find(CRLF, cursor)
        if end_of_line == -1:
            break
        elif end_of_line == cursor:
            cursor = end_of_line + 2
            done = True
            break
        else:
            line = buffer[cursor:end_of_line].decode()
            cursor = end_of_line + 2
            key, value = line.split(": ", 1)
            headers[key] = value

    return done, buffer[cursor:]
