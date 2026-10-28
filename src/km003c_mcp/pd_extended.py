"""USB PD extended messages: extended header, chunk payloads and data blocks (USB PD R3.1)."""

from __future__ import annotations

import struct

from .pd_objects import bits, decode_pdo, r3

MAX_CHUNK_BYTES = 26


def decode_extended_header(word: int) -> dict:
    return {"chunked": bool(word >> 15 & 1), "chunk_number": bits(word, 14, 11),
            "request_chunk": bool(word >> 10 & 1), "data_size": bits(word, 8, 0)}


def chunk_data(wire: bytes, ext: dict) -> bytes:
    """Return the data bytes carried by this (possibly chunked) extended message."""
    if ext["request_chunk"]:
        return b""
    size = ext["data_size"]
    if ext["chunked"]:
        size = max(0, min(MAX_CHUNK_BYTES, size - MAX_CHUNK_BYTES * ext["chunk_number"]))
    return wire[4:4 + size]


def is_complete_in_one_chunk(ext: dict) -> bool:
    return not ext["chunked"] or ext["data_size"] <= MAX_CHUNK_BYTES


def _named_bits(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value >> bit & 1]


def _id(data: bytes) -> dict:
    vid, pid = struct.unpack_from("<HH", data)
    return {"vid": f"0x{vid:04x}", "pid": f"0x{pid:04x}"}


_SOURCE_INPUTS = {0: "external_supply", 1: "external_supply_unconstrained", 2: "internal_battery"}


def _source_caps_extended(d: bytes) -> dict:
    out = {**_id(d), "xid": f"0x{struct.unpack_from('<I', d, 4)[0]:08x}", "fw_version": d[8],
           "hw_version": d[9], "holdup_time_ms": d[11],
           "source_inputs": _named_bits(d[21], _SOURCE_INPUTS),
           "fixed_batteries": d[22] & 0x0F, "hot_swappable_batteries": d[22] >> 4,
           "source_pdp_w": d[23]}
    if len(d) > 24:
        out["epr_source_pdp_w"] = d[24]
    return out


_SINK_MODES = {0: "pps_charging", 1: "vbus_powered", 2: "mains_powered", 3: "battery_powered",
               4: "battery_unlimited", 5: "avs_support"}


def _sink_caps_extended(d: bytes) -> dict:
    out = {**_id(d), "xid": f"0x{struct.unpack_from('<I', d, 4)[0]:08x}", "fw_version": d[8],
           "hw_version": d[9], "skedb_version": d[10], "load_step": d[11],
           "sink_modes": _named_bits(d[17], _SINK_MODES), "min_pdp_w": d[18] & 0x7F,
           "operational_pdp_w": d[19] & 0x7F, "max_pdp_w": d[20] & 0x7F}
    if len(d) > 23:
        out.update({"epr_min_pdp_w": d[21], "epr_operational_pdp_w": d[22], "epr_max_pdp_w": d[23]})
    return out


_PRESENT_INPUT = {1: "external_power", 2: "external_ac", 3: "internal_battery",
                  4: "internal_non_battery"}
_EVENT_FLAGS = {1: "ocp", 2: "otp", 3: "ovp", 4: "cl_mode"}
_TEMPERATURE = {0: "not_supported", 1: "normal", 2: "warning", 3: "over_temperature"}


def _status(d: bytes) -> dict:
    out = {"internal_temp_c": d[0], "present_input": _named_bits(d[1], _PRESENT_INPUT),
           "present_battery_input": d[2], "event_flags": _named_bits(d[3], _EVENT_FLAGS),
           "temperature_status": _TEMPERATURE[bits(d[4], 2, 1)]}
    if len(d) > 5:
        out["power_status"] = d[5]
    if len(d) > 6:
        out["power_state_change"] = d[6]
    return out


def _pps_status(d: bytes) -> dict:
    voltage, current, flags = struct.unpack_from("<HBB", d)
    return {"output_voltage_v": None if voltage == 0xFFFF else r3(voltage * 0.02),
            "output_current_a": None if current == 0xFF else r3(current * 0.05),
            "temperature": _TEMPERATURE[bits(flags, 2, 1)],
            "current_limit_mode": bool(flags >> 3 & 1)}


def _capacity(raw: int) -> float | None:
    return None if raw == 0xFFFF else r3(raw * 0.1)


def _battery_capabilities(d: bytes) -> dict:
    design, full = struct.unpack_from("<HH", d, 4)
    return {**_id(d), "design_capacity_wh": _capacity(design),
            "last_full_charge_capacity_wh": _capacity(full),
            "invalid_battery_reference": bool(d[8] & 1)}


def _manufacturer_info(d: bytes) -> dict:
    text = d[4:].split(b"\0", 1)[0].decode("utf-8", "replace")
    return {**_id(d), "manufacturer_string": text}


def _get_manufacturer_info(d: bytes) -> dict:
    return {"target": "battery" if d[0] == 1 else "port_or_cable_plug", "reference": d[1]}


_EXTENDED_CONTROL = {1: "EPR_Get_Source_Cap", 2: "EPR_Get_Sink_Cap", 3: "EPR_KeepAlive",
                     4: "EPR_KeepAlive_Ack"}


def _extended_control(d: bytes) -> dict:
    return {"type": _EXTENDED_CONTROL.get(d[0], f"reserved_{d[0]}"), "data": d[1]}


def _country(code: int) -> str:
    return bytes([code >> 8, code & 0xFF]).decode("ascii", "replace")


def _country_codes(d: bytes) -> dict:
    count = d[0]
    return {"country_codes": [_country(struct.unpack_from("<H", d, 2 + 2 * i)[0])
                              for i in range(count) if 4 + 2 * i <= len(d)]}


def _country_info(d: bytes) -> dict:
    return {"country_code": _country(struct.unpack_from("<H", d)[0]), "info_hex": d[4:].hex()}


def _pdo_list(role: str):
    def decode(d: bytes) -> dict:
        pdos = [int.from_bytes(d[i:i + 4], "little") for i in range(0, len(d) - 3, 4)]
        # Positions 1-7 are SPR PDOs (zero-padded), EPR PDOs start at position 8.
        return {"pdos": [{"position": i + 1, **(decode_pdo(p, role) if p else {"type": "unused"})}
                         for i, p in enumerate(pdos)]}
    return decode


_DECODERS = {
    "Source_Capabilities_Extended": _source_caps_extended,
    "Sink_Capabilities_Extended": _sink_caps_extended,
    "Status": _status,
    "PPS_Status": _pps_status,
    "Battery_Capabilities": _battery_capabilities,
    "Get_Battery_Cap": lambda d: {"battery_reference": d[0]},
    "Get_Battery_Status": lambda d: {"battery_reference": d[0]},
    "Manufacturer_Info": _manufacturer_info,
    "Get_Manufacturer_Info": _get_manufacturer_info,
    "Extended_Control": _extended_control,
    "Country_Codes": _country_codes,
    "Country_Info": _country_info,
    "EPR_Source_Capabilities": _pdo_list("source"),
    "EPR_Sink_Capabilities": _pdo_list("sink"),
}


def decode_extended_payload(message_type: str, data: bytes) -> dict | None:
    """Decode a complete extended-message data block; None when the type is not handled."""
    decoder = _DECODERS.get(message_type)
    if decoder is None or not data:
        return None
    try:
        return decoder(data)
    except (IndexError, struct.error):
        return {"error": "data block shorter than expected", "raw": data.hex()}
