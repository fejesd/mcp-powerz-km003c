import binascii
import struct

import pytest

from km003c_mcp.crypto import (
    ADDR_HARDWARE_ID, KEY_AUTH_ENC, KEY_MEMORY, aes_ecb, build_memory_read_packet,
    build_streaming_auth_packet, decrypt_memory, memory_cipher_len, parse_device_info,
    parse_memory_confirmation, parse_streaming_auth_response,
)
from km003c_mcp.protocol import ProtocolError

# Reference packets captured by okhsunrog/km003c-rs from a working session.
MEMREAD = bytes.fromhex("4402010133f8860c0054288cdc7e52729826872dd18b539a39c407d5c063d91102e36a9e")
AUTH = bytes.fromhex("4c0600025538815b69a452c83e54ef1d70f3bc9ae6aac1b12a6ac07c20fde58c7bf517ca")
HWID = bytes.fromhex("3037314b42500dff110affff")


def test_reference_memory_read_decrypts():
    plain = aes_ecb(MEMREAD[4:], KEY_MEMORY, decrypt=True)
    assert struct.unpack_from("<III", plain) == (0x420, 64, 0xFFFFFFFF)
    assert struct.unpack_from("<I", plain, 12)[0] == binascii.crc32(plain[:12])


def test_build_memory_read_roundtrip():
    pkt = build_memory_read_packet(ADDR_HARDWARE_ID, 12, 2)
    assert pkt[:4] == bytes.fromhex("44020101") and len(pkt) == 36
    plain = aes_ecb(pkt[4:], KEY_MEMORY, decrypt=True)
    assert parse_memory_confirmation(plain[:16]) == (ADDR_HARDWARE_ID, 12)
    assert plain[16:] == b"\xff" * 16


def test_memory_confirmation_bad_crc():
    body = struct.pack("<IIII", 0x420, 64, 0xFFFFFFFF, 0)
    with pytest.raises(ProtocolError):
        parse_memory_confirmation(body)


def test_reference_streaming_auth_contains_hwid():
    assert aes_ecb(AUTH[4:], KEY_AUTH_ENC, decrypt=True)[8:20] == HWID


def test_build_streaming_auth():
    pkt = build_streaming_auth_packet(HWID, 6, 1_700_000_000_000)
    assert pkt[:4] == AUTH[:4] and len(pkt) == 36
    plain = aes_ecb(pkt[4:], KEY_AUTH_ENC, decrypt=True)
    assert plain[8:20] == HWID
    assert struct.unpack_from("<Q", plain)[0] == 1_700_000_000_000


def test_parse_streaming_auth_levels():
    assert parse_streaming_auth_response(bytes.fromhex("4c000302") + bytes(32)) == 1
    assert parse_streaming_auth_response(bytes.fromhex("4c000102") + bytes(32)) == 0
    with pytest.raises(ProtocolError):
        parse_streaming_auth_response(bytes.fromhex("4c000002") + bytes(32))


def test_decrypt_memory_trims_and_checks_len():
    cipher = aes_ecb(bytes(range(16)), KEY_MEMORY)
    assert memory_cipher_len(12) == 16 and memory_cipher_len(17) == 32
    assert decrypt_memory(cipher, 12) == bytes(range(12))
    with pytest.raises(ProtocolError):
        decrypt_memory(cipher[:8], 12)


def _block(fields: dict[int, bytes]) -> bytes:
    data = bytearray(64)
    for offset, value in fields.items():
        data[offset:offset + len(value)] = value
    return bytes(data)


def test_parse_device_info():
    dev = _block({0x10: b"KM003C\0", 0x1C: b"V1.1\0", 0x28: b"2023.6.1\0"})
    fw = _block({0: struct.pack("<I", 0x4000), 0x1C: b"1.9.9\0", 0x28: b"2025.9.22\0"})
    cal = _block({0: b"007965 ", 7: b"A" * 32})
    info = parse_device_info(dev, fw, cal, HWID)
    assert (info.model, info.hw_version, info.mfg_date) == ("KM003C", "V1.1", "2023.6.1")
    assert (info.fw_version, info.fw_date) == ("1.9.9", "2025.9.22")
    assert (info.serial_id, info.uuid, info.hardware_id) == ("007965", "A" * 32, HWID.hex())


def test_parse_device_info_invalid_firmware_block():
    fw = _block({0: b"\xff\xff\xff\xff", 0x1C: b"1.9.9\0"})
    assert parse_device_info(bytes(64), fw, bytes(64), HWID).fw_version == ""
