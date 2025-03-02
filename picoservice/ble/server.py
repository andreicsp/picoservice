"""
BLE API server that creates a GATT service with characteristics for each generic API action.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""

import asyncio
import logging
from collections import namedtuple

import aioble
import bluetooth
from micropython import const

from picoservice.ble.sockets import BLEFrameServerSocket
from picoservice.core.router import Router

_logger = logging.getLogger(__name__)

_MTU = const(240)
_ADV_APPEARANCE = const(0x0502)
_ADV_INTERVAL_MS = const(1000)


BLEServerConfig = namedtuple("BLEServerConfig", ["device_name"])


class BLEServer:
    """
    A simple BLE server that advertises a GATT service with characteristics for each API route.
    """
    def __init__(
        self,
        service_uuid,
        config: BLEServerConfig,
        mtu=_MTU,
        wait_for_subscription=True,
    ):
        self._device_name = config.device_name
        self._service = aioble.Service(bluetooth.UUID(service_uuid))
        self._mtu = mtu

        self._handlers_by_characteristic = {}
        self._wait_for_subscription = wait_for_subscription

    def register_services(self, router: Router):
        self._handlers_by_characteristic = {}

        for route in router.routes:
            _logger.info(
                f"Registering route {route.uri} as BLE characteristic {route.uuid}"
            )
            characteristic = aioble.Characteristic(
                self._service,
                bluetooth.UUID(route.uuid),
                write=True,
                read=True,
                # notify=True,
                indicate=True,
                capture=True,
            )
            aioble.Descriptor(
                characteristic, uuid=bluetooth.UUID(0x2902), read=True, write=True
            )
            self._handlers_by_characteristic[characteristic] = router.route_handlers[
                route
            ]

        aioble.register_services(self._service)

        # Set the buffer size for each characteristic
        for characteristic in self._handlers_by_characteristic:
            aioble.core.ble.gatts_set_buffer(characteristic._value_handle, self._mtu)

    def _socket(self, connection, characteristic, handler):
        socket = BLEFrameServerSocket(
            connection,
            characteristic,
            self._mtu,
            wait_for_subscription=self._wait_for_subscription,
        )
        _logger.info("Creating handler task for %s", socket._characteristic.uuid)
        socket.create_task(handler)
        return socket

    async def run(self, router: Router):
        _logger.info("Registering BLE services")
        self.register_services(router)

        while True:
            _logger.info("Advertising Bluetooth %s", self._device_name)
            connection = await aioble.advertise(
                _ADV_INTERVAL_MS,
                name=self._device_name,
                services=[self._service.uuid],
                appearance=_ADV_APPEARANCE,
            )

            _logger.info(f"BLE connection from {connection.device}")

            sockets = [
                self._socket(connection, characteristic, handler)
                for characteristic, handler in self._handlers_by_characteristic.items()
            ]
            await connection.disconnected(timeout_ms=0)
            _logger.info("Disconnected from %s", connection.device)
            await asyncio.gather(*[socket.cancel_task() for socket in sockets])
