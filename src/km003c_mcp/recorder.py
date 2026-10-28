"""Background poller for AdcQueue streaming and PD event capture.

A single daemon thread polls the device while a stream and/or a PD capture is
active, combining both attributes into one GetData request per cycle.
"""

from __future__ import annotations

import csv
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable

from .pd import PdDecoder
from .protocol import (
    ATT_ADCQUEUE, ATT_PD, GRAPH_RATES_SPS, AdcQueueSample, PdEventStream, parse_adcqueue,
    parse_pd, sequence_step,
)
from .usb_device import Km003cDevice, Km003cError

CSV_HEADER = ["sequence", "marker", "vbus_v", "ibus_a", "power_w", "cc1_v", "cc2_v", "dp_v", "dm_v"]
RECENT_SAMPLES = 20
# A reply carries at most 63 queued samples and older ones are lost, so at 1000 SPS the
# queue must be drained well within 63 ms.
FAST_POLL_INTERVAL_S = 0.02


@dataclass
class StreamStats:
    rate_sps: int = 0
    samples: int = 0
    dropped_samples: int = 0
    duration_s: float = 0.0
    vbus_min_v: float | None = None
    vbus_max_v: float | None = None
    vbus_mean_v: float | None = None
    ibus_min_a: float | None = None
    ibus_max_a: float | None = None
    ibus_mean_a: float | None = None
    power_max_w: float | None = None
    power_mean_w: float | None = None
    energy_wh: float = 0.0
    charge_mah: float = 0.0
    csv_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _sample_dict(s: AdcQueueSample) -> dict:
    return {"sequence": s.sequence, "marker": s.marker, "vbus_v": s.vbus_v, "ibus_a": s.ibus_a,
            "power_w": s.vbus_v * s.ibus_a, "cc1_v": s.cc1_v, "cc2_v": s.cc2_v,
            "dp_v": s.dp_v, "dm_v": s.dm_v}


class _Stream:
    """Accumulates statistics for one AdcQueue stream."""

    def __init__(self, rate_index: int, csv_path: str | None, keep: int) -> None:
        self.rate_index = rate_index
        self.step = sequence_step(rate_index)
        self.dt = self.step / 1000
        self.stats = StreamStats(rate_sps=GRAPH_RATES_SPS[rate_index], csv_path=csv_path)
        self.samples: deque[dict] = deque(maxlen=keep)
        self.warming = True
        self.pending: AdcQueueSample | None = None
        self.last_seq: int | None = None
        self._sums = [0.0, 0.0, 0.0]
        self._csv_file = open(csv_path, "w", newline="") if csv_path else None
        self._csv = csv.writer(self._csv_file) if self._csv_file else None
        if self._csv:
            self._csv.writerow(CSV_HEADER)

    def feed(self, samples: list[AdcQueueSample]) -> None:
        for s in samples:
            if self.warming:
                # After StartGraph the device may still deliver samples taken at the previous
                # rate; start counting once two consecutive samples show the expected step.
                if self.pending is not None and (s.sequence - self.pending.sequence) % 65536 == self.step:
                    self.warming = False
                    self._add(self.pending)
                    self._add(s)
                else:
                    self.pending = s
                continue
            self._add(s)

    def _add(self, s: AdcQueueSample) -> None:
        st = self.stats
        if self.last_seq is not None:
            diff = (s.sequence - self.last_seq) % 65536
            if diff > self.step:
                st.dropped_samples += diff // self.step - 1
        self.last_seq = s.sequence
        row = _sample_dict(s)
        p = row["power_w"]
        st.samples += 1
        st.vbus_min_v = s.vbus_v if st.vbus_min_v is None else min(st.vbus_min_v, s.vbus_v)
        st.vbus_max_v = s.vbus_v if st.vbus_max_v is None else max(st.vbus_max_v, s.vbus_v)
        st.ibus_min_a = s.ibus_a if st.ibus_min_a is None else min(st.ibus_min_a, s.ibus_a)
        st.ibus_max_a = s.ibus_a if st.ibus_max_a is None else max(st.ibus_max_a, s.ibus_a)
        st.power_max_w = p if st.power_max_w is None else max(st.power_max_w, p)
        self._sums[0] += s.vbus_v
        self._sums[1] += s.ibus_a
        self._sums[2] += p
        st.vbus_mean_v = self._sums[0] / st.samples
        st.ibus_mean_a = self._sums[1] / st.samples
        st.power_mean_w = self._sums[2] / st.samples
        st.energy_wh += p * self.dt / 3600
        st.charge_mah += s.ibus_a * self.dt / 3.6
        st.duration_s = (st.samples + st.dropped_samples) * self.dt
        self.samples.append(row)
        if self._csv:
            self._csv.writerow([row[k] for k in CSV_HEADER])

    def close(self) -> None:
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv = None


class Recorder:
    def __init__(self, device_provider: Callable[[], Km003cDevice], poll_interval_s: float = 0.05,
                 keep_samples: int = 10_000) -> None:
        self._provider = device_provider
        self._poll = poll_interval_s
        self._keep = keep_samples
        self._state = threading.Lock()
        self._thread: threading.Thread | None = None
        self._device: Km003cDevice | None = None
        self._stream: _Stream | None = None
        self._last_stats: StreamStats | None = None
        self._stream_error: str | None = None
        self._pd_active = False
        self._pd_error: str | None = None
        self._pd_events: list[dict] = []
        self._pd_decoder = PdDecoder()

    # --- lifecycle -------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="km003c-poller", daemon=True)
            self._thread.start()

    def _join_if_idle(self) -> None:
        thread = self._thread
        if thread is not None and self._stream is None and not self._pd_active:
            thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._state:
                mask = (ATT_ADCQUEUE if self._stream else 0) | (ATT_PD if self._pd_active else 0)
                device = self._device
                poll = self._poll
                if self._stream and self._stream.rate_index == 3:
                    poll = min(poll, FAST_POLL_INTERVAL_S)
            if not mask or device is None:
                return
            try:
                packets = device.get_data(mask)
                with self._state:
                    for packet in packets:
                        if packet.attribute == ATT_ADCQUEUE and self._stream:
                            self._stream.feed(parse_adcqueue(packet.payload, self._stream.rate_index))
                        elif packet.attribute == ATT_PD and self._pd_active:
                            self._ingest_pd(packet.payload)
            except Exception as exc:  # noqa: BLE001 - any failure ends the capture and is reported
                self._fail(exc)
                return
            time.sleep(poll)

    def _fail(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        with self._state:
            if self._stream:
                self._stream.close()
                self._last_stats = self._stream.stats
                self._stream = None
                self._stream_error = message
            if self._pd_active:
                self._pd_active = False
                self._pd_error = message

    # --- AdcQueue stream -------------------------------------------------------------

    def start_stream(self, rate_index: int, csv_path: str | None = None) -> None:
        if rate_index not in GRAPH_RATES_SPS:
            raise ValueError(f"rate index must be one of {sorted(GRAPH_RATES_SPS)}")
        with self._state:
            if self._stream is not None:
                raise Km003cError("a stream is already running; stop it first")
        device = self._provider()
        device.start_graph(rate_index)
        with self._state:
            self._device = device
            self._stream = _Stream(rate_index, csv_path, self._keep)
            self._stream_error = None
        self._ensure_thread()

    def stop_stream(self) -> StreamStats:
        with self._state:
            stream, self._stream = self._stream, None
            device = self._device
        if stream is None:
            if self._last_stats is None:
                raise Km003cError("no stream is running")
            return self._last_stats
        stream.close()
        self._last_stats = stream.stats
        self._join_if_idle()
        if device is not None:
            device.stop_graph()
        return stream.stats

    def stream_status(self) -> dict:
        with self._state:
            stream = self._stream
            stats = stream.stats if stream else (self._last_stats or StreamStats())
            recent = list(stream.samples)[-RECENT_SAMPLES:] if stream else []
            return {"running": stream is not None, "error": self._stream_error,
                    "stats": stats.to_dict(), "recent": recent}

    def stream_samples(self, max_samples: int = 500) -> list[dict]:
        with self._state:
            samples = list(self._stream.samples) if self._stream else []
        if max_samples <= 0 or len(samples) <= max_samples:
            return samples
        step = len(samples) / max_samples
        return [samples[int(i * step)] for i in range(max_samples)]

    # --- PD capture ------------------------------------------------------------------

    def _ingest_pd(self, payload: bytes) -> None:
        parsed = parse_pd(payload)
        if not isinstance(parsed, PdEventStream):
            return
        for event in parsed.events:
            self._pd_events.append({
                "index": len(self._pd_events),
                "timestamp_ms": event.timestamp_ms,
                "kind": event.kind,
                "sop": event.sop,
                "wire_hex": event.wire.hex(),
                "decoded": self._pd_decoder.decode(event),
            })

    def start_pd(self) -> None:
        device = self._provider()
        with self._state:
            self._device = device
            self._pd_active = True
            self._pd_error = None
            self._pd_events = []
            self._pd_decoder = PdDecoder()
        self._ensure_thread()

    def stop_pd(self) -> None:
        with self._state:
            self._pd_active = False
        self._join_if_idle()

    def pd_events(self, since: int = 0, limit: int = 100, decode: bool = True) -> dict:
        with self._state:
            selected = self._pd_events[max(since, 0):max(since, 0) + max(limit, 0)]
            events = [e if decode else {k: v for k, v in e.items() if k != "decoded"}
                      for e in selected]
            return {"events": events, "next": max(since, 0) + len(selected),
                    "total": len(self._pd_events), "running": self._pd_active,
                    "error": self._pd_error}
