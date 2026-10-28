"""Structured VDM decoding: Discover Identity/SVIDs/Modes, DisplayPort and Thunderbolt 3.

Bit layouts follow USB PD R3.1, the VESA DP Alt Mode spec and the Linux kernel's
include/linux/usb/pd_vdo.h and typec_tbt.h.
"""

from __future__ import annotations

from .pd_objects import bits

SVID_PD = 0xFF00
SVID_DP = 0xFF01
SVID_TBT = 0x8087
SVID_NAMES = {SVID_PD: "PD_SID", SVID_DP: "DisplayPort", SVID_TBT: "Intel (Thunderbolt)"}
VDM_COMMANDS = {1: "Discover_Identity", 2: "Discover_SVIDs", 3: "Discover_Modes", 4: "Enter_Mode",
                5: "Exit_Mode", 6: "Attention", 16: "DP_Status_Update", 17: "DP_Configure"}
VDM_COMMAND_TYPES = {0: "REQ", 1: "ACK", 2: "NAK", 3: "BUSY"}

# --- Discover Identity --------------------------------------------------------------

_UFP_TYPES = {0: "not_ufp", 1: "pdusb_hub", 2: "pdusb_peripheral", 3: "psd", 5: "ama"}
_CABLE_TYPES = {0: "not_cable", 3: "passive_cable", 4: "active_cable", 6: "vpd"}
_DFP_TYPES = {0: "not_dfp", 1: "pdusb_hub", 2: "pdusb_host", 3: "power_brick"}
_IDH_CONNECTORS = {0: "unspecified", 1: "reserved", 2: "receptacle", 3: "plug"}
_VDO_CONNECTORS = {0: "reserved", 1: "reserved", 2: "receptacle", 3: "captive_plug"}
_USB_SPEEDS = {0: "USB2.0", 1: "USB3.2_Gen1", 2: "USB3.2_Gen2_or_USB4_Gen2", 3: "USB4_Gen3",
               4: "USB4_Gen4"}
_PLUG_TYPES = {0: "type_a", 1: "type_b", 2: "type_c", 3: "captive"}
_LATENCIES = {1: "<10ns (~1m)", 2: "10-20ns (~2m)", 3: "20-30ns (~3m)", 4: "30-40ns (~4m)",
              5: "40-50ns (~5m)", 6: "50-60ns (~6m)", 7: "60-70ns (~7m)", 8: ">70ns (>~7m)",
              9: "1000ns (~100m)", 10: "2000ns (~200m)", 11: "3000ns (~300m)"}
_PASSIVE_TERMINATION = {0: "vconn_not_required", 1: "vconn_required"}
_ACTIVE_TERMINATION = {2: "one_end_active", 3: "both_ends_active"}
_MAX_VBUS = {0: "20V", 1: "30V", 2: "40V", 3: "50V"}
_CABLE_CURRENT = {0: "default", 1: "3A", 2: "5A", 3: "reserved"}
_U3_CLD_POWER = {0: ">10mW", 1: "10mW", 2: "5mW", 3: "1mW", 4: "500uW", 5: "200uW", 6: "50uW"}
_VCONN_POWER = {0: "1W", 1: "1.5W", 2: "2W", 3: "3W", 4: "4W", 5: "5W", 6: "6W"}
_DEVICE_CAPABILITY = {0: "USB2.0", 1: "USB2.0_billboard", 2: "USB3.2", 3: "USB4"}
_HOST_CAPABILITY = {0: "USB2.0", 1: "USB3.2", 2: "USB4"}
_UFP_ALT_MODES = {0: "tbt3", 1: "reconfigurable", 2: "non_reconfigurable"}


def _named_bits(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value >> bit & 1]


def decode_id_header(vdo: int, sop: int) -> dict:
    ptype = bits(vdo, 29, 27)
    out = {"usb_host": bool(vdo >> 31 & 1), "usb_device": bool(vdo >> 30 & 1),
           "product_type": (_UFP_TYPES if sop == 0 else _CABLE_TYPES).get(ptype, f"reserved_{ptype}"),
           "modal_operation": bool(vdo >> 26 & 1)}
    if sop == 0:
        dfp = bits(vdo, 25, 23)
        out["dfp_product_type"] = _DFP_TYPES.get(dfp, f"reserved_{dfp}")
    out["connector_type"] = _IDH_CONNECTORS[bits(vdo, 22, 21)]
    out["vid"] = f"0x{vdo & 0xFFFF:04x}"
    return out


def _versions(vdo: int) -> dict:
    return {"hw_version": bits(vdo, 31, 28), "fw_version": bits(vdo, 27, 24),
            "vdo_version": bits(vdo, 23, 21)}


def _cable_common(vdo: int, termination: dict[int, str]) -> dict:
    term = bits(vdo, 12, 11)
    return {**_versions(vdo), "plug_type": _PLUG_TYPES[bits(vdo, 19, 18)],
            "epr_capable": bool(vdo >> 17 & 1),
            "latency": _LATENCIES.get(bits(vdo, 16, 13), f"reserved_{bits(vdo, 16, 13)}"),
            "termination": termination.get(term, f"reserved_{term}"),
            "max_vbus": _MAX_VBUS[bits(vdo, 10, 9)]}


def decode_passive_cable(vdo: int) -> dict:
    return {**_cable_common(vdo, _PASSIVE_TERMINATION),
            "vbus_current": _CABLE_CURRENT[bits(vdo, 6, 5)],
            "usb_speed": _USB_SPEEDS.get(bits(vdo, 2, 0), "reserved")}


def decode_active_cable(vdo: int) -> dict:
    return {**_cable_common(vdo, _ACTIVE_TERMINATION),
            "sbu_supported": not vdo >> 8 & 1,
            "sbu_type": "active" if vdo >> 7 & 1 else "passive",
            "vbus_current": _CABLE_CURRENT[bits(vdo, 6, 5)],
            "vbus_through_cable": bool(vdo >> 4 & 1), "sop2_controller": bool(vdo >> 3 & 1),
            "usb_speed": _USB_SPEEDS.get(bits(vdo, 2, 0), "reserved")}


def decode_active_cable_2(vdo: int) -> dict:
    return {"max_operating_temp_c": bits(vdo, 31, 24), "shutdown_temp_c": bits(vdo, 23, 16),
            "u3_cld_power": _U3_CLD_POWER.get(bits(vdo, 14, 12), "reserved"),
            "u3_to_u0": "through_u3s" if vdo >> 11 & 1 else "direct",
            "physical": "optical" if vdo >> 10 & 1 else "copper",
            "active_element": "retimer" if vdo >> 9 & 1 else "redriver",
            "usb4_supported": not vdo >> 8 & 1, "usb2_hub_hops": bits(vdo, 7, 6),
            "usb2_supported": not vdo >> 5 & 1, "usb32_supported": not vdo >> 4 & 1,
            "usb_lanes": 2 if vdo >> 3 & 1 else 1, "optically_isolated": bool(vdo >> 2 & 1),
            "usb_gen": "gen2+" if vdo & 1 else "gen1"}


def decode_ufp_vdo(vdo: int) -> dict:
    out = {"vdo_version": bits(vdo, 31, 29),
           "device_capability": _named_bits(bits(vdo, 27, 24), _DEVICE_CAPABILITY),
           "connector_type": _VDO_CONNECTORS[bits(vdo, 23, 22)],
           "alternate_modes": _named_bits(bits(vdo, 5, 3), _UFP_ALT_MODES),
           "usb_speed": _USB_SPEEDS.get(bits(vdo, 2, 0), "reserved")}
    if out["alternate_modes"]:
        # VCONN/VBUS fields are only meaningful for alternate-mode adapters.
        out.update({"vconn_power": _VCONN_POWER.get(bits(vdo, 10, 8), "reserved"),
                    "vconn_required": bool(vdo >> 7 & 1), "vbus_required": not vdo >> 6 & 1})
    return out


def decode_dfp_vdo(vdo: int) -> dict:
    return {"vdo_version": bits(vdo, 31, 29),
            "host_capability": _named_bits(bits(vdo, 26, 24), _HOST_CAPABILITY),
            "connector_type": _VDO_CONNECTORS[bits(vdo, 23, 22)], "port_number": bits(vdo, 4, 0)}


def decode_ama_vdo(vdo: int) -> dict:
    return {**_versions(vdo), "vconn_power": _VCONN_POWER.get(bits(vdo, 10, 8), "reserved"),
            "vconn_required": bool(vdo >> 7 & 1), "vbus_required": not vdo >> 6 & 1,
            "usb_speed": _USB_SPEEDS.get(bits(vdo, 2, 0), "reserved")}


def decode_vpd_vdo(vdo: int) -> dict:
    return {**_versions(vdo), "max_vbus": _MAX_VBUS[bits(vdo, 16, 15)],
            "charge_through_current": "5A" if vdo >> 14 & 1 else "3A",
            "vbus_impedance_mohm": bits(vdo, 12, 7) * 2, "ground_impedance_mohm": bits(vdo, 6, 1),
            "charge_through": bool(vdo & 1)}


def decode_identity(vdos: list[int], sop: int) -> dict:
    """Decode the VDOs of a Discover_Identity ACK (ID header, cert stat, product, type VDOs)."""
    if not vdos:
        return {}
    out: dict = {"id_header": decode_id_header(vdos[0], sop)}
    if len(vdos) > 1:
        out["cert_xid"] = f"0x{vdos[1]:08x}"
    if len(vdos) > 2:
        out["product"] = {"pid": f"0x{vdos[2] >> 16:04x}", "bcd_device": f"0x{vdos[2] & 0xFFFF:04x}"}
    ptype = bits(vdos[0], 29, 27)
    extra = vdos[3:]
    if sop != 0:
        if ptype == 3 and extra:
            out["passive_cable"] = decode_passive_cable(extra[0])
        elif ptype == 4 and extra:
            out["active_cable"] = decode_active_cable(extra[0])
            if len(extra) > 1:
                out["active_cable_2"] = decode_active_cable_2(extra[1])
        elif ptype == 6 and extra:
            out["vpd"] = decode_vpd_vdo(extra[0])
        return out
    dfp = bits(vdos[0], 25, 23)
    if ptype in (1, 2) and extra:
        out["ufp"] = decode_ufp_vdo(extra[0])
    elif ptype == 5 and extra:
        out["ama"] = decode_ama_vdo(extra[0])
    if dfp in (1, 2, 3):
        # With a UFP product type the DFP VDO follows a zero pad VDO.
        index = 2 if ptype else 0
        if len(extra) > index:
            out["dfp"] = decode_dfp_vdo(extra[index])
    return out


# --- DisplayPort -----------------------------------------------------------------------

DP_PORT_CAPABILITY = {0: "reserved", 1: "UFP_D", 2: "DFP_D", 3: "UFP_D_and_DFP_D"}
DP_CONNECTED = {0: "none", 1: "DFP_D", 2: "UFP_D", 3: "both"}
DP_CONFIGURATION = {0: "USB", 1: "UFP_U_as_DFP_D", 2: "UFP_U_as_UFP_D", 3: "reserved"}


def _pins(mask: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(6) if mask & (1 << i)]


def decode_dp_capabilities(vdo: int) -> dict:
    return {"port_capability": DP_PORT_CAPABILITY[bits(vdo, 1, 0)], "dp13": bool(vdo & 1 << 2),
            "gen2": bool(vdo & 1 << 3), "receptacle": bool(vdo & 1 << 6),
            "dfp_d_pins": _pins(bits(vdo, 15, 8)), "ufp_d_pins": _pins(bits(vdo, 23, 16))}


def decode_dp_status(vdo: int) -> dict:
    return {"connected": DP_CONNECTED[bits(vdo, 1, 0)], "power_low": bool(vdo & 1 << 2),
            "enabled": bool(vdo & 1 << 3), "multi_function_preferred": bool(vdo & 1 << 4),
            "usb_config_request": bool(vdo & 1 << 5), "exit_request": bool(vdo & 1 << 6),
            "hpd": bool(vdo & 1 << 7), "irq_hpd": bool(vdo & 1 << 8)}


def decode_dp_configuration(vdo: int) -> dict:
    return {"configuration": DP_CONFIGURATION[bits(vdo, 1, 0)], "signaling": bits(vdo, 5, 2),
            "pin_assignment": _pins(bits(vdo, 15, 8))}


# --- Thunderbolt 3 ---------------------------------------------------------------------

_TBT_CABLE_SPEEDS = {0: "none", 1: "USB3_Gen1", 2: "USB3_Gen2_10Gbps", 3: "10_and_20Gbps"}
_TBT_ROUNDED = {0: "gen3_non_rounded", 1: "gen3_gen4_rounded_and_non_rounded"}


def _tbt_vendor_bits(vdo: int) -> dict:
    return {"intel_specific_b0": bool(vdo >> 26 & 1), "vendor_specific_b0": bool(vdo >> 30 & 1),
            "vendor_specific_b1": bool(vdo >> 31 & 1)}


def _tbt_cable_speed(vdo: int) -> dict:
    return {"cable_speed": _TBT_CABLE_SPEEDS.get(bits(vdo, 18, 16), "reserved"),
            "rounded_support": _TBT_ROUNDED.get(bits(vdo, 20, 19), "reserved")}


def decode_tbt_vdo(vdo: int, cable: bool) -> dict:
    """Decode a Thunderbolt 3 Discover Mode VDO from a cable (SOP'/SOP'') or a device (SOP)."""
    out: dict = {"mode": f"0x{vdo & 0xFFFF:04x}"}
    if cable:
        out.update({**_tbt_cable_speed(vdo), "optical": bool(vdo >> 21 & 1),
                    "retimer": bool(vdo >> 22 & 1), "link_training": bool(vdo >> 23 & 1),
                    "active": bool(vdo >> 25 & 1)})
    else:
        out.update({"adapter": "tbt3" if vdo >> 16 & 1 else "legacy", **_tbt_vendor_bits(vdo)})
    return out


def decode_tbt_enter(vdo: int) -> dict:
    return {"mode": f"0x{vdo & 0xFFFF:04x}", **_tbt_cable_speed(vdo),
            "uni_dir_lsrx": bool(vdo >> 23 & 1), "active_cable": bool(vdo >> 24 & 1),
            **_tbt_vendor_bits(vdo)}


# --- VDM -------------------------------------------------------------------------------

def decode_vdm(objects: list[int], sop: int = 0) -> dict:
    """Decode a Vendor_Defined message (VDM header plus the VDOs of known commands)."""
    header, vdos = objects[0], objects[1:]
    svid = header >> 16
    out: dict = {"svid": f"0x{svid:04x}", "svid_name": SVID_NAMES.get(svid, "unknown"),
                 "structured": bool(header & 1 << 15)}
    if not out["structured"]:
        out["vendor_payload"] = f"{header & 0x7FFF:04x}"
        return out
    command = bits(header, 4, 0)
    ctype = VDM_COMMAND_TYPES[bits(header, 7, 6)]
    out.update({"command": VDM_COMMANDS.get(command, f"SVID_Specific_{command}"),
                "command_type": ctype, "object_position": bits(header, 10, 8),
                "version": f"{bits(header, 14, 13) + 1}.{bits(header, 12, 11)}"})
    if not vdos:
        return out
    if command == 1 and ctype == "ACK":
        out["identity"] = decode_identity(vdos, sop)
    elif command == 2 and ctype == "ACK":
        out["svids"] = [f"0x{v:04x}" for vdo in vdos for v in (vdo >> 16, vdo & 0xFFFF) if v]
    elif svid == SVID_DP:
        if command == 3 and ctype == "ACK":
            out["dp_capabilities"] = [decode_dp_capabilities(v) for v in vdos]
        elif command in (6, 16):
            out["dp_status"] = decode_dp_status(vdos[0])
        elif command == 17:
            out["dp_configuration"] = decode_dp_configuration(vdos[0])
    elif svid == SVID_TBT:
        if command == 3 and ctype == "ACK":
            out["tbt_modes"] = [decode_tbt_vdo(v, cable=sop != 0) for v in vdos]
        elif command == 4:
            out["tbt_enter"] = decode_tbt_enter(vdos[0])
    return out
