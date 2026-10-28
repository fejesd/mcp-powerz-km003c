"""Pure (I/O-free) encoding and parsing of the KM003C vendor bulk protocol.

All multi-byte values are little-endian. Field layouts follow the community
reverse-engineering work (okhsunrog/km003c-protocol-research) and were checked
against a live device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CMD_CONNECT = 0x02
CMD_DISCONNECT = 0x03
CMD_ACCEPT = 0x05
CMD_REJECT = 0x06
CMD_GET_DATA = 0x0C
CMD_START_GRAPH = 0x0E
CMD_STOP_GRAPH = 0x0F
CMD_NOT_READABLE = 0x27
CMD_PUT_DATA = 0x41
CMD_MEMORY_READ = 0x44
CMD_STREAMING_AUTH = 0x4C

ATT_ADC = 0x0001
ATT_ADCQUEUE = 0x0002
ATT_SETTINGS = 0x0008
ATT_PD = 0x0010

GRAPH_RATES_SPS = {0: 2, 1: 10, 2: 50, 3: 1000}

ADC_PAYLOAD_SIZE = 44
TEMP_INVALID_RAW = -32768


class ProtocolError(Exception):
    """Raised when a packet cannot be decoded."""


def encode_ctrl(cmd: int, tid: int, attribute: int = 0) -> bytes:
    """Encode a control header; the 15-bit attribute starts at bit 17."""
    word = (cmd & 0x7F) | ((tid & 0xFF) << 8) | ((attribute & 0x7FFF) << 17)
    return struct.pack("<I", word)


def encode_raw(cmd: int, tid: int, word: int) -> bytes:
    """Encode a header whose upper 16 bits are a raw word (MemoryRead, StreamingAuth)."""
    return bytes([cmd & 0xFF, tid & 0xFF]) + (word & 0xFFFF).to_bytes(2, "little")


@dataclass(frozen=True)
class Header:
    cmd: int
    flag: int
    tid: int
    attribute: int
    obj_count: int
    raw_word: int


def decode_header(data: bytes) -> Header:
    if len(data) < 4:
        raise ProtocolError(f"packet too short for a header: {len(data)} bytes")
    word = struct.unpack_from("<I", data)[0]
    return Header(
        cmd=word & 0x7F,
        flag=(word >> 7) & 1,
        tid=(word >> 8) & 0xFF,
        attribute=(word >> 17) & 0x7FFF,
        obj_count=word >> 22,
        raw_word=struct.unpack_from("<H", data, 2)[0],
    )


@dataclass(frozen=True)
class LogicalPacket:
    attribute: int
    next: bool
    chunk: int
    payload: bytes


def parse_putdata(data: bytes) -> tuple[Header, list[LogicalPacket]]:
    """Split a PutData response into its chained logical packets."""
    header = decode_header(data)
    packets: list[LogicalPacket] = []
    offset = 4
    while offset < len(data):
        if len(data) - offset < 4:
            raise ProtocolError(f"truncated extended header at offset {offset}")
        word = struct.unpack_from("<I", data, offset)[0]
        size = word >> 22
        offset += 4
        if len(data) - offset < size:
            raise ProtocolError(
                f"logical packet needs {size} bytes at offset {offset}, got {len(data) - offset}")
        packet = LogicalPacket(
            attribute=word & 0x7FFF,
            next=bool((word >> 15) & 1),
            chunk=(word >> 16) & 0x3F,
            payload=bytes(data[offset:offset + size]),
        )
        packets.append(packet)
        offset += size
        if not packet.next:
            break
    return header, packets


@dataclass(frozen=True)
class AdcReading:
    vbus_v: float
    ibus_a: float
    power_w: float
    vbus_avg_v: float
    ibus_avg_a: float
    vbus_ori_avg_v: float
    ibus_ori_avg_a: float
    temp_c: float | None
    cc1_v: float
    cc2_v: float
    dp_v: float
    dm_v: float
    vdd_v: float
    rate_index: int
    flags: int
    cc2_avg_v: float
    dp_avg_v: float
    dm_avg_v: float


def parse_adc(payload: bytes) -> AdcReading:
    if len(payload) < ADC_PAYLOAD_SIZE:
        raise ProtocolError(f"ADC payload must be {ADC_PAYLOAD_SIZE} bytes, got {len(payload)}")
    (vbus, ibus, vbus_avg, ibus_avg, vbus_ori, ibus_ori, temp,
     cc1, cc2, dp, dm, vdd, rate, flags, cc2_avg, dp_avg, dm_avg) = struct.unpack_from(
        "<iiiiiihHHHHHBBHHH", payload)
    vbus_v = vbus / 1e6
    ibus_a = ibus / 1e6
    return AdcReading(
        vbus_v=vbus_v,
        ibus_a=ibus_a,
        power_w=vbus_v * ibus_a,
        vbus_avg_v=vbus_avg / 1e6,
        ibus_avg_a=ibus_avg / 1e6,
        vbus_ori_avg_v=vbus_ori / 1e6,
        ibus_ori_avg_a=ibus_ori / 1e6,
        temp_c=None if temp == TEMP_INVALID_RAW else temp / 128,
        cc1_v=cc1 / 1e4,
        cc2_v=cc2 / 1e4,
        dp_v=dp / 1e4,
        dm_v=dm / 1e4,
        vdd_v=vdd / 1e4,
        rate_index=rate,
        flags=flags,
        cc2_avg_v=cc2_avg / 1e3,
        dp_avg_v=dp_avg / 1e3,
        dm_avg_v=dm_avg / 1e3,
    )
