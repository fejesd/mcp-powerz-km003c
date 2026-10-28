import pytest

from fakes import LIVE_ADC
from km003c_mcp.protocol import (
    ATT_ADC, ATT_PD, CMD_GET_DATA, CMD_MEMORY_READ, CMD_START_GRAPH, ProtocolError,
    encode_ctrl, encode_raw, parse_adc, parse_putdata,
)


def test_encode_ctrl_get_adc():
    assert encode_ctrl(CMD_GET_DATA, 0, ATT_ADC) == bytes.fromhex("0c000200")
    assert encode_ctrl(CMD_GET_DATA, 7, ATT_ADC | ATT_PD) == bytes.fromhex("0c072200")
    assert encode_ctrl(CMD_START_GRAPH, 1, 3) == bytes.fromhex("0e010600")


def test_encode_raw():
    assert encode_raw(CMD_MEMORY_READ, 2, 0x0101) == bytes.fromhex("44020101")


def test_parse_putdata_live_adc():
    hdr, pkts = parse_putdata(LIVE_ADC)
    assert (hdr.cmd, hdr.tid, hdr.obj_count) == (0x41, 2, 10)
    assert len(pkts) == 1 and pkts[0].attribute == ATT_ADC
    assert len(pkts[0].payload) == 44 and not pkts[0].next


def test_parse_adc_live_values():
    r = parse_adc(parse_putdata(LIVE_ADC)[1][0].payload)
    assert r.vbus_v == pytest.approx(0.002048)
    assert r.ibus_a == pytest.approx(-0.000411)
    assert r.temp_c is None  # raw 0x8000
    assert r.cc1_v == pytest.approx(0.0780)
    assert r.vdd_v == pytest.approx(3.2504)
    assert (r.rate_index, r.flags) == (0, 0x80)
    assert r.dm_avg_v == pytest.approx(0.086)


def test_parse_adc_temperature():
    payload = bytearray(44)
    payload[24:26] = int(31.5 * 128).to_bytes(2, "little", signed=True)
    assert parse_adc(bytes(payload)).temp_c == pytest.approx(31.5)


def test_parse_putdata_chain_two_packets():
    adc = (ATT_ADC | (1 << 15) | (44 << 22)).to_bytes(4, "little") + bytes(44)
    pd = (ATT_PD | (12 << 22)).to_bytes(4, "little") + bytes(12)
    _, pkts = parse_putdata(bytes.fromhex("41058004") + adc + pd)
    assert [p.attribute for p in pkts] == [ATT_ADC, ATT_PD]


def test_parse_putdata_truncated_raises():
    with pytest.raises(ProtocolError):
        parse_putdata(LIVE_ADC[:30])
