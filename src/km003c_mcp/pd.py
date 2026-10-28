"""Minimal USB Power Delivery message decoder (header, message names, PDOs, RDOs)."""

from __future__ import annotations

from .protocol import PdEvent

SOP_NAMES = {0: "SOP", 1: "SOP'", 2: "SOP''", 3: "SOP'_Debug", 4: "SOP''_Debug"}
SPEC_REVISIONS = {0: "1.0", 1: "2.0", 2: "3.x"}

CONTROL_TYPES = {
    1: "GoodCRC", 2: "GotoMin", 3: "Accept", 4: "Reject", 5: "Ping", 6: "PS_RDY",
    7: "Get_Source_Cap", 8: "Get_Sink_Cap", 9: "DR_Swap", 10: "PR_Swap", 11: "VCONN_Swap",
    12: "Wait", 13: "Soft_Reset", 14: "Data_Reset", 15: "Data_Reset_Complete",
    16: "Not_Supported", 17: "Get_Source_Cap_Extended", 18: "Get_Status", 19: "FR_Swap",
    20: "Get_PPS_Status", 21: "Get_Country_Codes", 22: "Get_Sink_Cap_Extended",
    23: "Get_Source_Info", 24: "Get_Revision",
}
DATA_TYPES = {
    1: "Source_Capabilities", 2: "Request", 3: "BIST", 4: "Sink_Capabilities",
    5: "Battery_Status", 6: "Alert", 7: "Get_Country_Info", 8: "Enter_USB", 9: "EPR_Request",
    10: "EPR_Mode", 11: "Source_Info", 12: "Revision", 15: "Vendor_Defined",
}
EXTENDED_TYPES = {
    1: "Source_Capabilities_Extended", 2: "Status", 3: "Get_Battery_Cap",
    4: "Get_Battery_Status", 5: "Battery_Capabilities", 6: "Get_Manufacturer_Info",
    7: "Manufacturer_Info", 8: "Security_Request", 9: "Security_Response",
    10: "Firmware_Update_Request", 11: "Firmware_Update_Response", 12: "PPS_Status",
    13: "Country_Info", 14: "Country_Codes", 15: "Sink_Capabilities_Extended",
    16: "Extended_Control", 17: "EPR_Source_Capabilities", 18: "EPR_Sink_Capabilities",
    19: "Vendor_Defined_Extended",
}
_CAPS_TYPES = {"Source_Capabilities", "Sink_Capabilities"}
_REQUEST_TYPES = {"Request", "EPR_Request"}
_PROGRAMMABLE = {"pps", "epr_avs", "spr_avs"}


def _bits(value: int, high: int, low: int) -> int:
    return (value >> low) & ((1 << (high - low + 1)) - 1)


def _r(x: float) -> float:
    return round(x, 3)


def decode_pdo(pdo: int) -> dict:
    kind = _bits(pdo, 31, 30)
    if kind == 0:
        return {"type": "fixed", "voltage_v": _r(_bits(pdo, 19, 10) * 0.05),
                "max_current_a": _r(_bits(pdo, 9, 0) * 0.01)}
    if kind == 1:
        return {"type": "battery", "max_voltage_v": _r(_bits(pdo, 29, 20) * 0.05),
                "min_voltage_v": _r(_bits(pdo, 19, 10) * 0.05),
                "max_power_w": _r(_bits(pdo, 9, 0) * 0.25)}
    if kind == 2:
        return {"type": "variable", "max_voltage_v": _r(_bits(pdo, 29, 20) * 0.05),
                "min_voltage_v": _r(_bits(pdo, 19, 10) * 0.05),
                "max_current_a": _r(_bits(pdo, 9, 0) * 0.01)}
    apdo = _bits(pdo, 29, 28)
    if apdo == 0:
        return {"type": "pps", "max_voltage_v": _r(_bits(pdo, 24, 17) * 0.1),
                "min_voltage_v": _r(_bits(pdo, 15, 8) * 0.1),
                "max_current_a": _r(_bits(pdo, 6, 0) * 0.05)}
    if apdo == 1:
        return {"type": "epr_avs", "max_voltage_v": _r(_bits(pdo, 25, 17) * 0.1),
                "min_voltage_v": _r(_bits(pdo, 15, 8) * 0.1), "pdp_w": _bits(pdo, 7, 0)}
    if apdo == 2:
        return {"type": "spr_avs", "max_current_15v_a": _r(_bits(pdo, 19, 10) * 0.01),
                "max_current_20v_a": _r(_bits(pdo, 9, 0) * 0.01)}
    return {"type": "reserved_apdo", "raw": f"{pdo:08x}"}


def decode_request(rdo: int, source_pdos: list[dict] | None = None) -> dict:
    position = _bits(rdo, 31, 28)
    pdo = None
    if source_pdos and 1 <= position <= len(source_pdos):
        pdo = source_pdos[position - 1]
    if pdo is not None and pdo["type"] in _PROGRAMMABLE:
        return {"object_position": position,
                "output_voltage_v": _r(_bits(rdo, 20, 9) * 0.02),
                "operating_current_a": _r(_bits(rdo, 6, 0) * 0.05)}
    return {"object_position": position,
            "operating_current_a": _r(_bits(rdo, 19, 10) * 0.01),
            "max_current_a": _r(_bits(rdo, 9, 0) * 0.01)}


def decode_message(wire: bytes, sop: int, source_pdos: list[dict] | None = None) -> dict:
    if len(wire) < 2:
        return {"error": "PD message shorter than its 2-byte header", "raw": wire.hex()}
    header = int.from_bytes(wire[:2], "little")
    msg_type = _bits(header, 4, 0)
    num_objects = _bits(header, 14, 12)
    extended = bool(header >> 15)
    if extended:
        name = EXTENDED_TYPES.get(msg_type)
    elif num_objects == 0:
        name = CONTROL_TYPES.get(msg_type)
    else:
        name = DATA_TYPES.get(msg_type)

    out: dict = {
        "sop": SOP_NAMES.get(sop, f"SOP?{sop}"),
        "message_id": _bits(header, 11, 9),
        "spec_revision": SPEC_REVISIONS.get(_bits(header, 7, 6), "reserved"),
    }
    if sop == 0:
        out["power_role"] = "source" if header & (1 << 8) else "sink"
        out["data_role"] = "dfp" if header & (1 << 5) else "ufp"
    out.update({"extended": extended, "type": name or f"Reserved_{msg_type}",
                "num_objects": num_objects})

    body_end = 2 + 4 * num_objects
    objects = [int.from_bytes(wire[i:i + 4], "little")
               for i in range(2, min(body_end, len(wire)) - 3, 4)]
    out["objects"] = [f"{o:08x}" for o in objects]
    if not extended:
        if name in _CAPS_TYPES:
            out["pdos"] = [{"position": i + 1, **decode_pdo(o)} for i, o in enumerate(objects)]
        elif name in _REQUEST_TYPES and objects:
            out["request"] = decode_request(objects[0], source_pdos)
    if len(wire) > body_end:
        out["trailing"] = wire[body_end:].hex()
    return out


class PdDecoder:
    """Stateful decoder that resolves Requests against the last Source_Capabilities."""

    def __init__(self) -> None:
        self.source_pdos: list[dict] | None = None

    def decode(self, event: PdEvent) -> dict:
        if event.kind != "message":
            return {"event": event.kind}
        msg = decode_message(event.wire, event.sop if event.sop is not None else 0,
                             self.source_pdos)
        if msg.get("type") == "Source_Capabilities":
            self.source_pdos = msg["pdos"]
        return msg
