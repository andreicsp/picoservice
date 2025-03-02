"""
Structure to manage API routes and their handlers.
Specific server implementations use the router to manage the API routes and their handlers.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import logging
from collections import namedtuple

from picoservice.core.handler import Handler

_logger = logging.getLogger(__name__)

Route = namedtuple("Route", ["uri", "method", "uuid"])


class ErrorHandler(namedtuple("_ErrorHandler", ["fn", "exception_type"])):
    def is_matching(self, exception):
        return self.exception_type is None or isinstance(exception, self.exception_type)

    async def __call__(self, *args, **kwargs):
        return await self.fn(*args, **kwargs)


class Router:
    def __init__(self, route_handlers: dict, error_handlers=None):
        self.route_handlers = route_handlers
        self._exception_mappers = tuple(error_handlers) if error_handlers else ()

    def get_route(self, path, method):
        for route in self.route_handlers:
            if route.uri == path and route.method == method:
                return route

    @property
    def routes(self):
        return list(self.route_handlers.keys())

    def get_handler(self, path, method) -> Handler:  # type: ignore
        """
        Return the handler function for the given path and method.
        """
        if route := self.get_route(path, method):
            return self.route_handlers[route]

    def log_routes(self, tabs=0):
        for route, handler in self.route_handlers.items():
            ident = "\t" * tabs
            _logger.info(f"{ident}Route: {route.method} {route.uri} -> {handler.fn}")

    def get_exception_handler(self, exception):
        """
        Return the handler function for the given exception.
        """
        for handler in self._exception_mappers:
            if handler.is_matching(exception):
                return handler

        raise exception
