"""USB Power Delivery message decoder (USB PD R3.1 message set)."""

from __future__ import annotations

from .pd_objects import bits as _bits
from .pd_objects import decode_data_object, decode_pdo, decode_request
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
_REQUEST_TYPES = {"Request", "EPR_Request"}


SVID_NAMES = {0xFF00: "PD_SID", 0xFF01: "DisplayPort", 0x8087: "Intel"}
VDM_COMMANDS = {1: "Discover_Identity", 2: "Discover_SVIDs", 3: "Discover_Modes", 4: "Enter_Mode",
                5: "Exit_Mode", 6: "Attention", 16: "DP_Status_Update", 17: "DP_Configure"}
VDM_COMMAND_TYPES = {0: "REQ", 1: "ACK", 2: "NAK", 3: "BUSY"}
DP_PORT_CAPABILITY = {0: "reserved", 1: "UFP_D", 2: "DFP_D", 3: "UFP_D_and_DFP_D"}
DP_CONNECTED = {0: "none", 1: "DFP_D", 2: "UFP_D", 3: "both"}
DP_CONFIGURATION = {0: "USB", 1: "UFP_U_as_DFP_D", 2: "UFP_U_as_UFP_D", 3: "reserved"}
SVID_DP = 0xFF01


def _pins(mask: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(6) if mask & (1 << i)]


def _dp_capabilities(vdo: int) -> dict:
    return {"port_capability": DP_PORT_CAPABILITY[_bits(vdo, 1, 0)], "dp13": bool(vdo & 1 << 2),
            "gen2": bool(vdo & 1 << 3), "receptacle": bool(vdo & 1 << 6),
            "dfp_d_pins": _pins(_bits(vdo, 15, 8)), "ufp_d_pins": _pins(_bits(vdo, 23, 16))}


def _dp_status(vdo: int) -> dict:
    return {"connected": DP_CONNECTED[_bits(vdo, 1, 0)], "power_low": bool(vdo & 1 << 2),
            "enabled": bool(vdo & 1 << 3), "multi_function_preferred": bool(vdo & 1 << 4),
            "usb_config_request": bool(vdo & 1 << 5), "exit_request": bool(vdo & 1 << 6),
            "hpd": bool(vdo & 1 << 7), "irq_hpd": bool(vdo & 1 << 8)}


def _dp_configuration(vdo: int) -> dict:
    return {"configuration": DP_CONFIGURATION[_bits(vdo, 1, 0)], "signaling": _bits(vdo, 5, 2),
            "pin_assignment": _pins(_bits(vdo, 15, 8))}


def decode_vdm(objects: list[int]) -> dict:
    """Decode a Vendor_Defined message: VDM header plus Discover_SVIDs and DisplayPort VDOs."""
    header, vdos = objects[0], objects[1:]
    svid = header >> 16
    out: dict = {"svid": f"0x{svid:04x}", "svid_name": SVID_NAMES.get(svid, "unknown"),
                 "structured": bool(header & 1 << 15)}
    if not out["structured"]:
        return out
    command = _bits(header, 4, 0)
    ctype = VDM_COMMAND_TYPES[_bits(header, 7, 6)]
    out.update({"command": VDM_COMMANDS.get(command, f"SVID_Specific_{command}"),
                "command_type": ctype, "object_position": _bits(header, 10, 8),
                "version": f"{_bits(header, 14, 13) + 1}.{_bits(header, 12, 11)}"})
    if command == 2 and ctype == "ACK":
        out["svids"] = [f"0x{v:04x}" for vdo in vdos for v in (vdo >> 16, vdo & 0xFFFF) if v]
    elif svid == SVID_DP and vdos:
        if command == 3 and ctype == "ACK":
            out["dp_capabilities"] = [_dp_capabilities(v) for v in vdos]
        elif command in (6, 16):
            out["dp_status"] = _dp_status(vdos[0])
        elif command == 17:
            out["dp_configuration"] = _dp_configuration(vdos[0])
    return out


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
        if name in ("Source_Capabilities", "Sink_Capabilities"):
            # The role comes from the message type: a dual-role port may send either.
            role = "source" if name == "Source_Capabilities" else "sink"
            out["pdos"] = [{"position": i + 1, **decode_pdo(o, role)} for i, o in enumerate(objects)]
        elif name in _REQUEST_TYPES and objects:
            pdos = source_pdos
            if name == "EPR_Request" and len(objects) > 1:
                # An EPR_Request carries a copy of the requested PDO as its second object.
                out["requested_pdo"] = decode_pdo(objects[1])
                pdos = [{"type": None}] * (_bits(objects[0], 31, 28) - 1) + [out["requested_pdo"]]
            out["request"] = decode_request(objects[0], pdos)
        elif name == "Vendor_Defined" and objects:
            out["vdm"] = decode_vdm(objects)
        elif objects and (data := decode_data_object(name, objects[0])) is not None:
            out["data"] = data
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
