from km003c_mcp.pd import PdDecoder, decode_message, decode_pdo
from km003c_mcp.protocol import PdEvent


def test_source_caps_fixed_5v3a():
    m = decode_message(bytes.fromhex("81112c910100"), 0)
    assert (m["type"], m["sop"], m["power_role"], m["spec_revision"]) == (
        "Source_Capabilities", "SOP", "source", "3.x")
    assert m["pdos"][0] == {"position": 1, "type": "fixed", "voltage_v": 5.0, "max_current_a": 3.0}


def test_pps_apdo():
    pdo = (3 << 30) | (0 << 28) | (110 << 17) | (33 << 8) | 60  # 3.3-11 V, 3 A
    assert decode_pdo(pdo) == {"type": "pps", "max_voltage_v": 11.0, "min_voltage_v": 3.3,
                               "max_current_a": 3.0}


def test_epr_avs_and_battery_pdos():
    avs = (3 << 30) | (1 << 28) | (280 << 17) | (150 << 8) | 140
    assert decode_pdo(avs) == {"type": "epr_avs", "max_voltage_v": 28.0, "min_voltage_v": 15.0,
                               "pdp_w": 140}
    bat = (1 << 30) | (400 << 20) | (100 << 10) | 240
    assert decode_pdo(bat) == {"type": "battery", "max_voltage_v": 20.0, "min_voltage_v": 5.0,
                               "max_power_w": 60.0}


def test_control_goodcrc_and_sop_prime():
    m = decode_message(bytes.fromhex("4100"), 1)
    assert (m["type"], m["sop"], m["num_objects"]) == ("GoodCRC", "SOP'", 0)
    assert "power_role" not in m


def test_extended_and_trailing_bytes():
    # Extended Status (type 2), 1 data object, followed by a 4-byte CRC
    hdr = (1 << 15) | (1 << 12) | (2 << 6) | 2
    m = decode_message(hdr.to_bytes(2, "little") + bytes(4) + b"\xaa\xbb\xcc\xdd", 0)
    assert (m["type"], m["extended"]) == ("Status", True)
    assert m["trailing"] == "aabbccdd"


def test_request_uses_previous_source_caps():
    dec = PdDecoder()
    dec.decode(PdEvent(0, "message", 0, bytes.fromhex("81112c910100")))
    rdo = (1 << 28) | (300 << 10) | 300
    req = dec.decode(PdEvent(1, "message", 0, bytes.fromhex("8210") + rdo.to_bytes(4, "little")))
    assert req["request"] == {"object_position": 1, "operating_current_a": 3.0, "max_current_a": 3.0}


def test_request_against_pps_pdo():
    dec = PdDecoder()
    pps = (3 << 30) | (110 << 17) | (33 << 8) | 60
    fixed = (100 << 10) | 300
    caps_hdr = (2 << 12) | (1 << 8) | (2 << 6) | 1
    dec.decode(PdEvent(0, "message", 0, caps_hdr.to_bytes(2, "little")
                       + fixed.to_bytes(4, "little") + pps.to_bytes(4, "little")))
    rdo = (2 << 28) | (450 << 9) | 40  # 9.0 V, 2.0 A
    req = dec.decode(PdEvent(1, "message", 0, bytes.fromhex("8210") + rdo.to_bytes(4, "little")))
    assert req["request"] == {"object_position": 2, "output_voltage_v": 9.0, "operating_current_a": 2.0}


def test_connect_event_passthrough():
    assert PdDecoder().decode(PdEvent(5, "connect", None, b"")) == {"event": "connect"}


def test_too_short_wire():
    assert "error" in decode_message(b"\x01", 0)
