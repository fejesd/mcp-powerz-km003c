import pytest
import usb.core

import km003c_mcp.usb_device as ud
from fakes import FakeTransport, live_adc_with_tid, memory_read_replies
from km003c_mcp.crypto import ADDR_HARDWARE_ID
from km003c_mcp.usb_device import (
    DeviceBusyError, DeviceNotFoundError, DeviceTimeoutError, Km003cDevice, Km003cError,
    PyUsbTransport,
)

HWID = bytes.fromhex("3037314b42500dff110affff")


def test_connect_retries_after_disconnect_reply():
    t = FakeTransport([bytes.fromhex("03010000"), bytes.fromhex("05020000")])
    Km003cDevice(t).connect()
    assert [w[0] for w in t.writes] == [0x02, 0x02]


def test_get_data_skips_stale_packets():
    t = FakeTransport([bytes.fromhex("05090000"), live_adc_with_tid(1)])
    r = Km003cDevice(t).read_adc()
    assert r.vdd_v == pytest.approx(3.2504)
    assert t.writes == [bytes.fromhex("0c010200")]


def test_memory_read_flow():
    plain = bytes(range(20))
    t = FakeTransport(memory_read_replies(1, 0x420, plain))
    assert Km003cDevice(t).memory_read(0x420, 20) == plain
    assert t.writes[0][:4] == bytes.fromhex("44010101")


def test_memory_read_not_readable():
    with pytest.raises(Km003cError, match="not readable"):
        Km003cDevice(FakeTransport([bytes.fromhex("27010000")])).memory_read(0x0, 16)


def test_memory_read_refused_while_streaming():
    dev = Km003cDevice(FakeTransport([]))
    dev.graph_rate = 3
    with pytest.raises(Km003cError, match="stream"):
        dev.memory_read(0x420, 16)


def test_authenticate_accepts_tid_zero_response():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000302") + bytes(32)])
    dev = Km003cDevice(t)
    assert dev.authenticate() == 1 and dev.auth_level == 1
    assert t.writes[1][:4] == bytes.fromhex("4c020002")


def test_start_graph_fails_when_auth_level_zero():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000102") + bytes(32)])
    with pytest.raises(Km003cError, match="streaming auth failed"):
        Km003cDevice(t).start_graph(3)


def test_start_and_stop_graph():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    dev = Km003cDevice(t)
    dev.auth_level = 1
    dev.start_graph(3)
    assert dev.graph_rate == 3 and t.writes[0] == bytes.fromhex("0e010600")
    dev.stop_graph()  # Reject means "already stopped"
    assert dev.graph_rate is None


def test_timeout_when_no_reply():
    with pytest.raises(DeviceTimeoutError):
        Km003cDevice(FakeTransport([])).read_adc()


class StubUsb:
    def __init__(self, serial, fail=None):
        self.serial_number = serial
        self.fail = fail

    def set_configuration(self):
        if self.fail:
            raise self.fail


def test_open_busy_maps_to_device_busy(monkeypatch):
    err = usb.core.USBError("Access denied (insufficient permissions)", errno=13)
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A", fail=err)])
    with pytest.raises(DeviceBusyError, match="POWER-Z PC software"):
        PyUsbTransport.open()


def test_open_no_device(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [])
    with pytest.raises(DeviceNotFoundError):
        PyUsbTransport.open()


def test_open_multiple_devices_requires_serial(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A"), StubUsb("B")])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, intf: None)
    with pytest.raises(Km003cError, match="A.*B"):
        PyUsbTransport.open()
    assert PyUsbTransport.open("B").serial_number == "B"
