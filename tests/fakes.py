"""Shared fixtures and fake transports for the KM003C tests."""

# GetData(ADC) response captured live from a KM003C (SN 014270) with no load attached.
LIVE_ADC = bytes.fromhex(
    "41028002" "0100000b"
    "00080000 65feffff 3b0d0000 f5ffffff 9f0d0000 ddffffff"
    "0080 0c03 6e01 4803 6e03 f87e 00 80 2300 5300 5600".replace(" ", ""))


def live_adc_with_tid(tid: int) -> bytes:
    data = bytearray(LIVE_ADC)
    data[1] = tid
    return bytes(data)


class FakeTransport:
    """Scripted UsbTransport: each read() pops the next scripted reply."""

    def __init__(self, script, serial_number="TEST"):
        self.script = list(script)
        self.writes: list[bytes] = []
        self.serial_number = serial_number
        self.closed = False

    def write(self, data: bytes, timeout_ms: int) -> None:
        self.writes.append(bytes(data))

    def read(self, size: int, timeout_ms: int) -> bytes:
        from km003c_mcp.usb_device import DeviceTimeoutError
        if not self.script:
            raise DeviceTimeoutError("fake transport: no scripted reply")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def memory_read_replies(tid: int, address: int, plain: bytes, chunk: int = 16) -> list[bytes]:
    """Confirmation packet plus raw AES chunks for a MemoryRead of len(plain) bytes."""
    import binascii
    import struct

    from km003c_mcp.crypto import KEY_MEMORY, aes_ecb, memory_cipher_len

    head = struct.pack("<III", address, len(plain), 0xFFFFFFFF)
    confirmation = bytes([0xC4, tid, 0x01, 0x01]) + head + struct.pack("<I", binascii.crc32(head))
    padded = plain + b"\0" * (memory_cipher_len(len(plain)) - len(plain))
    cipher = aes_ecb(padded, KEY_MEMORY)
    return [confirmation] + [cipher[i:i + chunk] for i in range(0, len(cipher), chunk)]


class FakeSerial:
    """pyserial stand-in: replies are queued when the matching command line is written."""

    def __init__(self, replies: dict[str, list[bytes]]):
        self.replies = replies
        self.written: list[bytes] = []
        self.queue: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.queue.clear()

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        self.queue.extend(self.replies.get(data.decode().strip(), []))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        return self.queue.pop(0) if self.queue else b""

    def close(self) -> None:
        self.closed = True
