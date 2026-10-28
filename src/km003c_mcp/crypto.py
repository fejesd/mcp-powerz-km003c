"""AES-framed KM003C commands: MemoryRead (0x44) and StreamingAuth (0x4C).

Keys and layouts come from the community reverse-engineering work
(okhsunrog/km003c-protocol-research). Everything here is read-only: the
server never writes device memory or settings.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .protocol import CMD_MEMORY_READ, CMD_STREAMING_AUTH, ProtocolError, encode_raw

KEY_MEMORY = b"Lh2yfB7n6X7d9a5Z"
KEY_AUTH_ENC = b"Fa0b4tA25f4R038a"
KEY_AUTH_DEC = b"FX0b4tA25f4R038a"

ADDR_DEVICE_INFO = 0x00000420
ADDR_FIRMWARE_INFO = 0x00004420
ADDR_CALIBRATION = 0x03000C00
ADDR_HARDWARE_ID = 0x40010450
HARDWARE_ID_SIZE = 12

MEMORY_READ_WORD = 0x0101
STREAMING_AUTH_WORD = 0x0200
_MAGIC = 0xFFFFFFFF
_BLOCK = 16


def aes_ecb(data: bytes, key: bytes, decrypt: bool = False) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def build_memory_read_packet(address: int, size: int, tid: int) -> bytes:
    head = struct.pack("<III", address, size, _MAGIC)
    plain = head + struct.pack("<I", binascii.crc32(head)) + b"\xff" * 16
    return encode_raw(CMD_MEMORY_READ, tid, MEMORY_READ_WORD) + aes_ecb(plain, KEY_MEMORY)


def parse_memory_confirmation(body: bytes) -> tuple[int, int]:
    """Decode the 16-byte confirmation that echoes a MemoryRead request."""
    if len(body) < 16:
        raise ProtocolError(f"MemoryRead confirmation needs 16 bytes, got {len(body)}")
    address, size, magic, crc = struct.unpack_from("<IIII", body)
    if magic != _MAGIC or crc != binascii.crc32(body[:12]):
        raise ProtocolError("MemoryRead confirmation failed its magic/CRC check")
    return address, size


def memory_cipher_len(size: int) -> int:
    return -(-size // _BLOCK) * _BLOCK


def decrypt_memory(cipher: bytes, size: int) -> bytes:
    if len(cipher) != memory_cipher_len(size):
        raise ProtocolError(
            f"encrypted memory response must be {memory_cipher_len(size)} bytes, got {len(cipher)}")
    return aes_ecb(cipher, KEY_MEMORY, decrypt=True)[:size]


def build_streaming_auth_packet(hardware_id: bytes, tid: int, timestamp_ms: int) -> bytes:
    if len(hardware_id) != HARDWARE_ID_SIZE:
        raise ValueError(f"hardware id must be {HARDWARE_ID_SIZE} bytes")
    plain = struct.pack("<Q", timestamp_ms) + hardware_id + b"\xff" * 12
    return encode_raw(CMD_STREAMING_AUTH, tid, STREAMING_AUTH_WORD) + aes_ecb(plain, KEY_AUTH_ENC)


def parse_streaming_auth_response(packet: bytes) -> int:
    """Return the granted auth level (0 = failed, AdcQueue disabled)."""
    if len(packet) < 4 or packet[0] & 0x7F != CMD_STREAMING_AUTH:
        raise ProtocolError("not a StreamingAuth response")
    word = struct.unpack_from("<H", packet, 2)[0]
    if not word & 1:
        raise ProtocolError(f"StreamingAuth response without result flag: 0x{word:04x}")
    return (word >> 1) & 0b11


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    hw_version: str
    mfg_date: str
    fw_version: str
    fw_date: str
    serial_id: str
    uuid: str
    hardware_id: str


def _cstr(data: bytes, start: int, end: int) -> str:
    return data[start:end].split(b"\0", 1)[0].decode("utf-8", "replace")


def parse_device_info(dev: bytes, fw: bytes, cal: bytes, hwid: bytes) -> DeviceInfo:
    fw_valid = len(fw) >= 4 and struct.unpack_from("<I", fw)[0] != _MAGIC
    return DeviceInfo(
        model=_cstr(dev, 0x10, 0x1C),
        hw_version=_cstr(dev, 0x1C, 0x28),
        mfg_date=_cstr(dev, 0x28, 0x40),
        fw_version=_cstr(fw, 0x1C, 0x28) if fw_valid else "",
        fw_date=_cstr(fw, 0x28, 0x34) if fw_valid else "",
        serial_id=_cstr(cal, 0x00, 0x07).strip(),
        uuid=_cstr(cal, 0x07, 0x27),
        hardware_id=hwid.hex(),
    )
