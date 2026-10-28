"""USB PD data objects: PDOs, RDOs and the PD 3.x data-message objects (USB PD R3.1)."""

from __future__ import annotations


def bits(value: int, high: int, low: int) -> int:
    return (value >> low) & ((1 << (high - low + 1)) - 1)


def r3(x: float) -> float:
    return round(x, 3)


def _flags(value: int, table: dict[int, str]) -> list[str]:
    return [name for bit, name in sorted(table.items(), reverse=True) if value >> bit & 1]


_SOURCE_FIXED_FLAGS = {29: "dual_role_power", 28: "usb_suspend_supported",
                       27: "unconstrained_power", 26: "usb_comms_capable", 25: "dual_role_data",
                       24: "unchunked_extended_supported", 23: "epr_capable"}
_SINK_FIXED_FLAGS = {29: "dual_role_power", 28: "higher_capability", 27: "unconstrained_power",
                     26: "usb_comms_capable", 25: "dual_role_data"}
_RDO_FLAGS = {27: "giveback", 26: "capability_mismatch", 25: "usb_comms_capable",
              24: "no_usb_suspend", 23: "unchunked_extended_supported", 22: "epr_capable"}
_PROGRAMMABLE = {"pps", "epr_avs", "spr_avs"}


def decode_pdo(pdo: int, role: str = "source") -> dict:
    """Decode a Power Data Object; `role` selects source or sink flag/field meanings."""
    kind = bits(pdo, 31, 30)
    if kind == 0:
        current_key = "max_current_a" if role == "source" else "operational_current_a"
        out = {"type": "fixed", "voltage_v": r3(bits(pdo, 19, 10) * 0.05),
               current_key: r3(bits(pdo, 9, 0) * 0.01)}
        flags = _flags(pdo, _SOURCE_FIXED_FLAGS if role == "source" else _SINK_FIXED_FLAGS)
        if flags:
            out["flags"] = flags
        if role == "source" and bits(pdo, 21, 20):
            out["peak_current"] = bits(pdo, 21, 20)
        if role == "sink" and bits(pdo, 24, 23):
            out["fast_role_swap_current"] = bits(pdo, 24, 23)
        return out
    if kind == 1:
        return {"type": "battery", "max_voltage_v": r3(bits(pdo, 29, 20) * 0.05),
                "min_voltage_v": r3(bits(pdo, 19, 10) * 0.05),
                "max_power_w": r3(bits(pdo, 9, 0) * 0.25)}
    if kind == 2:
        return {"type": "variable", "max_voltage_v": r3(bits(pdo, 29, 20) * 0.05),
                "min_voltage_v": r3(bits(pdo, 19, 10) * 0.05),
                "max_current_a": r3(bits(pdo, 9, 0) * 0.01)}
    apdo = bits(pdo, 29, 28)
    if apdo == 0:
        return {"type": "pps", "max_voltage_v": r3(bits(pdo, 24, 17) * 0.1),
                "min_voltage_v": r3(bits(pdo, 15, 8) * 0.1),
                "max_current_a": r3(bits(pdo, 6, 0) * 0.05)}
    if apdo == 1:
        return {"type": "epr_avs", "max_voltage_v": r3(bits(pdo, 25, 17) * 0.1),
                "min_voltage_v": r3(bits(pdo, 15, 8) * 0.1), "pdp_w": bits(pdo, 7, 0)}
    if apdo == 2:
        return {"type": "spr_avs", "max_current_15v_a": r3(bits(pdo, 19, 10) * 0.01),
                "max_current_20v_a": r3(bits(pdo, 9, 0) * 0.01)}
    return {"type": "reserved_apdo", "raw": f"{pdo:08x}"}


def decode_request(rdo: int, source_pdos: list[dict] | None = None) -> dict:
    """Decode a Request Data Object against the PDO it references (when known)."""
    position = bits(rdo, 31, 28)
    pdo_type = None
    if source_pdos and 1 <= position <= len(source_pdos):
        pdo_type = source_pdos[position - 1]["type"]
    out: dict = {"object_position": position}
    if pdo_type in _PROGRAMMABLE:
        unit = 0.02 if pdo_type == "pps" else 0.025
        out["output_voltage_v"] = r3(bits(rdo, 20, 9) * unit)
        out["operating_current_a"] = r3(bits(rdo, 6, 0) * 0.05)
    elif pdo_type == "battery":
        out["operating_power_w"] = r3(bits(rdo, 19, 10) * 0.25)
        out["max_power_w"] = r3(bits(rdo, 9, 0) * 0.25)
    else:
        out["operating_current_a"] = r3(bits(rdo, 19, 10) * 0.01)
        out["max_current_a"] = r3(bits(rdo, 9, 0) * 0.01)
    flags = _flags(rdo, _RDO_FLAGS)
    if flags:
        out["flags"] = flags
    return out


_BIST_MODES = {5: "BIST_Carrier_Mode", 8: "BIST_Test_Data", 9: "BIST_Shared_Test_Mode_Entry",
               10: "BIST_Shared_Test_Mode_Exit"}


def decode_bist(bdo: int) -> dict:
    mode = bits(bdo, 31, 28)
    return {"mode": _BIST_MODES.get(mode, f"reserved_{mode}")}


_CHARGING_STATUS = {0: "charging", 1: "discharging", 2: "idle", 3: "reserved"}


def decode_battery_status(bsdo: int) -> dict:
    capacity = bits(bsdo, 31, 16)
    return {"present_capacity_wh": None if capacity == 0xFFFF else r3(capacity * 0.1),
            "invalid_battery_reference": bool(bsdo >> 8 & 1),
            "battery_present": bool(bsdo >> 9 & 1),
            "charging_status": _CHARGING_STATUS[bits(bsdo, 11, 10)]}


_ALERT_TYPES = {1: "battery_status_change", 2: "ocp", 3: "otp", 4: "operating_condition_change",
                5: "source_input_change", 6: "ovp", 7: "extended_alert_event"}
_EXTENDED_ALERTS = {1: "power_state_change", 2: "power_button_press", 3: "power_button_release",
                    4: "controller_initiated_wake"}


def decode_alert(ado: int) -> dict:
    types = bits(ado, 31, 24)
    extended = bits(ado, 3, 0) if types >> 7 & 1 else None
    return {"alerts": [name for bit, name in _ALERT_TYPES.items() if types >> bit & 1],
            "fixed_batteries": [i for i in range(4) if bits(ado, 23, 20) >> i & 1],
            "hot_swappable_batteries": [i for i in range(4) if bits(ado, 19, 16) >> i & 1],
            "extended_alert": None if extended is None
            else _EXTENDED_ALERTS.get(extended, f"reserved_{extended}")}


_USB_MODES = {0: "USB2", 1: "USB3.2", 2: "USB4"}
_CABLE_SPEEDS = {0: "USB2", 1: "USB3.2_Gen1", 2: "USB3.2_Gen2_or_USB4_Gen2", 3: "USB4_Gen3",
                 4: "USB4_Gen4"}
_CABLE_TYPES = {0: "passive", 1: "active_retimer", 2: "active_redriver", 3: "optically_isolated"}
_CABLE_CURRENTS = {0: "VBUS_not_supported", 1: "reserved", 2: "3A", 3: "5A"}


def decode_enter_usb(eudo: int) -> dict:
    return {"usb_mode": _USB_MODES.get(bits(eudo, 30, 28), "reserved"),
            "usb4_drd": bool(eudo >> 26 & 1), "usb3_drd": bool(eudo >> 25 & 1),
            "cable_speed": _CABLE_SPEEDS.get(bits(eudo, 23, 21), "reserved"),
            "cable_type": _CABLE_TYPES[bits(eudo, 20, 19)],
            "cable_current": _CABLE_CURRENTS[bits(eudo, 18, 17)],
            "pcie_supported": bool(eudo >> 16 & 1), "dp_supported": bool(eudo >> 15 & 1),
            "tbt_supported": bool(eudo >> 14 & 1), "host_present": bool(eudo >> 13 & 1)}


_EPR_ACTIONS = {1: "Enter", 2: "Enter_Acknowledged", 3: "Enter_Succeeded", 4: "Enter_Failed",
                5: "Exit"}


def decode_epr_mode(eprmdo: int) -> dict:
    action = bits(eprmdo, 31, 24)
    return {"action": _EPR_ACTIONS.get(action, f"reserved_{action}"), "data": bits(eprmdo, 23, 16)}


def decode_source_info(sido: int) -> dict:
    return {"port_type": "guaranteed" if sido >> 31 & 1 else "managed",
            "max_pdp_w": bits(sido, 23, 16), "present_pdp_w": bits(sido, 15, 8),
            "reported_pdp_w": bits(sido, 7, 0)}


def decode_revision(rmdo: int) -> dict:
    return {"revision": f"{bits(rmdo, 31, 28)}.{bits(rmdo, 27, 24)}",
            "version": f"{bits(rmdo, 23, 20)}.{bits(rmdo, 19, 16)}"}


def _country(code: int) -> str:
    return bytes([code >> 8, code & 0xFF]).decode("ascii", "replace")


_DATA_OBJECT_DECODERS = {
    "BIST": decode_bist,
    "Battery_Status": decode_battery_status,
    "Alert": decode_alert,
    "Enter_USB": decode_enter_usb,
    "EPR_Mode": decode_epr_mode,
    "Source_Info": decode_source_info,
    "Revision": decode_revision,
    "Get_Country_Info": lambda o: {"country_code": _country(bits(o, 31, 16))},
}


def decode_data_object(message_type: str, obj: int) -> dict | None:
    """Decode the first data object of a PD 3.x data message, or None if not handled here."""
    decoder = _DATA_OBJECT_DECODERS.get(message_type)
    return decoder(obj) if decoder else None
