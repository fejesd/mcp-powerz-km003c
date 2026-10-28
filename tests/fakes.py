"""Shared fixtures and fake transports for the KM003C tests."""

# GetData(ADC) response captured live from a KM003C (SN 014270) with no load attached.
LIVE_ADC = bytes.fromhex(
    "41028002" "0100000b"
    "00080000 65feffff 3b0d0000 f5ffffff 9f0d0000 ddffffff"
    "0080 0c03 6e01 4803 6e03 f87e 00 80 2300 5300 5600".replace(" ", ""))


def live_adc_with_tid(tid: int) -> bytes:
    data = bytearray(LIVE_ADC)
    data[1] = tid
    return bytes(data)
