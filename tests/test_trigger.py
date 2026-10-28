from types import SimpleNamespace

import pytest

import km003c_mcp.trigger as trig
from fakes import FakeSerial
from km003c_mcp.trigger import (
    TriggerPort, entry_command, fast_charge_command, find_trigger_port, pd_request_command,
)
from km003c_mcp.usb_device import Km003cError


def _port(fs):
    return TriggerPort("COMX", serial_factory=lambda *a, **k: fs)


def test_command_collects_lines_until_idle():
    fs = FakeSerial({"pdm open": [b"pdm mode entry\n", b"ver1.0\n"]})
    assert _port(fs).command("pdm open", idle_s=0.05) == ["pdm mode entry", "ver1.0"]
    assert fs.written == [b"pdm open\r\n"]


def test_command_timeout_returns_empty():
    assert _port(FakeSerial({})).command("entry list", timeout_s=0.1, idle_s=0.05) == []


def test_ensure_open_only_once_and_close():
    fs = FakeSerial({"pdm open": [b"pdm mode entry\n"], "pdm close": [b"ok\n"]})
    tp = _port(fs)
    tp.ensure_open()
    tp.ensure_open()
    assert fs.written.count(b"pdm open\r\n") == 1 and tp.is_open
    assert tp.close() == ["ok"] and not tp.is_open and fs.closed


@pytest.mark.parametrize("args,expected", [
    (("qc", 9000), "qc 9V"), (("qc3", 3800), "qc3 volt=3800"), (("afc", 12000), "afc 12V"),
    (("scp", 11000, 5000), "scp volt=11000,cur=5000"),
    (("ufcs", 11000, 4000, 1), "ufcs req=1,volt=11000,cur=4000")])
def test_fast_charge_command(args, expected):
    assert fast_charge_command(*args) == expected


@pytest.mark.parametrize("args", [
    ("qc", 7000), ("qc3", 3700), ("qc3", 21000), ("scp", 9000), ("ufcs", 9000, 3000), ("foo", 5000)])
def test_fast_charge_command_rejects(args):
    with pytest.raises(ValueError):
        fast_charge_command(*args)


def test_pd_request_command():
    assert pd_request_command(5, 12000, 2000) == "pd req=5,volt=12000,cur=2000"
    assert pd_request_command(2, current_ma=3000) == "pd req=2,cur=3000"
    assert pd_request_command(2) == "pd req=2"
    with pytest.raises(ValueError):
        pd_request_command(0)


def test_entry_command():
    assert entry_command("list+") == "entry list+"
    with pytest.raises(ValueError):
        entry_command("vooc")


def _ports(monkeypatch, ports):
    monkeypatch.setattr(trig, "comports", lambda: ports)


def test_find_port_by_serial(monkeypatch):
    _ports(monkeypatch, [
        SimpleNamespace(device="COM11", vid=0x0403, pid=0x6001, serial_number="X"),
        SimpleNamespace(device="COM14", vid=0x5FC9, pid=0x0063, serial_number="014270"),
        SimpleNamespace(device="COM20", vid=0x5FC9, pid=0x0063, serial_number="999999")])
    assert find_trigger_port("014270") == "COM14"
    assert find_trigger_port("014270", override="COM7") == "COM7"


def test_find_port_missing(monkeypatch):
    _ports(monkeypatch, [])
    with pytest.raises(Km003cError):
        find_trigger_port("014270")
