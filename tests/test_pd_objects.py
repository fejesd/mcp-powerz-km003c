from km003c_mcp.pd_objects import (
    decode_alert, decode_battery_status, decode_bist, decode_data_object, decode_enter_usb,
    decode_epr_mode, decode_pdo, decode_request, decode_revision, decode_source_info,
)


def test_fixed_source_pdo_flags():
    # Real source PDO: 5 V 0.9 A, dual-role power/data, USB comms, unchunked extended messages.
    assert decode_pdo(0x2701905A, role="source") == {
        "type": "fixed", "voltage_v": 5.0, "max_current_a": 0.9,
        "flags": ["dual_role_power", "usb_comms_capable", "dual_role_data",
                  "unchunked_extended_supported"]}


def test_fixed_sink_pdo_flags():
    # Real sink PDO from Sink_Capabilities: 5 V 0.1 A, dual-role power, higher capability.
    d = decode_pdo(0x2E01900A, role="sink")
    assert d["voltage_v"] == 5.0 and d["operational_current_a"] == 0.1
    assert d["flags"] == ["dual_role_power", "unconstrained_power", "usb_comms_capable",
                          "dual_role_data"]


def test_fixed_pdo_without_flags_has_no_flags_key():
    assert decode_pdo((100 << 10) | 300) == {"type": "fixed", "voltage_v": 5.0, "max_current_a": 3.0}


def test_fixed_source_pdo_epr_and_peak():
    pdo = (1 << 23) | (2 << 20) | (400 << 10) | 500
    d = decode_pdo(pdo, role="source")
    assert d["flags"] == ["epr_capable"] and d["peak_current"] == 2


def test_request_fixed_with_flags():
    rdo = (3 << 28) | (1 << 25) | (1 << 24) | (300 << 10) | 300
    assert decode_request(rdo, [{"type": "fixed"}] * 3) == {
        "object_position": 3, "operating_current_a": 3.0, "max_current_a": 3.0,
        "flags": ["usb_comms_capable", "no_usb_suspend"]}


def test_request_pps_avs_and_battery():
    assert decode_request((1 << 28) | (450 << 9) | 40, [{"type": "pps"}]) == {
        "object_position": 1, "output_voltage_v": 9.0, "operating_current_a": 2.0}
    # AVS output voltage is in 25 mV units.
    assert decode_request((1 << 28) | (1120 << 9) | 60, [{"type": "epr_avs"}]) == {
        "object_position": 1, "output_voltage_v": 28.0, "operating_current_a": 3.0}
    assert decode_request((1 << 28) | (120 << 10) | 240, [{"type": "battery"}]) == {
        "object_position": 1, "operating_power_w": 30.0, "max_power_w": 60.0}


def test_bist():
    assert decode_bist(0x50000000) == {"mode": "BIST_Carrier_Mode"}
    assert decode_bist(0x80000000) == {"mode": "BIST_Test_Data"}


def test_battery_status_real():
    # Real Battery_Status BSDO: 1.6 Wh present, battery present, charging.
    assert decode_battery_status(0x00100200) == {
        "present_capacity_wh": 1.6, "invalid_battery_reference": False, "battery_present": True,
        "charging_status": "charging"}


def test_alert_real():
    # Real Alert ADO: battery status change on fixed battery 0.
    assert decode_alert(0x02100000) == {
        "alerts": ["battery_status_change"], "fixed_batteries": [0], "hot_swappable_batteries": [],
        "extended_alert": None}


def test_alert_extended_event():
    assert decode_alert(0x80000002)["extended_alert"] == "power_button_press"


def test_enter_usb():
    eudo = (2 << 28) | (1 << 26) | (3 << 21) | (1 << 19) | (3 << 17) | (1 << 15) | (1 << 13)
    assert decode_enter_usb(eudo) == {
        "usb_mode": "USB4", "usb4_drd": True, "usb3_drd": False, "cable_speed": "USB4_Gen3",
        "cable_type": "active_retimer", "cable_current": "5A", "pcie_supported": False,
        "dp_supported": True, "tbt_supported": False, "host_present": True}


def test_epr_mode():
    assert decode_epr_mode(0x01F00000) == {"action": "Enter", "data": 240}
    assert decode_epr_mode(0x04030000) == {"action": "Enter_Failed", "data": 3}


def test_source_info():
    assert decode_source_info(0x80645A41) == {
        "port_type": "guaranteed", "max_pdp_w": 100, "present_pdp_w": 90, "reported_pdp_w": 65}


def test_revision():
    assert decode_revision(0x31110000) == {"revision": "3.1", "version": "1.1"}


def test_decode_data_object_dispatch():
    assert decode_data_object("Battery_Status", 0x00100200)["battery_present"] is True
    assert decode_data_object("Get_Country_Info", 0x55530000) == {"country_code": "US"}
    assert decode_data_object("Unknown", 1) is None
