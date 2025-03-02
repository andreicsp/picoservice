"""
A Socket-like interface for interacting with BLE characteristics.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import asyncio
import logging

import aioble
import bluetooth

from picoservice.core.sockets import (
    ListStreamReader,
    ServiceSocket,
    SocketError,
    WSFrame,
)

_logger = logging.getLogger(__name__)

_indicate_lock = asyncio.Lock()


class BLESocketClosedError(SocketError):
    pass


class BLEServerSocket(ServiceSocket):
    """
    A Socket-like interface for interacting with BLE characteristics.
    Each characteristic is a separate socket and represents a connection to a single endpoint.
    """
    def __init__(
        self,
        connection,
        characteristic: aioble.Characteristic,
        mtu: int,
        indicate_timeout_ms=4000,
        wait_for_subscription=True,
    ):
        self._connection = connection
        self._characteristic = characteristic

        try:
            self._cccd = next(
                desc
                for desc in characteristic.descriptors
                if desc.uuid == bluetooth.UUID(0x2902)
            )
        except StopIteration:
            raise ValueError(
                "Characteristic does not have a CCCD descriptor to store subscription status"
            )

        self._mtu = mtu
        self._indicate_timeout_ms = indicate_timeout_ms
        self._task = None
        self._wait_for_subscription = wait_for_subscription

    async def cancel_task(self):
        if self._task:
            self._task.cancel()
        await self.unsubscribed()

    def create_task(self, handler):
        self._task = asyncio.create_task(handler(self))
        return self._task

    @property
    def is_connected(self):
        return self._connection.is_connected

    @property
    def max_payload_size(self):
        return self._mtu - 3

    async def _read(self, source, wait_written=False, reset_written=False):
        try:
            with self._connection.timeout(None):
                conn, data = await source.written()
                # if reset_written:
                #    source.write(b"")
                return data
        except aioble.DeviceDisconnectedError:
            raise BLESocketClosedError(self._characteristic.uuid)

    async def subscribed(self):
        while self._wait_for_subscription:
            cccd_value = await self._read(
                self._cccd, wait_written=False, reset_written=False
            )
            if cccd_value and cccd_value[0] == 1:
                break
            await asyncio.sleep(0.5)

    async def unsubscribed(self):
        self._cccd.write(b"")
        _logger.debug(f"Unsubscribed from {self._characteristic.uuid}")

    async def recv(self):
        return await self._read(
            self._characteristic, wait_written=True, reset_written=True
        )

    async def send(self, data, wait_subscribed=True):
        # Split data into chunks of MTU size
        if isinstance(data, str):
            data = data.encode()

        if len(data) > self.max_payload_size:
            raise ValueError(
                f"Data exceeds maximum payload size: {self.max_payload_size}"
            )

        if wait_subscribed:
            await self.subscribed()

        try:
            # Aquire the lock to prevent multiple indications
            async with _indicate_lock:
                await self._characteristic.indicate(
                    self._connection, data, timeout_ms=self._indicate_timeout_ms
                )
        except (aioble.DeviceDisconnectedError, ValueError):
            raise BLESocketClosedError(self._characteristic.uuid)


class BLEFrameServerSocket(BLEServerSocket):
    async def recv(self):
        payload = bytes()
        while True:
            buffer = await super().recv()
            frame = await WSFrame.from_stream(ListStreamReader(buffer))
            if frame.is_continuation and not payload:
                _logger.error("Received continuation frame without initial frame")
                continue

            _logger.debug(f"Received fragment of length {len(frame.raw_payload)}")
            payload += frame.raw_payload
            if frame.is_final:
                break

        _logger.info(f"Received frame of length {len(payload)}: {payload}")
        return payload

    async def send(self, data):
        await self.subscribed()

        chunks = WSFrame.create_fragments(data, self.max_payload_size)
        for chunk in chunks:
            await super().send(chunk.to_bytes(), wait_subscribed=False)
