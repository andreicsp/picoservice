"""
Definitions for a WebSocket frame and the socket interfaces and exceptions to handle
reading and writing frames from a socket-like object.

(c) 2025 Andrei Dumitrache
Licensed under the MIT License.
See the LICENSE file in the project root for more information.

Author: Andrei Dumitrache
"""
import io
import struct

OP_CONT = 0
OP_TEXT = 1
OP_BINARY = 2
OP_CLOSE = 8
OP_PING = 9
OP_PONG = 10


class SocketError(Exception):
    pass


class ListStreamReader(io.BytesIO):
    async def read(self, num_bytes):
        return super().read(num_bytes)


class WSFrame:
    def __init__(self, opbyte, payload):
        self.opbyte = opbyte
        self._payload = payload

    def _length_bytes(self):
        length = len(self._payload)
        if length <= 125:
            return bytes([length])
        elif length <= 65535:
            return bytes([126]) + struct.pack("!H", length)
        else:
            return bytes([127]) + struct.pack("!Q", length)

    def to_bytes(self):
        return bytes([self.opbyte]) + self._length_bytes() + self._payload

    @property
    def payload(self):
        if self.opcode == OP_TEXT and isinstance(self._payload, bytes):
            return self._payload.decode()
        return self._payload

    @property
    def raw_payload(self):
        return self._payload

    @property
    def is_final(self):
        return bool(self.opbyte & 0x80)

    @property
    def is_continuation(self):
        return bool(self.opbyte & 0x00)

    @property
    def is_ping(self):
        return self.opcode == OP_PING

    @property
    def is_pong(self):
        return self.opcode == OP_PONG

    @property
    def is_close(self):
        return self.opcode == OP_CLOSE

    @property
    def opcode(self):
        return self.opbyte & 0x0F

    @classmethod
    async def decode_variable_length_with_mask_flag(cls, stream_reader):
        length = (await stream_reader.read(1))[0]

        has_mask = bool(length & 0x80)
        length = length & 0x7F

        if length <= 125:
            return length, has_mask
        elif length == 126:
            length = await stream_reader.read(2)
            return struct.unpack("!H", length)[0], has_mask
        elif length == 127:
            length = await stream_reader.read(8)
            return struct.unpack("!Q", length)[0], has_mask
        else:
            raise ValueError(f"Invalid length byte: {length}")

    @classmethod
    async def from_stream(cls, stream_reader, max_payload_length=65535):
        opbyte = await stream_reader.read(1)
        if not opbyte:
            raise SocketError("Connection closed")
        opbyte = opbyte[0]
        length, has_mask = await cls.decode_variable_length_with_mask_flag(
            stream_reader
        )
        if length > max_payload_length:
            raise ValueError("Message too large")

        if has_mask:
            mask = await stream_reader.read(4)
        else:
            mask = None

        payload = await stream_reader.read(length)
        if mask:
            payload = bytes([payload[i] ^ mask[i % 4] for i in range(len(payload))])

        return cls(opbyte, payload)

    @classmethod
    def from_payload(cls, payload, is_final=True, is_continuation=False):
        opbyte = OP_BINARY if isinstance(payload, (bytes, bytearray)) else OP_TEXT
        if is_final:
            opbyte |= 0x80
        if is_continuation:
            opbyte |= 0x00
        return cls(opbyte, payload)

    @classmethod
    def create_fragments(cls, payload, mtu):
        # Compute size of length block based on maximum total frame size
        fragments = []
        while payload:
            max_payload_size = min(len(payload), mtu - 2)  # opcode + length
            if max_payload_size > 125:
                max_payload_size -= 2
            if max_payload_size > 65535:
                max_payload_size -= 6

            fragment_payload = payload[:max_payload_size]
            payload = payload[max_payload_size:]
            fragments.append(
                cls.from_payload(
                    fragment_payload,
                    is_final=len(payload) == 0,
                    is_continuation=bool(fragments),
                )
            )

        return fragments

    @classmethod
    def create_ping(cls, payload=b""):
        return cls(OP_PING | 0x80, payload)

    @classmethod
    def create_pong(cls, payload=b""):
        return cls(OP_PONG | 0x80, payload)


class ServiceSocket:
    async def send(self, data):
        raise NotImplementedError()

    async def recv(self):
        raise NotImplementedError()

    @property
    def is_connected(self):
        return True
