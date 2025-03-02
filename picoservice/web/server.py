"""
HTTP server implementation that listens for incoming connections and handles requests
to the registered routes.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import asyncio
import logging
from collections import namedtuple

from picoservice.core.handler import SocketHandler
from picoservice.core.router import ErrorHandler, Router
from picoservice.web.http import (
    HTTPError,
    HTTPInternalServerError,
    HTTPJSONResponse,
    HTTPNotFoundError,
    HTTPRequest,
    HTTPUpgradeResponse,
)
from picoservice.web.sockets import WebsocketHandler

_logger = logging.getLogger(__name__)

HTTPServerConfig = namedtuple("HTTPServerConfig", ["host", "port"])
HandlerResult = namedtuple("HandlerResult", ["request", "response", "handler"])


class HTTPErrorHandler(ErrorHandler):
    async def __call__(self, request: HTTPRequest, exception: HTTPError):
        """
        Default exception handler that returns a JSON response with the exception message.
        """
        return HTTPJSONResponse(
            status_code=exception.status_code,
            data={
                "error": exception.status_code,
                "path": request.path if request else "",
                "method": request.method if request else "",
            },
        )


class HTTPServer:
    """
    A simple HTTP server that listens for incoming connections and handles requests.
    """

    def __init__(
        self,
        host: str,
        port: int,
        read_buffer_size: int = 256,
        read_timeout: int = 5,
        backlog: int = 5,
        max_incoming_header_size: int = 4096,
    ):
        self.host = host
        self.port = port
        self._server = None
        self._router = Router({})
        self._read_buffer_size = read_buffer_size
        self._max_incoming_header_size = max_incoming_header_size
        self._read_timeout = read_timeout
        self._backlog = backlog
        self._error_response = HTTPJSONResponse(
            status_code=500, data={"error": "Internal server error"}
        )

    @property
    def router(self):
        return self._router

    @router.setter
    def router(self, router: Router):
        socket_handlers = {}
        for route, handler in router.route_handlers.items():
            if isinstance(handler, SocketHandler) and not isinstance(
                handler, WebsocketHandler
            ):
                socket_handlers[route] = WebsocketHandler(fn=handler)
            else:
                socket_handlers[route] = handler

        error_handlers = [HTTPErrorHandler(fn=None, exception_type=None)]
        self._router = Router(
            route_handlers=socket_handlers, error_handlers=error_handlers
        )

    async def _handle_request(self, reader, writer) -> HandlerResult:
        request = None
        keep_alive = False

        try:
            # Use pre-allocated buffer for request reading
            request = await HTTPRequest.from_stream(
                reader,
                read_buffer_size=self._read_buffer_size,
                max_header_size=self._max_incoming_header_size,
            )
            _logger.info(f"Request: {request.method} {request.path}")
            keep_alive = request.is_keep_alive

            handler = self._router.get_handler(path=request.path, method=request.method)
            if not handler:
                raise HTTPNotFoundError(request.path)

            response = await handler(request)
            if not response.is_connection_upgraded:
                response.is_connection_closing = not keep_alive

        except Exception as e:
            if not isinstance(e, HTTPError):
                e = HTTPInternalServerError(request.path if request else "")

            handler = self._router.get_exception_handler(e)
            response = await handler(request, e) if handler else self._error_response
            response.is_connection_closing = True

        if request:
            await request.drain_content()
        await response.write(writer)
        return HandlerResult(request, response, handler)

    async def _handle_connection(self, reader, writer):
        info = writer.get_extra_info("peername")
        addr, port = info[0], info[1]
        _logger.info(f"New connection from {addr}:{port}")

        while True:
            result = await self._handle_request(reader, writer)
            if (
                result.response.is_connection_closing
                or result.response.is_connection_upgraded
            ):
                break

        if isinstance(result.response, HTTPUpgradeResponse) and isinstance(
            result.handler, WebsocketHandler
        ):
            _logger.info(f"Upgrading connection: {result.response.upgrade_protocol}")
            try:
                await result.handler.handle_upgraded_connection(
                    reader, writer, request=result.request, response=result.response
                )
            except Exception as e:
                _logger.error(f"Error while handling upgraded connection: {e}")
                _logger.exception(e)

            _logger.info("Closing websocket connection")

        try:
            await writer.drain()
        except Exception as e:
            _logger.info(f"Connection lost: {e}")
        else:
            await writer.wait_closed()
            _logger.info(f"Connection with {addr}:{port} closed")

    async def run(self, router: Router):
        self.router = router
        self.router.log_routes()

        _logger.info("Starting HTTP server")
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port, backlog=self._backlog
        )
        _logger.info(
            "Server listening on port %d with backlog %d", self.port, self._backlog
        )
        await self._server.wait_closed()
