import struct

from km003c_mcp.pd import PdDecoder, decode_message
from km003c_mcp.pd_extended import decode_extended_payload
from km003c_mcp.protocol import PdEvent

# Real extended messages from a charger + monitor capture (KM003C fw 1.8.4).
CABLE_MANUFACTURER_INFO = bytes.fromhex(
    "87f51a80ae1a000000000000000000000000000000000000000000000000")
CABLE_GET_MANUFACTURER_INFO = bytes.fromhex("869602800000")
MONITOR_EMPTY_STATUS = bytes.fromhex("828c")


def ext_msg(msg_type: int, data: bytes, chunk: int = 0, size: int | None = None,
            request: bool = False) -> bytes:
    size = len(data) if size is None else size
    num_objects = -(-(2 + len(data)) // 4)
    header = (1 << 15) | (num_objects << 12) | (1 << 8) | (2 << 6) | msg_type
    ext = (1 << 15) | (chunk << 11) | (int(request) << 10) | size
    body = struct.pack("<HH", header, ext) + data
    return body + bytes(2 + 4 * num_objects - len(body))


def test_real_manufacturer_info():
    m = decode_message(CABLE_MANUFACTURER_INFO, 1)
    assert m["type"] == "Manufacturer_Info"
    assert m["extended_header"] == {"chunked": True, "chunk_number": 0, "request_chunk": False,
                                    "data_size": 26}
    assert m["payload"] == {"vid": "0x1aae", "pid": "0x0000", "manufacturer_string": ""}


def test_real_get_manufacturer_info():
    m = decode_message(CABLE_GET_MANUFACTURER_INFO, 1)
    assert m["payload"] == {"target": "port_or_cable_plug", "reference": 0}


def test_extended_message_without_payload():
    m = decode_message(MONITOR_EMPTY_STATUS, 0)
    assert m["type"] == "Status" and m["extended_header"] is None
    assert "no extended payload" in m["note"]


def test_source_capabilities_extended():
    data = struct.pack("<HHIBBBBBBHHHBBBBB", 0x1234, 0x5678, 0xAABBCCDD, 2, 1, 0, 3, 0, 0,
                       0, 0, 0, 0, 0b011, 0x21, 65, 140)
    assert decode_extended_payload("Source_Capabilities_Extended", data) == {
        "vid": "0x1234", "pid": "0x5678", "xid": "0xaabbccdd", "fw_version": 2, "hw_version": 1,
        "holdup_time_ms": 3, "source_inputs": ["external_supply", "external_supply_unconstrained"],
        "fixed_batteries": 1, "hot_swappable_batteries": 2, "source_pdp_w": 65,
        "epr_source_pdp_w": 140}


def test_sink_capabilities_extended():
    data = struct.pack("<HHIBBBBHBBBBBBBBBB", 0x1234, 0x5678, 0, 1, 2, 1, 0, 0, 0, 0, 0,
                       0b100001, 15, 45, 60, 0, 0, 0)
    out = decode_extended_payload("Sink_Capabilities_Extended", data)
    assert out["sink_modes"] == ["pps_charging", "avs_support"]
    assert (out["min_pdp_w"], out["operational_pdp_w"], out["max_pdp_w"]) == (15, 45, 60)
    assert out["epr_max_pdp_w"] == 0


def test_status():
    assert decode_extended_payload("Status", bytes([45, 0b10, 0, 0b100, 0b100, 0, 0])) == {
        "internal_temp_c": 45, "present_input": ["external_power"], "present_battery_input": 0,
        "event_flags": ["otp"], "temperature_status": "warning", "power_status": 0,
        "power_state_change": 0}


def test_pps_status():
    assert decode_extended_payload("PPS_Status", struct.pack("<HBB", 450, 40, 0b1010)) == {
        "output_voltage_v": 9.0, "output_current_a": 2.0, "temperature": "normal",
        "current_limit_mode": True}
    assert decode_extended_payload("PPS_Status", struct.pack("<HBB", 0xFFFF, 0xFF, 0)) == {
        "output_voltage_v": None, "output_current_a": None, "temperature": "not_supported",
        "current_limit_mode": False}


def test_battery_capabilities():
    assert decode_extended_payload("Battery_Capabilities", struct.pack("<HHHHB", 1, 2, 500, 450, 0)) == {
        "vid": "0x0001", "pid": "0x0002", "design_capacity_wh": 50.0,
        "last_full_charge_capacity_wh": 45.0, "invalid_battery_reference": False}


def test_extended_control():
    assert decode_extended_payload("Extended_Control", bytes([3, 0])) == {"type": "EPR_KeepAlive",
                                                                           "data": 0}


def test_epr_source_capabilities_reassembled_from_chunks():
    pdos = [(100 << 10) | 300] + [0] * 6 + [(3 << 30) | (1 << 28) | (280 << 17) | (150 << 8) | 140]
    data = b"".join(p.to_bytes(4, "little") for p in pdos)
    dec = PdDecoder()
    first = dec.decode(PdEvent(0, "message", 0, ext_msg(17, data[:26], 0, len(data))))
    assert first["partial"] is True and "payload" not in first
    dec.decode(PdEvent(1, "message", 0, ext_msg(17, b"", 1, len(data), request=True)))
    last = dec.decode(PdEvent(2, "message", 0, ext_msg(17, data[26:], 1, len(data))))
    assert last["reassembled"] is True
    assert last["payload"]["pdos"][7] == {"position": 8, "type": "epr_avs", "max_voltage_v": 28.0,
                                          "min_voltage_v": 15.0, "pdp_w": 140}
    assert dec.source_pdos[7]["type"] == "epr_avs"
