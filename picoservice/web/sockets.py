"""
Implementation of a WebSocket server and client.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import asyncio
import binascii
import logging
import random
import time

from picoservice.core.handler import SocketHandler
from picoservice.core.sockets import ServiceSocket, SocketError, WSFrame
from picoservice.web.http import HTTPRequest, HTTPResponse, WebsocketHandshakeResponse

_logger = logging.getLogger(__name__)


class WebsocketHandler(SocketHandler):
    async def additional_headers(self, request: HTTPRequest):
        return {}

    async def handshake_handler(self, request: HTTPRequest):
        sec_websocket_key = request.headers.get("Sec-WebSocket-Key")
        return WebsocketHandshakeResponse(
            sec_websocket_key=sec_websocket_key,
            upgrade_handler=self.fn,
            headers=await self.additional_headers(request),
        )

    async def handle_upgraded_connection(
        self, reader, writer, request: HTTPRequest, response: HTTPResponse
    ):
        websocket = WebSocket(reader, writer, request=request, response=response)
        await self.fn(websocket)

    async def __call__(self, request):
        return await self.handshake_handler(request=request)


class WebSocketError(SocketError):
    pass


class WebSocket(ServiceSocket):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: HTTPRequest,
        response: HTTPResponse,
        ping_interval: int = 15,
        ping_timeout: int = 30,
    ):
        self._reader = reader
        self._writer = writer
        try:
            info = writer.get_extra_info("peername")
        except KeyError:
            info = ("", 0)
        self._remote_host, self._remote_port = info[0], info[1]

        self._max_payload_length = 2048
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._pong_received_at = time.time()
        self._last_activity_at = time.time()
        self._ping_sent_at = 0
        self._is_connected = True
        self._ping_task = asyncio.create_task(self.ping_loop())
        self.response = response
        self.request = request
        self._receive_event = asyncio.Event()
        self._last_message = None
        self._receive_task = asyncio.create_task(self._receive_loop())

    def _log_frame(self, frame, received):
        if frame.is_close:
            op = "CLOSE"
        elif frame.is_ping:
            op = "PING"
        elif frame.is_pong:
            op = "PONG"
        else:
            op = "MSG"

        _logger.debug(
            f"[{self._remote_host}:{self._remote_port}] {'<' if received else '>'} {op}: {frame.payload}"
        )

    async def _process_frame(self, frame):
        self._last_activity_at = time.time()
        if frame.is_close:
            self._is_connected = False
            raise WebSocketError("Connection closed by client")
        elif frame.is_ping:
            await self._send(WSFrame.create_pong(frame.payload))
            return None
        elif frame.is_pong:
            self._pong_received_at = time.time()
            return None
        return frame

    async def _read_frame(self):
        try:
            frame = await WSFrame.from_stream(self._reader)
            self._log_frame(frame, received=True)
            return frame
        except OSError as e:
            raise SocketError(e)

    @property
    def is_connected(self):
        return self._is_connected

    async def _send(self, frame: WSFrame):
        """Send a message to the client.
        :param frame: the frame to send.
        """
        if not self.is_connected:
            raise WebSocketError("Connection closed")

        try:
            msg = frame.to_bytes()
            self._writer.write(msg)
            self._log_frame(frame, received=False)
        except Exception as e:
            raise WebSocketError(f"Connection closed by client: {e}")
        try:
            await asyncio.wait_for(self._writer.drain(), timeout=self._ping_timeout)
        except asyncio.TimeoutError:
            raise WebSocketError("Timeout sending data")

    async def send(self, data):
        """Send a message to the client.

        :param data: the data to send, given as a string or bytes.
        """
        return await self._send(WSFrame.from_payload(data))

    async def ping(self):
        """Send a PING message to the client."""
        await self._send(WSFrame.create_ping(payload=b"123"))
        self._ping_sent_at = time.time()

    async def ping_loop(self):
        """
        Start a loop that sends PING messages to the client at regular intervals.
        It also checks for PONG messages from the client and closes the connection if no PONG is received
        within the timeout period since the last PING.
        """
        while self.is_connected:
            if time.time() - self._ping_sent_at >= self._ping_interval:
                try:
                    await self.ping()
                except WebSocketError as e:
                    _logger.error(
                        f"Terminating ping loop to {self._remote_host}:{self._remote_port}: {e}"
                    )
                    break
            if time.time() - self._last_activity_at >= self._ping_timeout:
                self._is_connected = False
                _logger.info(
                    f"Closing connection due to timeout {self._remote_host}:{self._remote_port}"
                )
                break

            await asyncio.sleep(1)

    def _set_last_message(self, message, is_connected=True):
        self._last_message = message
        self._is_connected = is_connected
        self._receive_event.set()

    async def recv(self):
        """Receive a message from the client.

        :return: the received message, given as a string or bytes.
        """
        await self._receive_event.wait()
        self._receive_event.clear()
        return self._last_message

    async def _receive_loop(self):
        while self.is_connected:
            try:
                frame = await asyncio.wait_for(
                    self._read_frame(), timeout=self._ping_timeout
                )
                if not frame:
                    self._set_last_message(None, is_connected=False)
                    break
                else:
                    frame = await self._process_frame(frame)
                    if frame:
                        self._set_last_message(frame.payload)
            except SocketError as e:
                self._set_last_message(None, is_connected=False)
                _logger.debug(f"{e}: {self._remote_host}:{self._remote_port}")
                break
            except asyncio.TimeoutError:
                self._is_connected = False
                _logger.info(
                    f"Closing connection due to timeout {self._remote_host}:{self._remote_port}"
                )
                break

        _logger.debug(
            f"Receive loop terminated for {self._remote_host}:{self._remote_port}"
        )


async def connect(
    address: str,
    ping_interval: int = 15,
    ping_timeout: int = 30,
    additional_headers: dict = {},
    read_buffer_size: int = 4096,
    max_header_size: int = 8192,
) -> WebSocket:
    """Connect to a WebSocket server.

    :param address: ws://host:port/path
    :return: a WebSocket instance.
    """
    if not address.startswith("ws://"):
        raise ValueError("address must start with ws://")
    address = address[5:]
    parts = address.split("/", 1)
    host, port = parts[0].split(":", 1)
    path = parts[1] if len(parts) > 1 else ""
    path = f"/{path}"

    # Sec-WebSocket-Key is 16 bytes of random base64 encoded
    key = binascii.b2a_base64(bytes(random.getrandbits(8) for _ in range(16)))[:-1]

    headers = {}
    headers.update(additional_headers)
    headers.update(
        {
            "Host": f"{host}:{port}",
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "Origin": f"http://{host}:{port}",
        }
    )

    request = HTTPRequest(method="GET", path=path, headers=headers)
    reader, writer = await asyncio.open_connection(host, int(port))

    for line in request:
        writer.write(line)
    await writer.drain()

    response = await HTTPResponse.from_stream(
        reader, read_buffer_size=read_buffer_size, max_header_size=max_header_size
    )
    assert response.status_code == 101, f"Handshake failed: {response.status_code}"
    websocket = WebSocket(
        reader,
        writer,
        request=request,
        response=response,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    )
    return websocket
