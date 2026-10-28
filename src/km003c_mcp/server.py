"""MCP server exposing the ChargerLab POWER-Z KM003C as tools (stdio transport)."""

from __future__ import annotations

import atexit
import functools
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import Callable, Literal

import serial
from mcp.server.mcpserver import MCPServer

from .protocol import PdEventStream
from .recorder import Recorder
from .trigger import (
    TriggerPort, entry_command, fast_charge_command, find_trigger_port, pd_request_command,
)
from .usb_device import Km003cDevice, Km003cError, PyUsbTransport, TransportError

RATE_INDEX = {2: 0, 10: 1, 50: 2, 1000: 3}
VBUS_WARNING = ("Makes the attached charger raise VBUS on the meter's output; "
                "disconnect sensitive loads first.")


class Km003cService:
    """Owns the lazily opened USB session, the trigger port and the background recorder."""

    def __init__(self, transport_factory: Callable = PyUsbTransport.open,
                 trigger_factory: Callable[[str], TriggerPort] = TriggerPort,
                 port_finder: Callable = find_trigger_port) -> None:
        self._transport_factory = transport_factory
        self._trigger_factory = trigger_factory
        self._port_finder = port_finder
        self._lock = threading.RLock()
        self._device: Km003cDevice | None = None
        self._trigger: TriggerPort | None = None
        self.recorder = Recorder(self.device, on_transport_error=self.invalidate_device)

    def device(self) -> Km003cDevice:
        with self._lock:
            if self._device is None:
                transport = self._transport_factory()
                device = Km003cDevice(transport)
                try:
                    device.connect()
                    # A previous session may have left the firmware streaming; Reject is fine.
                    device.stop_graph()
                except Km003cError:
                    transport.close()
                    raise
                self._device = device
            return self._device

    def invalidate_device(self) -> None:
        """Drop a broken USB session; the next call opens a fresh one."""
        with self._lock:
            device, self._device = self._device, None
        if device is None:
            return
        self.recorder.abort("USB session reset after a transport error")
        with device.lock:
            try:
                device.transport.close()
            except Exception:  # noqa: BLE001 - the link is already broken
                pass

    def usb_serial(self) -> str | None:
        try:
            return self.device().serial_number or None
        except Km003cError:
            return None

    def trigger(self) -> TriggerPort:
        with self._lock:
            if self._trigger is None:
                self._trigger = self._trigger_factory(self._port_finder(self.usb_serial()))
            return self._trigger

    def invalidate_trigger(self) -> None:
        with self._lock:
            trigger, self._trigger = self._trigger, None
        if trigger is not None:
            trigger.dispose()

    def shutdown(self) -> None:
        """Best-effort cleanup at process exit."""
        steps = [self.recorder.stop_pd]
        if self.recorder.stream_status()["running"]:
            steps.append(self.recorder.stop_stream)
        if self._trigger is not None:
            steps.append(self._trigger.close)
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            device, self._device = self._device, None
        if device is not None:
            device.close()


def build_tools(service: Km003cService) -> SimpleNamespace:
    """Create the tool callables; errors come back as {"error", "kind"} dicts."""

    def safe(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except TransportError as exc:
                service.invalidate_device()
                return {"error": str(exc), "kind": type(exc).__name__}
            except serial.SerialException as exc:
                service.invalidate_trigger()
                return {"error": str(exc), "kind": type(exc).__name__}
            except (Km003cError, ValueError, OSError) as exc:
                return {"error": str(exc), "kind": type(exc).__name__}
        return wrapper

    def trigger_run(command: str, timeout_s: float = 3.0) -> dict:
        port = service.trigger()
        port.ensure_open()
        return {"command": command, "lines": port.command(command, timeout_s=timeout_s)}

    @safe
    def device_info() -> dict:
        """Identify the attached KM003C: model, hardware/firmware version, serial, trigger port."""
        device = service.device()
        info = asdict(device.device_info())
        info["usb_serial"] = device.serial_number
        try:
            info["trigger_port"] = service._port_finder(device.serial_number)
        except Km003cError:
            info["trigger_port"] = None
        return info

    @safe
    def measure(samples: int = 1, interval_ms: int = 100) -> dict:
        """Read VBUS/IBUS/power, CC1/CC2, D+/D-, VDD and temperature (V, A, W, °C).

        With samples > 1 the readings are taken interval_ms apart and mean/min/max are
        returned for vbus_v, ibus_a and power_w. Positive current flows from the meter's
        USB-C receptacle (input) to its plug (output).
        """
        if not 1 <= samples <= 100:
            raise ValueError("samples must be 1..100")
        readings = []
        for i in range(samples):
            if i:
                time.sleep(interval_ms / 1000)
            readings.append(asdict(service.device().read_adc()))
        if samples == 1:
            return readings[0]
        out: dict = {"samples": samples}
        for key in ("vbus_v", "ibus_a", "power_w"):
            values = [r[key] for r in readings]
            out[key] = {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}
        out["last"] = readings[-1]
        return out

    @safe
    def pd_status() -> dict:
        """Read the PD-side status snapshot (timestamp, VBUS, IBUS, CC1, CC2).

        Not available while a PD capture runs, because reading would consume captured events.
        """
        if service.recorder.pd_events(limit=0)["running"]:
            raise Km003cError("a PD capture is running; use pd_capture_read instead")
        result = service.device().read_pd()
        if isinstance(result, PdEventStream):
            out = asdict(result.preamble)
            out["discarded_events"] = len(result.events)
            return out
        return asdict(result)

    @safe
    def stream_start(rate_sps: Literal[2, 10, 50, 1000] = 1000, csv_path: str | None = None) -> dict:
        """Start high-rate VBUS/IBUS sampling in the background (2, 10, 50 or 1000 samples/s).

        Optionally writes every sample to csv_path. Use stream_status / stream_samples while
        running and stream_stop for the final statistics (energy, charge, min/max/mean).
        """
        if rate_sps not in RATE_INDEX:
            raise ValueError(f"rate_sps must be one of {sorted(RATE_INDEX)}")
        service.recorder.start_stream(RATE_INDEX[rate_sps], csv_path)
        return {"started": True, "rate_sps": rate_sps, "csv_path": csv_path}

    @safe
    def stream_status() -> dict:
        """Running state, error (if any), live statistics and the most recent samples."""
        return service.recorder.stream_status()

    @safe
    def stream_samples(max_samples: int = 500) -> dict:
        """Return up to max_samples evenly spaced samples from the running stream's buffer."""
        return {"samples": service.recorder.stream_samples(max_samples)}

    @safe
    def stream_stop() -> dict:
        """Stop the stream and return its statistics."""
        return service.recorder.stop_stream().to_dict()

    @safe
    def pd_capture_start() -> dict:
        """Start capturing USB PD traffic (messages, attach/detach) in the background."""
        service.recorder.start_pd()
        return {"started": True}

    @safe
    def pd_capture_read(since: int = 0, limit: int = 100, decode: bool = True) -> dict:
        """Page through captured PD events from index `since`; `next` is the next index to ask."""
        return service.recorder.pd_events(since, limit, decode)

    @safe
    def pd_capture_stop() -> dict:
        """Stop the PD capture; captured events stay readable until the next start."""
        service.recorder.stop_pd()
        return {"stopped": True, "total": service.recorder.pd_events(limit=0)["total"]}

    @safe
    def trigger_detect(extended: bool = False, timeout_s: float = 15) -> dict:
        """Detect the fast-charge protocols the attached charger supports (`entry list`).

        extended=True also probes VOOC/MTK PE (`entry list+`), which takes much longer.
        """
        return trigger_run("entry list+" if extended else "entry list", timeout_s)

    @safe
    def trigger_enter(protocol: str, timeout_s: float = 5) -> dict:
        """Enter a trigger protocol: pd, ufcs, qc, fcp, scp, afc, vfcp, sfcp, tfcp, bc, apple."""
        return trigger_run(entry_command(protocol), timeout_s)

    @safe
    def trigger_pd_source_caps() -> dict:
        """Ask the charger for its PD Source_Capabilities (`pd pdo`)."""
        return trigger_run("pd pdo")

    @safe
    def trigger_pd_request(position: int, voltage_mv: int | None = None,
                           current_ma: int | None = None) -> dict:
        """Request PDO `position` (1-based); voltage_mv is needed for PPS/AVS PDOs."""
        return trigger_run(pd_request_command(position, voltage_mv, current_ma))

    @safe
    def trigger_fast_charge(protocol: str, voltage_mv: int, current_ma: int | None = None,
                            request: int | None = None) -> dict:
        """Request a voltage via qc, qc3, fcp, afc, sfcp, scp, vfcp, tfcp or ufcs."""
        return trigger_run(fast_charge_command(protocol, voltage_mv, current_ma, request))

    @safe
    def trigger_reset() -> dict:
        """Reset the protocol trigger to its initial state (re-opened on the next trigger call)."""
        port = service.trigger()
        lines = port.command("reset")
        port.is_open = False
        return {"command": "reset", "lines": lines}

    @safe
    def trigger_close() -> dict:
        """Leave protocol trigger mode (`pdm close`)."""
        lines = service.trigger().close()
        service.invalidate_trigger()
        return {"command": "pdm close", "lines": lines}

    @safe
    def trigger_raw(command: str, timeout_s: float = 3) -> dict:
        """Send any trigger command from ChargerLab's virtual serial port sheet verbatim."""
        return trigger_run(command, timeout_s)

    for fn in (trigger_pd_request, trigger_fast_charge, trigger_raw):
        fn.__doc__ = f"{fn.__doc__}\n\n{VBUS_WARNING}"

    return SimpleNamespace(**{fn.__name__: fn for fn in (
        device_info, measure, pd_status, stream_start, stream_status, stream_samples,
        stream_stop, pd_capture_start, pd_capture_read, pd_capture_stop, trigger_detect,
        trigger_enter, trigger_pd_source_caps, trigger_pd_request, trigger_fast_charge,
        trigger_reset, trigger_close, trigger_raw)})


service = Km003cService()
tools = build_tools(service)
mcp = MCPServer(
    "km003c",
    instructions="Tools for the ChargerLab POWER-Z KM003C USB-C power meter: measurements, "
                 "high-rate streaming, USB PD capture/decoding and fast-charge protocol triggering.",
)
for _tool in vars(tools).values():
    mcp.tool()(_tool)


def main() -> None:
    atexit.register(service.shutdown)
    mcp.run()
