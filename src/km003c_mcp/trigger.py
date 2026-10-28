"""Protocol trigger over the KM003C virtual serial port (CDC, interface 2).

Commands follow ChargerLab's "KM003C/002C Protocol Trigger by Virtual Serial
Port" sheet: text lines terminated by CRLF, answered with LF-terminated lines.
"""

from __future__ import annotations

import os
import time

import serial
from serial.tools.list_ports import comports

from .usb_device import PID, VID, Km003cError

ENTRY_PROTOCOLS = ("pd", "ufcs", "qc", "fcp", "scp", "afc", "vfcp", "sfcp", "tfcp", "bc",
                   "apple", "list", "list+")
_FIXED_STEPS = {
    "qc": (5000, 9000, 12000, 20000),
    "fcp": (5000, 9000, 12000),
    "afc": (5000, 9000, 12000),
    "sfcp": (5000, 9000, 12000),
}
_VOLT_CUR = ("scp", "vfcp", "tfcp")


def find_trigger_port(serial_number: str | None = None, override: str | None = None) -> str:
    override = override or os.environ.get("KM003C_COM_PORT")
    if override:
        return override
    ports = [p for p in comports() if p.vid == VID and p.pid == PID]
    if serial_number:
        ports = [p for p in ports if p.serial_number == serial_number]
    if not ports:
        raise Km003cError(
            f"no KM003C virtual serial port found (serial {serial_number!r}); set KM003C_COM_PORT")
    if len(ports) > 1:
        raise Km003cError(
            f"multiple KM003C serial ports ({', '.join(p.device for p in ports)}); set KM003C_COM_PORT")
    return ports[0].device


class TriggerPort:
    def __init__(self, port: str, serial_factory=serial.Serial) -> None:
        self.port = port
        # The CDC port ignores the baud rate; a short read timeout keeps the polling loop responsive.
        self._ser = serial_factory(port, 115200, timeout=0.05)
        self.is_open = False

    def command(self, cmd: str, timeout_s: float = 3.0, idle_s: float = 0.5) -> list[str]:
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r\n").encode())
        buf = b""
        start = last = time.monotonic()
        while (now := time.monotonic()) - start < timeout_s:
            chunk = self._ser.read(4096)
            if chunk:
                buf += chunk
                last = now
            elif b"\n" in buf and now - last >= idle_s:
                break
            else:
                time.sleep(0.01)
        text = buf.decode("utf-8", "replace")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def ensure_open(self) -> list[str]:
        if self.is_open:
            return []
        lines = self.command("pdm open")
        self.is_open = True
        return lines

    def dispose(self) -> None:
        """Release the serial handle without talking to the device."""
        try:
            self._ser.close()
        except Exception:  # noqa: BLE001 - the port may already be gone
            pass
        self.is_open = False

    def close(self) -> list[str]:
        try:
            return self.command("pdm close")
        finally:
            self.is_open = False
            self._ser.close()


def pd_request_command(position: int, voltage_mv: int | None = None,
                       current_ma: int | None = None) -> str:
    if not 1 <= position <= 13:
        raise ValueError("PDO position must be 1..13")
    parts = [f"req={position}"]
    if voltage_mv is not None:
        parts.append(f"volt={voltage_mv}")
    if current_ma is not None:
        parts.append(f"cur={current_ma}")
    return "pd " + ",".join(parts)


def fast_charge_command(protocol: str, voltage_mv: int, current_ma: int | None = None,
                        request: int | None = None) -> str:
    protocol = protocol.lower()
    if protocol in _FIXED_STEPS:
        if voltage_mv not in _FIXED_STEPS[protocol]:
            raise ValueError(f"{protocol} supports only {_FIXED_STEPS[protocol]} mV")
        return f"{protocol} {voltage_mv // 1000}V"
    if protocol == "qc3":
        if not 3600 <= voltage_mv <= 20000 or (voltage_mv - 3600) % 200:
            raise ValueError("qc3 voltage must be 3600..20000 mV in 200 mV steps")
        return f"qc3 volt={voltage_mv}"
    if protocol in _VOLT_CUR:
        if current_ma is None:
            raise ValueError(f"{protocol} needs current_ma")
        return f"{protocol} volt={voltage_mv},cur={current_ma}"
    if protocol == "ufcs":
        if current_ma is None or request is None:
            raise ValueError("ufcs needs request and current_ma")
        return f"ufcs req={request},volt={voltage_mv},cur={current_ma}"
    raise ValueError(f"unsupported protocol {protocol!r}")


def entry_command(protocol: str) -> str:
    protocol = protocol.lower()
    if protocol not in ENTRY_PROTOCOLS:
        raise ValueError(f"protocol must be one of {', '.join(ENTRY_PROTOCOLS)}")
    return f"entry {protocol}"
