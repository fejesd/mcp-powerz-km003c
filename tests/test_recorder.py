import struct
import threading
import time

import pytest

from km003c_mcp.protocol import ATT_ADCQUEUE, ATT_PD, LogicalPacket
from km003c_mcp.recorder import Recorder
from km003c_mcp.usb_device import Km003cError
from test_protocol import PD_STREAM


class FakeDevice:
    def __init__(self, replies=(), error=None):
        self.replies = list(replies)
        self.error = error
        self.masks = []
        self.graph = []
        self.lock = threading.RLock()

    def get_data(self, mask):
        self.masks.append(mask)
        if self.replies:
            return self.replies.pop(0)
        if self.error:
            raise self.error
        return []

    def start_graph(self, rate):
        self.graph.append(("start", rate))

    def stop_graph(self):
        self.graph.append(("stop",))


def queue(seqs, uv=10_000_000, ua=1_000_000):
    payload = b"".join(struct.pack("<HHiiHHHH", s, 3, uv, ua, 0, 0, 0, 0) for s in seqs)
    return [LogicalPacket(ATT_ADCQUEUE, False, len(seqs), payload)]


def wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


def test_stream_stats_energy_and_drops():
    dev = FakeDevice([queue([0, 1, 2, 5])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 4)
    st = rec.stop_stream()
    assert dev.graph == [("start", 3), ("stop",)]
    assert st.dropped_samples == 2 and st.vbus_mean_v == pytest.approx(10.0)
    assert st.power_max_w == pytest.approx(10.0)
    assert st.energy_wh == pytest.approx(4 * 10 * 0.001 / 3600)
    assert st.charge_mah == pytest.approx(4 * 1 * 0.001 / 3.6)


def test_stream_warmup_discards_previous_rate_samples():
    # Rate 2 (50 SPS, step 20): leftover 1 ms-step samples must be ignored.
    dev = FakeDevice([queue([100, 101, 102]), queue([120, 140, 160])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(2)
    wait_until(lambda: len(dev.masks) >= 3)
    st = rec.stop_stream()
    assert st.samples == 3 and st.dropped_samples == 0


def test_stream_writes_csv(tmp_path):
    dev = FakeDevice([queue([0, 1])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3, csv_path=str(tmp_path / "s.csv"))
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 2)
    assert rec.stop_stream().csv_path == str(tmp_path / "s.csv")
    lines = (tmp_path / "s.csv").read_text().splitlines()
    assert lines[0] == "sequence,marker,vbus_v,ibus_a,power_w,cc1_v,cc2_v,dp_v,dm_v"
    assert len(lines) == 3 and lines[1].startswith("0,3,10.0,1.0,10.0")


def test_stream_twice_raises():
    rec = Recorder(lambda: FakeDevice(), poll_interval_s=0.001)
    rec.start_stream(3)
    with pytest.raises(Km003cError):
        rec.start_stream(3)
    rec.stop_stream()


def test_empty_putdata_is_ignored():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: len(dev.masks) >= 5)
    status = rec.stream_status()
    assert status["running"] and status["error"] is None and status["stats"]["samples"] == 0
    rec.stop_stream()


def test_stream_samples_decimates():
    dev = FakeDevice([queue(list(range(100)))])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 100)
    out = rec.stream_samples(max_samples=10)
    assert len(out) == 10 and out[0]["sequence"] == 0
    assert len(rec.stream_status()["recent"]) == 20
    rec.stop_stream()


def test_pd_events_paging_and_decode():
    dev = FakeDevice([[LogicalPacket(ATT_PD, False, 0, PD_STREAM)]])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    wait_until(lambda: rec.pd_events()["total"] == 3)
    page = rec.pd_events(since=1, limit=1)
    assert page["next"] == 2 and page["events"][0]["index"] == 1
    assert page["events"][0]["decoded"]["type"] == "Source_Capabilities"
    assert page["events"][0]["wire_hex"] == "81112c910100"
    assert "decoded" not in rec.pd_events(decode=False)["events"][0]
    rec.stop_pd()
    assert rec.pd_events()["running"] is False


def test_device_error_stops_and_reports():
    dev = FakeDevice(error=Km003cError("unplugged"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: not rec.stream_status()["running"])
    assert "unplugged" in rec.stream_status()["error"]


def test_combined_mask_when_both_active():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    rec.start_pd()
    wait_until(lambda: (ATT_ADCQUEUE | ATT_PD) in dev.masks)
    rec.stop_pd()
    rec.stop_stream()


def test_fast_stream_polls_at_least_every_20ms():
    # The device returns at most 63 queued samples per reply (6-bit chunk) and drops older ones,
    # so a 1000 SPS stream must be polled well within 63 ms regardless of the default interval.
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.05)
    rec.start_stream(3)
    time.sleep(0.3)
    rec.stop_stream()
    assert len(dev.masks) >= 10


def test_transport_error_triggers_callback():
    from km003c_mcp.usb_device import DeviceTimeoutError
    calls = []
    dev = FakeDevice(error=DeviceTimeoutError("unplugged"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001, on_transport_error=lambda: calls.append(1))
    rec.start_pd()
    wait_until(lambda: calls == [1])


def test_logical_error_stops_device_stream():
    dev = FakeDevice(error=Km003cError("bad reply"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: ("stop",) in dev.graph)
    assert not rec.stream_status()["running"]


def test_bad_csv_path_does_not_start_graph(tmp_path):
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    with pytest.raises(OSError):
        rec.start_stream(3, csv_path=str(tmp_path / "missing" / "s.csv"))
    assert dev.graph == [] and not rec.stream_status()["running"]


def test_abort_stops_running_capture():
    rec = Recorder(lambda: FakeDevice(), poll_interval_s=0.001)
    rec.start_stream(3)
    rec.abort("USB session reset")
    status = rec.stream_status()
    assert not status["running"] and "USB session reset" in status["error"]
