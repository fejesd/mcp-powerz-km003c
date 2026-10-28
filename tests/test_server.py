import pytest

from fakes import FakeSerial, FakeTransport, live_adc_with_tid
from km003c_mcp.server import Km003cService, build_tools
from km003c_mcp.trigger import TriggerPort
from km003c_mcp.usb_device import DeviceBusyError, DeviceNotFoundError

ACCEPT_1 = bytes.fromhex("05010000")


def opened(*replies):
    """Session-open replies (Connect accept, StopGraph reject) followed by later replies."""
    return [ACCEPT_1, bytes.fromhex("06020000"), *replies]


def make_tools(script=None, serial_replies=None, factory=None):
    fs = FakeSerial(serial_replies or {})
    svc = Km003cService(
        transport_factory=factory or (lambda: FakeTransport(script or [])),
        trigger_factory=lambda port: TriggerPort(port, serial_factory=lambda *a, **k: fs),
        port_finder=lambda serial_number=None, override=None: "COMX",
    )
    tools = build_tools(svc)
    tools._service = svc  # test-only handle
    return tools, fs


def test_measure_single():
    tools, _ = make_tools(opened(live_adc_with_tid(3)))
    r = tools.measure()
    assert r["vdd_v"] == pytest.approx(3.2504) and r["temp_c"] is None


def test_measure_average():
    tools, _ = make_tools(opened(*[live_adc_with_tid(t) for t in (3, 4, 5)]))
    r = tools.measure(samples=3, interval_ms=0)
    assert r["samples"] == 3
    assert r["vbus_v"]["mean"] == pytest.approx(0.002048)
    assert set(r["power_w"]) == {"mean", "min", "max"}


def test_measure_rejects_bad_sample_count():
    tools, _ = make_tools(opened())
    assert tools.measure(samples=0)["kind"] == "ValueError"


def test_busy_device_returns_error_dict():
    def busy():
        raise DeviceBusyError("close the POWER-Z PC software")
    tools, _ = make_tools(factory=busy)
    r = tools.measure()
    assert r["kind"] == "DeviceBusyError" and "POWER-Z PC software" in r["error"]


def test_reopens_after_error():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise DeviceNotFoundError("not plugged in")
        return FakeTransport(opened(live_adc_with_tid(3)))
    tools, _ = make_tools(factory=factory)
    assert tools.measure()["kind"] == "DeviceNotFoundError"
    assert "vbus_v" in tools.measure()


def test_reopens_after_transport_timeout():
    transports = [FakeTransport(opened()), FakeTransport(opened(live_adc_with_tid(3)))]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    assert tools.measure()["kind"] == "DeviceTimeoutError"
    assert "vbus_v" in tools.measure()


def test_stream_rejects_unknown_rate():
    tools, _ = make_tools(opened())
    assert tools.stream_start(rate_sps=100)["kind"] == "ValueError"


def test_trigger_fast_charge_opens_pdm_first():
    tools, fs = make_tools(factory=lambda: (_ for _ in ()).throw(DeviceNotFoundError("x")),
                           serial_replies={"pdm open": [b"pdm mode entry\n"], "qc 9V": [b"ok\n"]})
    r = tools.trigger_fast_charge("qc", 9000)
    assert fs.written[:2] == [b"pdm open\r\n", b"qc 9V\r\n"]
    assert r == {"command": "qc 9V", "lines": ["ok"]}


def test_trigger_fast_charge_invalid_returns_error():
    tools, _ = make_tools()
    assert "error" in tools.trigger_fast_charge("qc", 7000)


def test_trigger_pd_request_builds_command():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"]})
    assert tools.trigger_pd_request(position=5, voltage_mv=12000, current_ma=2000)["command"] == \
        "pd req=5,volt=12000,cur=2000"


def test_poller_transport_error_reopens_on_next_call():
    import time
    transports = [FakeTransport(opened()), FakeTransport(opened(live_adc_with_tid(3)))]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    assert tools.pd_capture_start() == {"started": True}
    deadline = time.monotonic() + 3
    while tools.pd_capture_read()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tools.pd_capture_read()["error"]
    assert "vbus_v" in tools.measure()


def test_stream_start_bad_csv_returns_error(tmp_path):
    tools, _ = make_tools(opened())
    r = tools.stream_start(rate_sps=1000, csv_path=str(tmp_path / "missing" / "x.csv"))
    assert r["kind"] in ("FileNotFoundError", "OSError")


def test_invalidate_trigger_closes_serial_port():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"]})
    tools.trigger_raw("pd pdo", timeout_s=0.1)
    tools._service.invalidate_trigger()
    assert fs.closed


def test_trigger_close_sends_pdm_close():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"],
                                           "pdm close": [b"ok\n"]})
    tools.trigger_raw("pd pdo", timeout_s=0.1)
    assert tools.trigger_close() == {"command": "pdm close", "lines": ["ok"]}
    assert fs.closed
