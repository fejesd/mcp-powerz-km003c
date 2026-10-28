from km003c_mcp.pd import decode_message
from km003c_mcp.pd_vdm import decode_identity, decode_tbt_vdo, decode_vdm

# Real Discover_Identity ACKs from a charger + LG monitor capture (KM003C fw 1.8.4).
CABLE_IDENTITY = bytes.fromhex("8f5141a000ffae1a601c000000000000000021480800")
MONITOR_IDENTITY = bytes.fromhex("8f5041a000ff3e044054000000000000029c91018044")


def test_real_passive_cable_identity():
    m = decode_message(CABLE_IDENTITY, 1)
    assert m["cable_plug"] is True
    ident = m["vdm"]["identity"]
    assert ident["id_header"] == {
        "usb_host": False, "usb_device": False, "product_type": "passive_cable",
        "modal_operation": True, "connector_type": "plug", "vid": "0x1aae"}
    assert ident["cert_xid"] == "0x00000000"
    assert ident["product"] == {"pid": "0x0000", "bcd_device": "0x0000"}
    assert ident["passive_cable"] == {
        "hw_version": 0, "fw_version": 0, "vdo_version": 0, "plug_type": "type_c",
        "epr_capable": False, "latency": "10-20ns (~2m)", "termination": "vconn_required",
        "max_vbus": "20V", "vbus_current": "3A", "usb_speed": "USB3.2_Gen1"}


def test_real_monitor_identity():
    ident = decode_message(MONITOR_IDENTITY, 0)["vdm"]["identity"]
    assert ident["id_header"] == {
        "usb_host": False, "usb_device": True, "product_type": "pdusb_peripheral",
        "modal_operation": True, "dfp_product_type": "not_dfp", "connector_type": "receptacle",
        "vid": "0x043e"}
    assert ident["product"] == {"pid": "0x9c02", "bcd_device": "0x0000"}
    assert ident["ufp"] == {
        "vdo_version": 2, "device_capability": ["USB3.2"], "connector_type": "receptacle",
        "alternate_modes": ["reconfigurable"], "usb_speed": "USB3.2_Gen1",
        "vconn_power": "1.5W", "vconn_required": True, "vbus_required": True}


def _idh(ptype, dfp=0, conn=3, host=0, dev=0, modal=0, vid=0x1234):
    return host << 31 | dev << 30 | ptype << 27 | modal << 26 | dfp << 23 | conn << 21 | vid


def test_active_cable_identity():
    vdo1 = (2 << 18) | (9 << 13) | (3 << 11) | (3 << 9) | (1 << 7) | (2 << 5) | (1 << 4) | (1 << 3) | 3
    vdo2 = (85 << 24) | (95 << 16) | (2 << 12) | (1 << 10) | (1 << 9) | (1 << 6) | (1 << 4) | (1 << 3) | 1
    ident = decode_identity([_idh(4), 0, 0, vdo1, vdo2], sop=1)
    assert ident["active_cable"] == {
        "hw_version": 0, "fw_version": 0, "vdo_version": 0, "plug_type": "type_c",
        "epr_capable": False, "latency": "1000ns (~100m)", "termination": "both_ends_active",
        "max_vbus": "50V", "sbu_supported": True, "sbu_type": "active", "vbus_current": "5A",
        "vbus_through_cable": True, "sop2_controller": True, "usb_speed": "USB4_Gen3"}
    assert ident["active_cable_2"] == {
        "max_operating_temp_c": 85, "shutdown_temp_c": 95, "u3_cld_power": "5mW",
        "u3_to_u0": "direct", "physical": "optical", "active_element": "retimer",
        "usb4_supported": True, "usb2_hub_hops": 1, "usb2_supported": True,
        "usb32_supported": False, "usb_lanes": 2, "optically_isolated": False, "usb_gen": "gen2+"}


def test_dfp_identity_uses_pad_slot():
    dfp_vdo = (1 << 29) | (0b110 << 24) | (2 << 22) | 3
    ident = decode_identity([_idh(2, dfp=2, conn=2, host=1, dev=1), 0, 0, 0, 0, dfp_vdo], sop=0)
    assert ident["id_header"]["dfp_product_type"] == "pdusb_host"
    assert ident["dfp"] == {"vdo_version": 1, "host_capability": ["USB3.2", "USB4"],
                            "connector_type": "receptacle", "port_number": 3}


def test_vpd_identity():
    vpd = (1 << 15) | (1 << 14) | (10 << 7) | (5 << 1) | 1
    assert decode_identity([_idh(6), 0, 0, vpd], sop=1)["vpd"] == {
        "hw_version": 0, "fw_version": 0, "vdo_version": 0, "max_vbus": "30V",
        "charge_through_current": "5A", "vbus_impedance_mohm": 20, "ground_impedance_mohm": 5,
        "charge_through": True}


def test_tbt_cable_mode():
    vdo = (1 << 25) | (1 << 22) | (1 << 19) | (3 << 16) | 1
    assert decode_tbt_vdo(vdo, cable=True) == {
        "mode": "0x0001", "cable_speed": "10_and_20Gbps",
        "rounded_support": "gen3_gen4_rounded_and_non_rounded", "optical": False, "retimer": True,
        "link_training": False, "active": True}


def test_tbt_discover_modes_and_enter_mode():
    device_vdo = (1 << 26) | (1 << 16) | 1
    modes = decode_vdm([0x8087A043, device_vdo])
    assert modes["svid_name"] == "Intel (Thunderbolt)"
    assert modes["tbt_modes"] == [{"mode": "0x0001", "adapter": "tbt3", "intel_specific_b0": True,
                                   "vendor_specific_b0": False, "vendor_specific_b1": False}]
    enter_vdo = (1 << 24) | (3 << 16) | 1
    enter = decode_vdm([0x80878104, enter_vdo])
    assert enter["tbt_enter"]["cable_speed"] == "10_and_20Gbps"
    assert enter["tbt_enter"]["active_cable"] is True
