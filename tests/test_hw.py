"""Hardware tests against a real KM003C. Run with KM003C_HW=1 and the POWER-Z PC software closed."""

import os
import time

import pytest

from km003c_mcp.server import Km003cService, build_tools

pytestmark = [pytest.mark.hw,
              pytest.mark.skipif(os.environ.get("KM003C_HW") != "1", reason="set KM003C_HW=1")]


@pytest.fixture(scope="module")
def tools():
    service = Km003cService()
    yield build_tools(service)
    service.shutdown()


def test_hw_device_info(tools):
    info = tools.device_info()
    assert info["model"] == "KM003C" and info["fw_version"] and info["usb_serial"]


def test_hw_measure(tools):
    r = tools.measure()
    assert 0 <= r["vbus_v"] < 50 and 3.0 < r["vdd_v"] < 3.6


def test_hw_stream_1000sps(tools):
    assert tools.stream_start(rate_sps=1000)["started"]
    time.sleep(2)
    st = tools.stream_stop()
    assert st["samples"] >= 1500 and st["dropped_samples"] <= st["samples"] * 0.01


def test_hw_pd_capture_idle(tools):
    assert tools.pd_capture_start() == {"started": True}
    time.sleep(0.5)
    page = tools.pd_capture_read()
    assert page["error"] is None
    assert tools.pd_capture_stop()["stopped"]


def test_hw_trigger_open_close(tools):
    lines = tools.trigger_raw("pd pdo")["lines"]
    assert any(line.startswith("max power") for line in lines)
    assert tools.trigger_close()["lines"] == ["ok"]


def test_hw_trigger_and_usb_concurrently(tools):
    tools.stream_start(rate_sps=50)
    assert "lines" in tools.trigger_raw("pd pdo")
    time.sleep(0.5)
    assert tools.stream_status()["error"] is None
    tools.stream_stop()
    tools.trigger_close()
