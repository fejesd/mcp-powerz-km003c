import pytest

from fakes import FakeSerial, FakeTransport, live_adc_with_tid
from km003c_mcp.server import Km003cService, build_tools
from km003c_mcp.trigger import TriggerPort
from km003c_mcp.usb_device import DeviceBusyError, DeviceNotFoundError

ACCEPT_1 = bytes.fromhex("05010000")


def make_tools(script=None, serial_replies=None, factory=None):
    fs = FakeSerial(serial_replies or {})
    svc = Km003cService(
        transport_factory=factory or (lambda: FakeTransport(script or [])),
        trigger_factory=lambda port: TriggerPort(port, serial_factory=lambda *a, **k: fs),
        port_finder=lambda serial_number=None, override=None: "COMX",
    )
    return build_tools(svc), fs


def test_measure_single():
    tools, _ = make_tools([ACCEPT_1, live_adc_with_tid(2)])
    r = tools.measure()
    assert r["vdd_v"] == pytest.approx(3.2504) and r["temp_c"] is None


def test_measure_average():
    tools, _ = make_tools([ACCEPT_1] + [live_adc_with_tid(t) for t in (2, 3, 4)])
    r = tools.measure(samples=3, interval_ms=0)
    assert r["samples"] == 3
    assert r["vbus_v"]["mean"] == pytest.approx(0.002048)
    assert set(r["power_w"]) == {"mean", "min", "max"}


def test_measure_rejects_bad_sample_count():
    tools, _ = make_tools([ACCEPT_1])
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
        return FakeTransport([ACCEPT_1, live_adc_with_tid(2)])
    tools, _ = make_tools(factory=factory)
    assert tools.measure()["kind"] == "DeviceNotFoundError"
    assert "vbus_v" in tools.measure()


def test_reopens_after_transport_timeout():
    transports = [FakeTransport([ACCEPT_1]), FakeTransport([ACCEPT_1, live_adc_with_tid(2)])]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    assert tools.measure()["kind"] == "DeviceTimeoutError"
    assert "vbus_v" in tools.measure()


def test_stream_rejects_unknown_rate():
    tools, _ = make_tools([ACCEPT_1])
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
