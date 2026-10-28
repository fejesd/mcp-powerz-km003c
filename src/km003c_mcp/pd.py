"""USB Power Delivery message decoder (USB PD R3.1 message set)."""

from __future__ import annotations

from .pd_objects import bits as _bits
from .pd_objects import decode_data_object, decode_pdo, decode_request
from .pd_extended import (
    chunk_data, decode_extended_header, decode_extended_payload, is_complete_in_one_chunk,
)
from .pd_vdm import decode_vdm
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
    elif sop in (1, 2):
        out["cable_plug"] = bool(header & (1 << 8))
    out.update({"extended": extended, "type": name or f"Reserved_{msg_type}",
                "num_objects": num_objects})

    body_end = 2 + 4 * num_objects
    if extended:
        _decode_extended(out, wire[:body_end], out["type"])
    else:
        objects = [int.from_bytes(wire[i:i + 4], "little")
                   for i in range(2, min(body_end, len(wire)) - 3, 4)]
        out["objects"] = [f"{o:08x}" for o in objects]
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
            out["vdm"] = decode_vdm(objects, sop)
        elif objects and (data := decode_data_object(name, objects[0])) is not None:
            out["data"] = data
    if len(wire) > body_end:
        out["trailing"] = wire[body_end:].hex()
    return out


def _decode_extended(out: dict, wire: bytes, name: str) -> None:
    if len(wire) < 4:
        out["extended_header"] = None
        out["note"] = "no extended payload captured (header only)"
        return
    ext = decode_extended_header(int.from_bytes(wire[2:4], "little"))
    data = chunk_data(wire, ext)
    out["extended_header"] = ext
    out["data_hex"] = data.hex()
    if not ext["request_chunk"] and is_complete_in_one_chunk(ext):
        payload = decode_extended_payload(name, data[:ext["data_size"]])
        if payload is not None:
            out["payload"] = payload


class PdDecoder:
    """Stateful decoder: reassembles chunked extended messages and resolves Requests
    against the last (EPR_)Source_Capabilities."""

    def __init__(self) -> None:
        self.source_pdos: list[dict] | None = None
        self._chunks: dict[tuple, bytearray] = {}

    def _reassemble(self, msg: dict) -> None:
        ext = msg.get("extended_header")
        if not ext or ext["request_chunk"] or is_complete_in_one_chunk(ext):
            return
        key = (msg["sop"], msg["type"])
        if ext["chunk_number"] == 0:
            self._chunks[key] = bytearray()
        buf = self._chunks.get(key)
        if buf is None:
            return
        buf += bytes.fromhex(msg["data_hex"])
        if len(buf) < ext["data_size"]:
            msg["partial"] = True
            return
        del self._chunks[key]
        msg["reassembled"] = True
        payload = decode_extended_payload(msg["type"], bytes(buf[:ext["data_size"]]))
        if payload is not None:
            msg["payload"] = payload

    def decode(self, event: PdEvent) -> dict:
        if event.kind != "message":
            return {"event": event.kind}
        msg = decode_message(event.wire, event.sop if event.sop is not None else 0,
                             self.source_pdos)
        self._reassemble(msg)
        if msg.get("type") == "Source_Capabilities":
            self.source_pdos = msg["pdos"]
        elif msg.get("type") == "EPR_Source_Capabilities" and "payload" in msg:
            self.source_pdos = msg["payload"]["pdos"]
        return msg
