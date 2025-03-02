"""
Base handler classes for the service endpoints.
HTTP or BLE handlers use inheritance to define their specific endpoint handler classes

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import logging

from picoservice.core.sockets import ServiceSocket, SocketError

_logger = logging.getLogger(__name__)


class Handler:
    def __init__(self, fn):
        self.fn = fn

    async def __call__(self, *args, **kwargs):
        return await self.fn(*args, **kwargs)


class SocketHandler(Handler):
    """
    Base class for socket handlers that handle interactions with a single endpoint.
    """
    async def __call__(self, socket: ServiceSocket):
        try:
            return await self.fn(socket=socket)
        except SocketError as e:
            _logger.exception(f"Client socket error {e.__class__.__name__}: {e}")
        except Exception as e:
            _logger.exception(
                f"Error in socket handler of type {e.__class__.__name__}: {e}"
            )
            raise e
