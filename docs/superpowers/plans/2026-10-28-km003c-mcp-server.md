# KM003C MCP szerver – implementációs terv

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Általános célú MCP szerver a ChargerLab POWER-Z KM003C USB-C mérőhöz: mérés, nagy sebességű adatgyűjtés, PD-forgalom elkapás/dekódolás és protokoll-trigger.

**Architecture:** Tiszta (I/O-mentes) protokoll-réteg (`protocol`, `crypto`, `pd`) → eszközréteg cserélhető transzporttal (`usb_device` a vendor bulk interfészen, `trigger` a CDC soros porton) → egyetlen háttér-poller szál a streaminghez és PD-elkapáshoz (`recorder`) → vékony FastMCP tool-réteg (`server`). Minden I/O-réteg fake transzporttal unit-tesztelhető; a valódi eszközt külön, opt-in hardvertesztek használják.

**Tech Stack:** Python ≥ 3.11, `mcp` (FastMCP, stdio), `pyusb` + `libusb-package`, `pyserial`, `cryptography` (AES-128-ECB), `pytest`.

**Spec:** Nincs külön spec (a felhasználó kihagyta a tervezési kérdéseket; cél: „általános célú MCP szerver”). A protokoll-forrás: `okhsunrog/km003c-protocol-research` `docs/protocol_reference.md` és a `okhsunrog/km003c-rs` `km003c-lib/src/{auth,device,pd}.rs` (MIT/Apache), valamint a ChargerLab `bin/translations/cmd.pdf` (CDC parancsok). Az alábbi értékek ezekből és élő eszköz-mérésből (2026-10-28, SN 014270) származnak.

## Global Constraints

- Python `>=3.11`; csomagnév `km003c-mcp`, import-név `km003c_mcp`, src-layout (`src/km003c_mcp/`).
- USB: VID `0x5FC9`, PID `0x0063`; vendor interfész `0`, bulk OUT `0x01`, bulk IN `0x81`; tranzakció-timeout `2000 ms`; IN olvasás buffer `8192` bájt.
- CDC trigger port: ugyanazon VID/PID `MI_02` interfésze (Windows-on pl. `COM14`); sorvég küldéskor `\r\n`, a válasz `\n`-tagolt sorok; baud érdektelen (115200-at adunk meg).
- Minden szám little-endian. Minden kódkomment és commit message angol nyelvű.
- Kimeneti mértékegységek a tool-okban: V, A, W, °C, ms, Wh, mAh — float-ként, mezőnévben jelölve (`vbus_v`, `ibus_a` …).
- Pozitív IBUS = áram a mérő USB-C anya (bemenet) felől a dugó (kimenet) felé.
- Konfiguráció környezeti változókkal: `KM003C_SERIAL` (USB sorozatszám szerinti választás), `KM003C_COM_PORT` (CDC port felülírás), `KM003C_LIBUSB_DLL` (libusb DLL útvonal felülírás).
- A szerver nem írhat az eszköz memóriájába/beállításaiba és nem végezhet firmware-frissítést (csak olvasás + mérés + trigger).
- Hardveres tesztek csak `KM003C_HW=1` mellett futnak (`@pytest.mark.hw`), alapból kihagyva.

## Review Focus

- **A ChargerLab PC-szoftver (Mtools) fogja az eszközt** → az interfész-claim elbukik; elvárt: `DeviceBusyError` „close the POWER-Z PC software” üzenettel, a szerver tovább fut, a következő hívás újrapróbál. (Teszt: Task 5.)
- **Eszköz kihúzása stream / PD-capture közben** → a poller szál leáll, a `*_status` tool a hibát mutatja, a következő tool-hívás újranyitja az eszközt. (Teszt: Task 7.)
- **Elavult / nem illeszkedő válaszcsomagok** (élőben látott: Disconnect-re rossz TID-del jött `03 01 00 00`; a referencia szerint az első Connect-re néha Disconnect jön) → a tranzakció eldobja a nem illeszkedőket, a Connect 3× újrapróbál. (Teszt: Task 5.)
- **Üres PutData** (nincs pufferelt minta / PD esemény) → üres lista, nem kivétel. (Teszt: Task 2.)
- **Több KM003C csatlakoztatva** → `KM003C_SERIAL` választ; a trigger-port is sorozatszám alapján párosodik. (Teszt: Task 5 és Task 6.)

---

## Fájlszerkezet

| Fájl | Felelősség |
|---|---|
| `pyproject.toml`, `.gitignore`, `README.md` | csomag, függőségek, `km003c-mcp` console script, használati leírás |
| `src/km003c_mcp/protocol.py` | fejléc-kódolás, PutData-lánc, ADC / AdcQueue / PD-status / PD-event parse (tiszta) |
| `src/km003c_mcp/crypto.py` | AES kulcsok, MemoryRead és StreamingAuth csomagépítés/-értelmezés (tiszta) |
| `src/km003c_mcp/pd.py` | USB PD üzenet dekóder (fejléc, típusnevek, PDO, RDO) (tiszta) |
| `src/km003c_mcp/usb_device.py` | `UsbTransport` protokoll, `PyUsbTransport`, `Km003cDevice` |
| `src/km003c_mcp/trigger.py` | CDC port keresés, `TriggerPort`, trigger-parancs építők |
| `src/km003c_mcp/recorder.py` | háttér-poller: AdcQueue stream statisztikával/CSV-vel, PD esemény-napló |
| `src/km003c_mcp/server.py` | `Km003cService` (lusta eszköznyitás, újranyitás) + FastMCP tool-ok + `main()` |
| `tests/fakes.py` | `FakeTransport`, `FakeSerial` |
| `tests/test_*.py` | modulonkénti unit tesztek; `tests/test_hw.py` hardveres |

---

### Task 1: Projektváz + fejlécek, PutData-lánc, ADC parse

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/km003c_mcp/__init__.py`, `src/km003c_mcp/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces (`protocol.py`):
  - Konstansok: `CMD_CONNECT=0x02, CMD_DISCONNECT=0x03, CMD_ACCEPT=0x05, CMD_REJECT=0x06, CMD_GET_DATA=0x0C, CMD_START_GRAPH=0x0E, CMD_STOP_GRAPH=0x0F, CMD_NOT_READABLE=0x27, CMD_PUT_DATA=0x41, CMD_MEMORY_READ=0x44, CMD_STREAMING_AUTH=0x4C`; `ATT_ADC=0x0001, ATT_ADCQUEUE=0x0002, ATT_SETTINGS=0x0008, ATT_PD=0x0010`; `GRAPH_RATES_SPS={0: 2, 1: 10, 2: 50, 3: 1000}`.
  - `encode_ctrl(cmd: int, tid: int, attribute: int = 0) -> bytes` — `u32 = (cmd & 0x7F) | (tid << 8) | ((attribute & 0x7FFF) << 17)`, 4 bájt.
  - `encode_raw(cmd: int, tid: int, word: int) -> bytes` — `bytes([cmd, tid]) + word.to_bytes(2, "little")`.
  - `@dataclass(frozen=True) Header(cmd: int, flag: int, tid: int, attribute: int, obj_count: int, raw_word: int)`; `decode_header(data: bytes) -> Header` (attribute = `(u32 >> 17) & 0x7FFF`, obj_count = `u32 >> 22`, raw_word = `u16(data[2:4])`).
  - `@dataclass(frozen=True) LogicalPacket(attribute: int, next: bool, chunk: int, payload: bytes)`; `parse_putdata(data: bytes) -> tuple[Header, list[LogicalPacket]]` — kiterjesztett fejléc: `att = w & 0x7FFF`, `next = (w >> 15) & 1`, `chunk = (w >> 16) & 0x3F`, `size = w >> 22`; a láncot a `next` bitig követi. Csonka lánc → `ProtocolError`.
  - `class ProtocolError(Exception)`.
  - `@dataclass(frozen=True) AdcReading(vbus_v, ibus_a, power_w, vbus_avg_v, ibus_avg_a, vbus_ori_avg_v, ibus_ori_avg_a, temp_c: float | None, cc1_v, cc2_v, dp_v, dm_v, vdd_v, rate_index: int, flags: int, cc2_avg_v, dp_avg_v, dm_avg_v)`; `parse_adc(payload: bytes) -> AdcReading`.

ADC payload (44 B): `0 i32 vbus µV | 4 i32 ibus µA | 8 i32 vbus_avg µV | 12 i32 ibus_avg µA | 16 i32 vbus_ori_avg µV | 20 i32 ibus_ori_avg µA | 24 i16 temp (raw/128 °C; -32768 = nincs érték → None) | 26 u16 cc1 0.1 mV | 28 u16 cc2 0.1 mV | 30 u16 D+ 0.1 mV | 32 u16 D− 0.1 mV | 34 u16 vdd 0.1 mV | 36 u8 rate_index | 37 u8 flags | 38 u16 cc2_avg mV | 40 u16 D+_avg mV | 42 u16 D−_avg mV`. `power_w = vbus_v * ibus_a`.

- [ ] **Step 1: Projektváz.** `git init`; `pyproject.toml` (hatchling, `requires-python = ">=3.11"`, deps: `mcp>=1.2`, `pyusb>=1.2`, `libusb-package`, `pyserial>=3.5`, `cryptography>=42`; extra `dev = ["pytest>=8"]`; `[project.scripts] km003c-mcp = "km003c_mcp.server:main"`; pytest marker `hw`). `.gitignore`: `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`. `python -m venv .venv` és `.venv\Scripts\pip install -e .[dev]`.

- [ ] **Step 2: Failing tesztek** a `tests/test_protocol.py`-ban, az élőben rögzített ADC-válasszal:

```python
LIVE_ADC = bytes.fromhex(
    "41028002" "0100000b"
    "00080000 65feffff 3b0d0000 f5ffffff 9f0d0000 ddffffff"
    "0080 0c03 6e01 4803 6e03 f87e 00 80 2300 5300 5600".replace(" ", ""))

def test_encode_ctrl_get_adc():
    assert encode_ctrl(CMD_GET_DATA, 0, ATT_ADC) == bytes.fromhex("0c000200")
    assert encode_ctrl(CMD_GET_DATA, 7, ATT_ADC | ATT_PD) == bytes.fromhex("0c072200")
    assert encode_ctrl(CMD_START_GRAPH, 1, 3) == bytes.fromhex("0e010600")

def test_encode_raw():
    assert encode_raw(CMD_MEMORY_READ, 2, 0x0101) == bytes.fromhex("44020101")

def test_parse_putdata_live_adc():
    hdr, pkts = parse_putdata(LIVE_ADC)
    assert (hdr.cmd, hdr.tid, hdr.obj_count) == (0x41, 2, 10)
    assert len(pkts) == 1 and pkts[0].attribute == ATT_ADC and len(pkts[0].payload) == 44 and not pkts[0].next

def test_parse_adc_live_values():
    r = parse_adc(parse_putdata(LIVE_ADC)[1][0].payload)
    assert r.vbus_v == pytest.approx(0.002048)
    assert r.ibus_a == pytest.approx(-0.000411)
    assert r.temp_c is None                      # raw 0x8000
    assert r.cc1_v == pytest.approx(0.0780)
    assert r.vdd_v == pytest.approx(3.2504)
    assert (r.rate_index, r.flags) == (0, 0x80)
    assert r.dm_avg_v == pytest.approx(0.086)

def test_parse_adc_temperature():
    payload = bytearray(44); payload[24:26] = (int(31.5 * 128)).to_bytes(2, "little", signed=True)
    assert parse_adc(bytes(payload)).temp_c == pytest.approx(31.5)

def test_parse_putdata_chain_two_packets():
    adc = (ATT_ADC | (1 << 15) | (44 << 22)).to_bytes(4, "little") + bytes(44)
    pd = (ATT_PD | (12 << 22)).to_bytes(4, "little") + bytes(12)
    _, pkts = parse_putdata(bytes.fromhex("41058004") + adc + pd)
    assert [p.attribute for p in pkts] == [ATT_ADC, ATT_PD]

def test_parse_putdata_truncated_raises():
    with pytest.raises(ProtocolError):
        parse_putdata(LIVE_ADC[:30])
```

- [ ] **Step 3: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_protocol.py -v` → FAIL (ImportError).
- [ ] **Step 4: Implementáció** a fenti Interfaces szerint, `struct`-tal.
- [ ] **Step 5: Futtatás.** Ugyanaz a parancs → PASS.
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: protocol headers, PutData chain and ADC parsing"`

---

### Task 2: AdcQueue és PD (status + eseményfolyam) parse

**Files:**
- Modify: `src/km003c_mcp/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Consumes: `LogicalPacket`, `ProtocolError` (Task 1).
- Produces:
  - `@dataclass(frozen=True) AdcQueueSample(sequence: int, marker: int, vbus_v: float, ibus_a: float, cc1_v: float, cc2_v: float, dp_v: float, dm_v: float)`; `parse_adcqueue(payload: bytes, rate_index: int) -> list[AdcQueueSample]` — 20 B/minta: `0 u16 seq | 2 u16 marker | 4 i32 vbus µV | 8 i32 ibus µA | 12,14,16,18 u16 cc1, cc2, D+, D−`; a 4 segédfeszültség LSB-je `0.1 mV`, ha `rate_index == 0`, egyébként `1 mV`. A payload hossza nem 20 többszöröse → `ProtocolError`. Üres payload → `[]`.
  - `sequence_step(rate_index: int) -> int` → `{0: 500, 1: 100, 2: 20, 3: 1}`.
  - `@dataclass(frozen=True) PdStatus(timestamp_ms: int, vbus_v: float, ibus_a: float, cc1_v: float, cc2_v: float)` — 12 B: `u32 ts ms | u16 vbus mV | i16 ibus mA | u16 cc1 mV | u16 cc2 mV`; az átváltás osztással (`mV / 1000`), így a tesztbeli egyenlőség pontos.
  - `@dataclass(frozen=True) PdEvent(timestamp_ms: int, kind: str, sop: int | None, wire: bytes)` (`kind` ∈ `"connect"`, `"disconnect"`, `"message"`).
  - `@dataclass(frozen=True) PdEventStream(preamble: PdStatus, events: list[PdEvent])`.
  - `parse_pd(payload: bytes) -> PdStatus | PdEventStream` — 12 B → `PdStatus`; hosszabb → preambulum + események. Eseményfej 6 B: ha `b[0] == 0x45` → kapcsolati rekord: ts = 24 bit (`b[1..3]`), kód `b[5]`: `0x21` connect, `0x22` disconnect, egyéb → `kind="message", sop=kód, wire=b""`; különben `size_flag=b[0]`, `ts=u32(b[1:5])`, `sop=b[5]`, `wire_len=(size_flag & 0x3F) - 5` (negatív → `ProtocolError`), majd `wire_len` bájt adat.

- [ ] **Step 1: Failing tesztek:**

```python
def _sample(seq, uv, ua, aux):
    return struct.pack("<HHiiHHHH", seq, 3, uv, ua, *aux)

def test_parse_adcqueue_units_by_rate():
    p = _sample(1000, 5_000_000, -1_500_000, (780, 366, 840, 878))
    s_slow = parse_adcqueue(p, 0)[0]; s_fast = parse_adcqueue(p, 3)[0]
    assert (s_slow.vbus_v, s_slow.ibus_a) == (5.0, -1.5)
    assert s_slow.cc1_v == pytest.approx(0.078) and s_fast.cc1_v == pytest.approx(0.780)

def test_parse_adcqueue_empty_and_bad_length():
    assert parse_adcqueue(b"", 3) == []
    with pytest.raises(ProtocolError):
        parse_adcqueue(bytes(19), 3)

def test_parse_pd_status_live_prefix():
    st = parse_pd(bytes.fromhex("55040200" "0300" "0000" "0000" "0000"))
    assert st == PdStatus(132181, 0.003, 0.0, 0.0, 0.0)

PD_STREAM = (bytes(12)
    + bytes.fromhex("451027000021")                   # connect @ 10000 ms
    + bytes.fromhex("8b1a27000000") + bytes.fromhex("81112c910100")  # SOP Source_Capabilities, 1 PDO
    + bytes.fromhex("452827000022"))                  # disconnect @ 10024 ms

def test_parse_pd_event_stream():
    ev = parse_pd(PD_STREAM).events
    assert [e.kind for e in ev] == ["connect", "message", "disconnect"]
    assert ev[1].timestamp_ms == 10010 and ev[1].sop == 0 and ev[1].wire == bytes.fromhex("81112c910100")

def test_parse_pd_bad_size_flag_raises():
    with pytest.raises(ProtocolError):
        parse_pd(bytes(12) + bytes.fromhex("830000000000"))
```

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_protocol.py -v` → az új tesztek FAIL.
- [ ] **Step 3: Implementáció** a fenti Interfaces szerint.
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git commit -am "feat: AdcQueue and PD status/event stream parsing"`

---

### Task 3: Kriptó – MemoryRead és StreamingAuth

**Files:**
- Create: `src/km003c_mcp/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: `encode_raw`, `CMD_MEMORY_READ`, `CMD_STREAMING_AUTH`, `decode_header`, `ProtocolError` (Task 1).
- Produces:
  - `KEY_MEMORY = b"Lh2yfB7n6X7d9a5Z"`, `KEY_AUTH_ENC = b"Fa0b4tA25f4R038a"`, `KEY_AUTH_DEC = b"FX0b4tA25f4R038a"`; címek: `ADDR_DEVICE_INFO=0x00000420`, `ADDR_FIRMWARE_INFO=0x00004420`, `ADDR_CALIBRATION=0x03000C00`, `ADDR_HARDWARE_ID=0x40010450`; `HARDWARE_ID_SIZE=12`.
  - `aes_ecb(data: bytes, key: bytes, decrypt: bool = False) -> bytes` (`cryptography` Cipher, ECB).
  - `build_memory_read_packet(address: int, size: int, tid: int) -> bytes` — 36 B: `encode_raw(0x44, tid, 0x0101)` + AES(key0) a 32 B plaintextből: `u32 addr | u32 size | u32 0xFFFFFFFF | u32 crc32(első 12 B) | 16×0xFF`.
  - `parse_memory_confirmation(body: bytes) -> tuple[int, int]` — 16 B: `addr, size, magic, crc`; ha `magic != 0xFFFFFFFF` vagy CRC hibás → `ProtocolError`.
  - `memory_cipher_len(size: int) -> int` = `ceil(size/16)*16`; `decrypt_memory(cipher: bytes, size: int) -> bytes` (key0, a végén `size`-ra vágva; rossz hossz → `ProtocolError`).
  - `build_streaming_auth_packet(hardware_id: bytes, tid: int, timestamp_ms: int) -> bytes` — 36 B: `encode_raw(0x4C, tid, 0x0200)` + AES(`KEY_AUTH_ENC`) a plaintextből: `u64 ts | 12 B hwid | 12×0xFF`.
  - `parse_streaming_auth_response(packet: bytes) -> int` — auth level: `word = u16(packet[2:4])`; ha `packet[0] & 0x7F != 0x4C` vagy `word & 1 == 0` → `ProtocolError`; visszatérés `(word >> 1) & 0b11` (0 = sikertelen).
  - `@dataclass(frozen=True) DeviceInfo(model: str, hw_version: str, mfg_date: str, fw_version: str, fw_date: str, serial_id: str, uuid: str, hardware_id: str)`; `parse_device_info(dev: bytes, fw: bytes, cal: bytes, hwid: bytes) -> DeviceInfo` — `dev[0x10:0x1C]` model, `dev[0x1C:0x28]` hw, `dev[0x28:0x40]` mfg; ha `u32(fw[0:4]) != 0xFFFFFFFF`: `fw[0x1C:0x28]` fw_version, `fw[0x28:0x34]` fw_date; `cal[0:7]` serial (strip), `cal[7:0x27]` uuid; NUL-végű stringek; `hardware_id` = hwid hex.

- [ ] **Step 1: Failing tesztek** a referencia-implementáció rögzített csomagjaival:

```python
MEMREAD = bytes.fromhex("4402010133f8860c0054288cdc7e52729826872dd18b539a39c407d5c063d91102e36a9e")
AUTH = bytes.fromhex("4c0600025538815b69a452c83e54ef1d70f3bc9ae6aac1b12a6ac07c20fde58c7bf517ca")
HWID = bytes.fromhex("3037314b42500dff110affff")

def test_reference_memory_read_decrypts():
    plain = aes_ecb(MEMREAD[4:], KEY_MEMORY, decrypt=True)
    assert struct.unpack_from("<III", plain) == (0x420, 64, 0xFFFFFFFF)

def test_build_memory_read_roundtrip():
    pkt = build_memory_read_packet(ADDR_HARDWARE_ID, 12, 2)
    assert pkt[:4] == bytes.fromhex("44020101") and len(pkt) == 36
    plain = aes_ecb(pkt[4:], KEY_MEMORY, decrypt=True)
    assert parse_memory_confirmation(plain[:16]) == (ADDR_HARDWARE_ID, 12)

def test_memory_confirmation_bad_crc():
    body = struct.pack("<IIII", 0x420, 64, 0xFFFFFFFF, 0)
    with pytest.raises(ProtocolError):
        parse_memory_confirmation(body)

def test_reference_streaming_auth_contains_hwid():
    assert aes_ecb(AUTH[4:], KEY_AUTH_ENC, decrypt=True)[8:20] == HWID

def test_build_streaming_auth():
    pkt = build_streaming_auth_packet(HWID, 6, 1_700_000_000_000)
    assert pkt[:4] == AUTH[:4]
    assert aes_ecb(pkt[4:], KEY_AUTH_ENC, decrypt=True)[8:20] == HWID

def test_parse_streaming_auth_levels():
    assert parse_streaming_auth_response(bytes.fromhex("4c000302") + bytes(32)) == 1
    assert parse_streaming_auth_response(bytes.fromhex("4c000102") + bytes(32)) == 0
    with pytest.raises(ProtocolError):
        parse_streaming_auth_response(bytes.fromhex("4c000002") + bytes(32))

def test_decrypt_memory_trims_and_checks_len():
    cipher = aes_ecb(bytes(range(16)), KEY_MEMORY)
    assert decrypt_memory(cipher, 12) == bytes(range(12))
    with pytest.raises(ProtocolError):
        decrypt_memory(cipher[:8], 12)
```

Plusz egy `test_parse_device_info` szintetikus blokkokkal (model `"KM003C"` a 0x10-n, fw `"1.9.9"` a 0x1C-n, magic ≠ 0xFFFFFFFF; és egy eset magic = 0xFFFFFFFF → `fw_version == ""`).

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_crypto.py -v` → FAIL.
- [ ] **Step 3: Implementáció.**
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: AES memory read and streaming auth framing"`

---

### Task 4: USB PD üzenet dekóder

**Files:**
- Create: `src/km003c_mcp/pd.py`
- Test: `tests/test_pd.py`

**Interfaces:**
- Produces:
  - `decode_pdo(pdo: int) -> dict` — kulcsok: `type` és a típus mezői (értékek V/A/W-ban, minden float `round(x, 3)`-mal kerekítve, hogy a tesztek egyenlőséggel összevethetők legyenek).
  - `decode_message(wire: bytes, sop: int, source_pdos: list[dict] | None = None) -> dict` — kulcsok: `sop` (`"SOP"`, `"SOP'"`, `"SOP''"`, `"SOP'_Debug"`, `"SOP''_Debug"` a 0–4 kódokra, egyébként `"SOP?<n>"`), `message_id`, `spec_revision` (`"1.0"/"2.0"/"3.x"`), `power_role`/`data_role` (csak SOP-nál; `"source"/"sink"`, `"dfp"/"ufp"`), `extended: bool`, `type` (név), `num_objects`, `objects` (hex lista); `Source_Capabilities`/`Sink_Capabilities` esetén `pdos: list[dict]` (1-től számozott `position`-nel); `Request`/`EPR_Request` esetén `request: dict`; ha a wire 2 bájtnál rövidebb → `{"error": "...", "raw": hex}`. A fejléc + objektumok utáni maradék bájtok `trailing` hexként (pl. CRC).
  - `PdDecoder` osztály: `decode(event: PdEvent) -> dict` — megjegyzi az utolsó `Source_Capabilities` PDO-it és ezzel dekódolja a későbbi `Request`-et.

Fejléc-bitek: `4:0 type | 5 data role | 7:6 spec rev | 8 power role | 11:9 msg id | 14:12 num obj | 15 extended`. Típusnevek (USB PD 3.1 szerint):
- Control (num_obj = 0, nem extended): 1 GoodCRC, 2 GotoMin, 3 Accept, 4 Reject, 5 Ping, 6 PS_RDY, 7 Get_Source_Cap, 8 Get_Sink_Cap, 9 DR_Swap, 10 PR_Swap, 11 VCONN_Swap, 12 Wait, 13 Soft_Reset, 14 Data_Reset, 15 Data_Reset_Complete, 16 Not_Supported, 17 Get_Source_Cap_Extended, 18 Get_Status, 19 FR_Swap, 20 Get_PPS_Status, 21 Get_Country_Codes, 22 Get_Sink_Cap_Extended, 23 Get_Source_Info, 24 Get_Revision.
- Data: 1 Source_Capabilities, 2 Request, 3 BIST, 4 Sink_Capabilities, 5 Battery_Status, 6 Alert, 7 Get_Country_Info, 8 Enter_USB, 9 EPR_Request, 10 EPR_Mode, 11 Source_Info, 12 Revision, 15 Vendor_Defined.
- Extended: 1 Source_Capabilities_Extended, 2 Status, 3 Get_Battery_Cap, 4 Get_Battery_Status, 5 Battery_Capabilities, 6 Get_Manufacturer_Info, 7 Manufacturer_Info, 8 Security_Request, 9 Security_Response, 10 Firmware_Update_Request, 11 Firmware_Update_Response, 12 PPS_Status, 13 Country_Info, 14 Country_Codes, 15 Sink_Capabilities_Extended, 16 Extended_Control, 17 EPR_Source_Capabilities, 18 EPR_Sink_Capabilities, 19 Vendor_Defined_Extended. Ismeretlen kód → `"Reserved_<n>"`.

PDO (`bits 31:30`): `0 fixed`: `voltage_v = b19:10 × 0.05`, `max_current_a = b9:0 × 0.01`; `1 battery`: `max_voltage_v = b29:20 × 0.05`, `min_voltage_v = b19:10 × 0.05`, `max_power_w = b9:0 × 0.25`; `2 variable`: `max/min_voltage_v` mint battery, `max_current_a = b9:0 × 0.01`; `3 APDO` (`b29:28`): `0 pps`: `max_voltage_v = b24:17 × 0.1`, `min_voltage_v = b15:8 × 0.1`, `max_current_a = b6:0 × 0.05`; `1 epr_avs`: `max_voltage_v = b25:17 × 0.1`, `min_voltage_v = b15:8 × 0.1`, `pdp_w = b7:0`; `2 spr_avs`: `max_current_15v_a = b19:10 × 0.01`, `max_current_20v_a = b9:0 × 0.01`.

RDO: `object_position = b31:28`; ha a hivatkozott forrás-PDO `pps` vagy `*_avs`: `output_voltage_v = b20:9 × 0.02`, `operating_current_a = b6:0 × 0.05`; egyébként (fixed/variable, ill. ha nincs ismert PDO): `operating_current_a = b19:10 × 0.01`, `max_current_a = b9:0 × 0.01`.

- [ ] **Step 1: Failing tesztek:**

```python
def test_source_caps_fixed_5v3a():
    m = decode_message(bytes.fromhex("81112c910100"), 0)
    assert (m["type"], m["sop"], m["power_role"], m["spec_revision"]) == ("Source_Capabilities", "SOP", "source", "3.x")
    assert m["pdos"][0] == {"position": 1, "type": "fixed", "voltage_v": 5.0, "max_current_a": 3.0}

def test_pps_apdo():
    pdo = (3 << 30) | (0 << 28) | (110 << 17) | (33 << 8) | 60   # 3.3-11 V, 3 A
    d = decode_pdo(pdo)
    assert d == {"type": "pps", "max_voltage_v": 11.0, "min_voltage_v": 3.3, "max_current_a": 3.0}

def test_control_goodcrc_and_sop_prime():
    m = decode_message(bytes.fromhex("4100"), 1)
    assert (m["type"], m["sop"], m["num_objects"]) == ("GoodCRC", "SOP'", 0)

def test_request_uses_previous_source_caps():
    dec = PdDecoder()
    dec.decode(PdEvent(0, "message", 0, bytes.fromhex("81112c910100")))
    rdo = (1 << 28) | (300 << 10) | 300
    req = dec.decode(PdEvent(1, "message", 0, bytes.fromhex("8210") + rdo.to_bytes(4, "little")))
    assert req["request"] == {"object_position": 1, "operating_current_a": 3.0, "max_current_a": 3.0}

def test_too_short_wire():
    assert "error" in decode_message(b"\x01", 0)
```

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_pd.py -v` → FAIL.
- [ ] **Step 3: Implementáció.**
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: USB PD message, PDO and RDO decoder"`

---

### Task 5: Eszközréteg (USB transzport + `Km003cDevice`)

**Files:**
- Create: `src/km003c_mcp/usb_device.py`, `tests/fakes.py`
- Test: `tests/test_usb_device.py`

**Interfaces:**
- Consumes: Task 1–3 minden publikus neve.
- Produces:
  - Kivételek: `class Km003cError(Exception)`; `DeviceNotFoundError(Km003cError)`; `DeviceBusyError(Km003cError)`; `DeviceTimeoutError(Km003cError)`; `CommandRejectedError(Km003cError)`.
  - `class UsbTransport(Protocol)`: `write(data: bytes, timeout_ms: int) -> None`; `read(size: int, timeout_ms: int) -> bytes` (timeoutnál `DeviceTimeoutError`); `close() -> None`; `serial_number: str`.
  - `class PyUsbTransport`: `@classmethod open(cls, serial: str | None = None) -> PyUsbTransport` — libusb backend: `KM003C_LIBUSB_DLL` ha meg van adva, különben `libusb_package.find_library`; VID/PID szerinti keresés, több találatnál `serial` kötelezően szűr (ha nincs megadva és több van → `Km003cError` a listázott sorozatszámokkal); `set_configuration()` + `claim_interface(0)`; claim/open `USBError` (pl. access denied / busy) → `DeviceBusyError("... close the POWER-Z PC software ...")`; nincs eszköz → `DeviceNotFoundError`.
  - `class Km003cDevice`:
    - `__init__(self, transport: UsbTransport)`; `lock: threading.RLock` attribútum; `graph_rate: int | None`; `auth_level: int | None`.
    - `connect() -> None` — Connect max. 3×, 100 ms szünettel; Accept-et vár a saját TID-re.
    - `disconnect() -> None` — Disconnect, a válasz típusát nem ellenőrzi (élőben `0x03` jött), timeout-ot lenyel.
    - `get_data(attribute_mask: int) -> list[LogicalPacket]` — GetData; az első `0x41` válasz a saját TID-del.
    - `read_adc() -> AdcReading`; `read_pd() -> PdStatus | PdEventStream`.
    - `memory_read(address: int, size: int) -> bytes` — tilos aktív graph alatt (`Km003cError`); vár `0x44` (saját TID) / `0x06` / `0x27` választ; `0x44` → `parse_memory_confirmation(resp[4:20])` és az echo egyezik, majd fejléc nélküli nyers olvasások `memory_cipher_len(size)` bájtig → `decrypt_memory`; `0x06` → `CommandRejectedError`; `0x27` → `Km003cError("not readable")`.
    - `device_info() -> DeviceInfo` — a 4 blokk (`0x420`/64, `0x4420`/64, `0x3000C00`/64, `0x40010450`/12) + `parse_device_info`.
    - `authenticate() -> int` — hardware ID olvasás, `build_streaming_auth_packet(hwid, tid, now_ms)`, válasz a **TID-től függetlenül** az első `0x4C` típusú csomag; `auth_level` eltárolva.
    - `start_graph(rate_index: int) -> None` — ha `auth_level` nincs/0 → előbb `authenticate()`; ha utána is 0 → `Km003cError("streaming auth failed")`; StartGraph(attribute=rate_index) → Accept.
    - `stop_graph() -> None` — Accept vagy Reject (már leállt) is OK.
    - `close() -> None` — `stop_graph` ha aktív, `disconnect`, `transport.close`; hibákat lenyel.
  - Belső `_transact(packet: bytes, match: Callable[[bytes], bool]) -> bytes`: ír, majd 2 s határidőig olvas; a nem illeszkedő csomagokat eldobja (debug log); határidő → `DeviceTimeoutError`. A TID 1..255 körbejáró számláló.
- `tests/fakes.py`: `FakeTransport(script: list[bytes | Exception], serial_number="TEST")` — `writes: list[bytes]`; `read` a scriptből ad (Exception-t dob, ha az jön; kifogyáskor `DeviceTimeoutError`). A válaszokban a TID-et a teszt a várható számlálóértékre állítja (az első parancs TID-je 1).

- [ ] **Step 1: Failing tesztek:**

```python
def test_connect_retries_after_disconnect_reply():
    t = FakeTransport([bytes.fromhex("03010000"), bytes.fromhex("05020000")])
    Km003cDevice(t).connect()
    assert [w[0] for w in t.writes] == [0x02, 0x02]

def test_get_data_skips_stale_packets():
    t = FakeTransport([bytes.fromhex("05090000"), LIVE_ADC_WITH_TID(1)])
    r = Km003cDevice(t).read_adc()
    assert r.vdd_v == pytest.approx(3.2504)

def test_memory_read_flow():
    # confirmation (tid 1) + two raw cipher chunks for 20 bytes -> 32 bytes cipher
    ...  # build expected confirmation via struct + crc32, cipher via aes_ecb(KEY_MEMORY)
    assert dev.memory_read(0x420, 20) == plain[:20]

def test_memory_read_not_readable():
    with pytest.raises(Km003cError, match="not readable"):
        Km003cDevice(FakeTransport([bytes.fromhex("27010000")])).memory_read(0x0, 16)

def test_authenticate_accepts_tid_zero_response():
    # memory read script for hwid (tid 1) then auth reply with tid 0
    assert dev.authenticate() == 1

def test_start_graph_fails_when_auth_level_zero():
    with pytest.raises(Km003cError, match="streaming auth failed"):
        dev.start_graph(3)

def test_timeout_when_no_reply():
    with pytest.raises(DeviceTimeoutError):
        Km003cDevice(FakeTransport([])).read_adc()

def test_open_busy_maps_to_device_busy(monkeypatch):
    # monkeypatch usb.core.find to return a stub whose claim raises usb.core.USBError(errno=13 / "Access denied")
    with pytest.raises(DeviceBusyError, match="POWER-Z PC software"):
        PyUsbTransport.open()

def test_open_multiple_devices_requires_serial(monkeypatch):
    # two stub devices with serials "A", "B"; open(None) -> Km003cError mentioning "A" and "B"; open("B") selects B
```

(A `...` részeket az implementáló a megadott értékekkel tölti ki; a `LIVE_ADC_WITH_TID(n)` a Task 1 `LIVE_ADC`-ja `[1] = n`-nel, a `tests/fakes.py`-ban.)

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_usb_device.py -v` → FAIL.
- [ ] **Step 3: Implementáció.**
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: KM003C USB device layer with retry, matching and auth"`

---

### Task 6: Protokoll-trigger (CDC soros port)

**Files:**
- Create: `src/km003c_mcp/trigger.py`
- Modify: `tests/fakes.py` (`FakeSerial`)
- Test: `tests/test_trigger.py`

**Interfaces:**
- Consumes: `Km003cError` (Task 5).
- Produces:
  - `find_trigger_port(serial_number: str | None = None, override: str | None = None) -> str` — `override` (vagy `KM003C_COM_PORT`) elsőbbséget élvez; különben `serial.tools.list_ports.comports()` VID `0x5FC9` / PID `0x0063` szerint, `serial_number` megadásakor arra szűrve; nincs találat → `Km003cError`.
  - `class TriggerPort`: `__init__(self, port: str, serial_factory=serial.Serial)`; `command(cmd: str, timeout_s: float = 3.0, idle_s: float = 0.5) -> list[str]` — input-puffer ürítés, `cmd + "\r\n"` küldés, olvasás addig, amíg legalább egy teljes sor jött és `idle_s` óta csend van, vagy `timeout_s` letelik; visszatér a nem üres, strip-elt sorokkal (üres lista is érvényes); `ensure_open() -> list[str]` — egyszer `pdm open`, utána no-op; `close() -> list[str]` — `pdm close` és a port zárása; `is_open: bool`.
  - Parancsépítők (tiszta függvények, `ValueError` érvénytelen bemenetre):
    - `pd_request_command(position: int, voltage_mv: int | None = None, current_ma: int | None = None) -> str` → `"pd req=2"`, `"pd req=2,cur=3000"`, `"pd req=5,volt=12000,cur=2000"` (sorrend: req, volt, cur; `position` 1–13).
    - `fast_charge_command(protocol: str, voltage_mv: int, current_ma: int | None = None, request: int | None = None) -> str`:
      - `qc` → `"qc 9V"` (csak 5000/9000/12000/20000 mV);
      - `qc3` → `"qc3 volt=3800"` (3600–20000, 200 mV lépés);
      - `fcp`/`afc`/`sfcp` → `"fcp 9V"` (5000/9000/12000);
      - `scp`/`vfcp`/`tfcp` → `"scp volt=11000,cur=5000"` (`current_ma` kötelező);
      - `ufcs` → `"ufcs req=1,volt=11000,cur=4000"` (`request` és `current_ma` kötelező).
    - `entry_command(protocol: str) -> str` — `pd|ufcs|qc|fcp|scp|afc|vfcp|sfcp|tfcp|bc|apple|list|list+` → `"entry <p>"`.

- [ ] **Step 1: Failing tesztek:**

```python
def test_command_collects_lines_until_idle():
    fs = FakeSerial({"pdm open": [b"pdm mode entry\n", b"ver1.0\n"]})
    tp = TriggerPort("COMX", serial_factory=lambda *a, **k: fs)
    assert tp.command("pdm open", idle_s=0.05) == ["pdm mode entry", "ver1.0"]
    assert fs.written == [b"pdm open\r\n"]

def test_command_timeout_returns_empty():
    tp = TriggerPort("COMX", serial_factory=lambda *a, **k: FakeSerial({}))
    assert tp.command("entry list", timeout_s=0.1, idle_s=0.05) == []

def test_ensure_open_only_once():
    fs = FakeSerial({"pdm open": [b"pdm mode entry\n"]})
    tp = TriggerPort("COMX", serial_factory=lambda *a, **k: fs)
    tp.ensure_open(); tp.ensure_open()
    assert fs.written.count(b"pdm open\r\n") == 1

@pytest.mark.parametrize("args,expected", [
    (("qc", 9000), "qc 9V"), (("qc3", 3800), "qc3 volt=3800"), (("afc", 12000), "afc 12V"),
    (("scp", 11000, 5000), "scp volt=11000,cur=5000"), (("ufcs", 11000, 4000, 1), "ufcs req=1,volt=11000,cur=4000")])
def test_fast_charge_command(args, expected):
    assert fast_charge_command(*args) == expected

@pytest.mark.parametrize("args", [("qc", 7000), ("qc3", 3700), ("scp", 9000), ("foo", 5000)])
def test_fast_charge_command_rejects(args):
    with pytest.raises(ValueError):
        fast_charge_command(*args)

def test_pd_request_command():
    assert pd_request_command(5, 12000, 2000) == "pd req=5,volt=12000,cur=2000"
    assert pd_request_command(2) == "pd req=2"

def test_find_port_by_serial(monkeypatch):
    # comports() stub: two ports with vid 0x5FC9/pid 0x0063, serials "014270" and "999999"
    assert find_trigger_port("014270") == "COM14"
```

`FakeSerial(replies: dict[str, list[bytes]])`: `write` rögzít a `written`-be és a parancshoz tartozó válaszokat sorba állítja; `read(n)` a sorból ad, üresen `b""`-t (a `timeout`-ot nem várja ki).

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_trigger.py -v` → FAIL.
- [ ] **Step 3: Implementáció.**
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: CDC protocol trigger port and command builders"`

---

### Task 7: Háttér-poller – stream és PD-napló

**Files:**
- Create: `src/km003c_mcp/recorder.py`
- Test: `tests/test_recorder.py`

**Interfaces:**
- Consumes: `Km003cDevice` (Task 5: `get_data`, `start_graph`, `stop_graph`, `lock`), `parse_adcqueue`, `sequence_step`, `parse_pd`, `PdEventStream`, `GRAPH_RATES_SPS` (Task 2), `PdDecoder` (Task 4).
- Produces:
  - `@dataclass StreamStats` (mezők: `rate_sps, samples, dropped_samples, duration_s, vbus_min_v, vbus_max_v, vbus_mean_v, ibus_min_a, ibus_max_a, ibus_mean_a, power_max_w, power_mean_w, energy_wh, charge_mah, csv_path: str | None`), `to_dict()`.
  - `class Recorder`:
    - `__init__(self, device_provider: Callable[[], Km003cDevice], poll_interval_s: float = 0.05, keep_samples: int = 10_000)`.
    - `start_stream(rate_index: int, csv_path: str | None = None) -> None` — `device.start_graph(rate_index)`; ha már fut → `Km003cError`. A CSV fejléc: `sequence,marker,vbus_v,ibus_a,power_w,cc1_v,cc2_v,dp_v,dm_v`.
    - `stop_stream() -> StreamStats` — `stop_graph`, CSV zárása, utolsó statisztika.
    - `stream_status() -> dict` — `{"running": bool, "error": str | None, "stats": StreamStats.to_dict(), "recent": [utolsó ≤ 20 minta dict-ként]}`.
    - `stream_samples(max_samples: int = 500) -> list[dict]` — a megtartott (legfeljebb `keep_samples` darabos) pufferből egyenletesen ritkítva.
    - `start_pd() -> None`; `stop_pd() -> None`; `pd_events(since: int = 0, limit: int = 100, decode: bool = True) -> dict` — `{"events": [...], "next": int, "total": int, "running": bool, "error": str | None}`; minden esemény: `index`, `timestamp_ms`, `kind`, `sop`, `wire_hex`, és `decode` esetén `decoded` (`PdDecoder` kimenete).
  - Egyetlen daemon szál fut, amíg a stream vagy a PD-capture aktív: ciklusonként `mask = (ATT_ADCQUEUE ha stream) | (ATT_PD ha pd)`, `device.get_data(mask)`, majd attribútum szerint szétosztás. Energia: minta-intervallum = `sequence_step(rate) / 1000` s; `energy_wh += p * dt / 3600`, `charge_mah += i * dt / 3.6`. Kiesett minták: ha két egymást követő `sequence` különbsége (mod 65536) `> step`, akkor `dropped += diff // step - 1`. PD: csak `PdEventStream` eseményei kerülnek a naplóba (`PdStatus` eldobva).
  - Szál-hiba (bármely kivétel): `error` mezőbe kerül, a futás leáll (stream és PD is), a szál kilép; a `device_provider` a következő start-nál új eszközt adhat.

- [ ] **Step 1: Failing tesztek** egy `FakeDevice`-szal (a `tests/test_recorder.py`-ban: `get_data` előre megadott `list[LogicalPacket]` sorozatot ad, `start_graph`/`stop_graph` rögzít):

```python
def test_stream_stats_energy_and_drops():
    # rate 3 (1000 SPS): sequences 0,1,2,5 at 10 V / 1 A -> dropped 2, 4 samples, energy 4*10*0.001/3600 Wh
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3); wait_until(lambda: rec.stream_status()["stats"]["samples"] == 4)
    st = rec.stop_stream()
    assert st.dropped_samples == 2 and st.vbus_mean_v == pytest.approx(10.0)
    assert st.energy_wh == pytest.approx(4 * 10 * 0.001 / 3600)

def test_stream_writes_csv(tmp_path):
    ...; assert (tmp_path / "s.csv").read_text().splitlines()[0] == "sequence,marker,vbus_v,ibus_a,power_w,cc1_v,cc2_v,dp_v,dm_v"

def test_empty_putdata_is_ignored():
    # get_data returns [] repeatedly -> running stays True, samples 0, error None

def test_pd_events_paging_and_decode():
    # one PD_STREAM payload (Task 2) -> 3 events; pd_events(since=1, limit=1) returns the Source_Capabilities with decoded["type"] == "Source_Capabilities" and next == 2

def test_device_error_stops_and_reports():
    # get_data raises Km003cError("unplugged") -> stream_status()["running"] is False and "unplugged" in error

def test_combined_mask_when_both_active():
    # start_stream + start_pd -> FakeDevice saw mask ATT_ADCQUEUE | ATT_PD
```

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_recorder.py -v` → FAIL.
- [ ] **Step 3: Implementáció.** A szál minden `get_data` hívást `device.lock` alatt végez, így az MCP-ből érkező egyszeri hívások (pl. `measure`) közé ékelődhetnek.
- [ ] **Step 4: Futtatás.** → PASS.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: background poller for AdcQueue streaming and PD capture"`

---

### Task 8: MCP szerver és dokumentáció

**Files:**
- Create: `src/km003c_mcp/server.py`, `README.md`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: Task 5 (`PyUsbTransport.open`, `Km003cDevice`, kivételek), Task 6 (`find_trigger_port`, `TriggerPort`, parancsépítők), Task 7 (`Recorder`).
- Produces:
  - `class Km003cService`: `__init__(self, transport_factory=PyUsbTransport.open, trigger_factory=TriggerPort)`; `device() -> Km003cDevice` (lusta nyitás + `connect()`; `Km003cError` esetén a cache-elt eszközt eldobja, hogy a következő hívás újranyisson); `trigger() -> TriggerPort` (port a megnyitott USB eszköz sorozatszáma alapján); `recorder: Recorder`; `shutdown()`.
  - `mcp = FastMCP("km003c")`, `main()` → `mcp.run()` (stdio).
  - Minden tool `Km003cError`-t `{"error": str(e), "kind": type(e).__name__}` dict-ként ad vissza (nem dob), így a kliens látja az okot.

Tool-ok (név → paraméterek → visszatérés):

| Tool | Paraméterek | Visszatérés |
|---|---|---|
| `device_info` | – | `DeviceInfo` mezői + `usb_serial`, `trigger_port` (ha található) |
| `measure` | `samples: int = 1` (1–100), `interval_ms: int = 100` | egy `AdcReading` dict; `samples > 1` esetén `{"mean": …, "min": …, "max": …, "samples": n}` a V/I/P mezőkre |
| `pd_status` | – | `PdStatus` dict (ha éppen eseményfolyam jön, annak preambuluma) |
| `stream_start` | `rate_sps: Literal[2, 10, 50, 1000] = 1000`, `csv_path: str \| None = None` | `{"started": true, "rate_sps": …}` |
| `stream_status` | – | `Recorder.stream_status()` |
| `stream_samples` | `max_samples: int = 500` | mintalista |
| `stream_stop` | – | `StreamStats.to_dict()` |
| `pd_capture_start` | – | `{"started": true}` |
| `pd_capture_read` | `since: int = 0`, `limit: int = 100`, `decode: bool = True` | `Recorder.pd_events(...)` |
| `pd_capture_stop` | – | `{"stopped": true, "total": n}` |
| `trigger_detect` | `extended: bool = False`, `timeout_s: float = 15` | `{"command": "entry list" / "entry list+", "lines": [...]}` |
| `trigger_enter` | `protocol: str`, `timeout_s: float = 5` | `{"command", "lines"}` |
| `trigger_pd_source_caps` | – | `{"command": "pd pdo", "lines"}` |
| `trigger_pd_request` | `position: int`, `voltage_mv: int \| None`, `current_ma: int \| None` | `{"command", "lines"}` |
| `trigger_fast_charge` | `protocol: str`, `voltage_mv: int`, `current_ma: int \| None`, `request: int \| None` | `{"command", "lines"}` |
| `trigger_reset` | – | `reset` parancs válasza (utána `ensure_open` újra szükséges → `is_open=False`) |
| `trigger_close` | – | `pdm close` válasza |
| `trigger_raw` | `command: str`, `timeout_s: float = 3` | `{"command", "lines"}` — szabad szöveges parancs a `cmd.pdf` szerint |

Minden `trigger_*` tool (a `trigger_close` kivételével) előbb `ensure_open()`-t hív. A `trigger_pd_request`, `trigger_fast_charge`, `trigger_raw` tool leírása angolul figyelmeztet: „Makes the attached charger raise VBUS on the meter's output; disconnect sensitive loads first.”

- [ ] **Step 1: Failing tesztek** (a tool-függvényeket közvetlenül hívva, fake factory-kkal injektált `Km003cService`-szel):

```python
def test_measure_single(service_with_fake_adc):
    r = tools.measure()
    assert r["vdd_v"] == pytest.approx(3.2504) and r["temp_c"] is None

def test_measure_average(service_with_fake_adc):
    r = tools.measure(samples=3, interval_ms=0)
    assert r["samples"] == 3 and "mean" in r

def test_busy_device_returns_error_dict():
    svc = Km003cService(transport_factory=raise_(DeviceBusyError("close the POWER-Z PC software")))
    assert tools_for(svc).device_info()["kind"] == "DeviceBusyError"

def test_reopens_after_error():
    # first factory call raises DeviceNotFoundError, second returns FakeTransport -> second measure() succeeds

def test_trigger_fast_charge_opens_pdm_first(fake_trigger):
    tools.trigger_fast_charge("qc", 9000)
    assert fake_trigger.written[:2] == [b"pdm open\r\n", b"qc 9V\r\n"]

def test_trigger_fast_charge_invalid_returns_error():
    assert "error" in tools.trigger_fast_charge("qc", 7000)
```

- [ ] **Step 2: Futtatás, bukás.** `.venv\Scripts\pytest tests/test_server.py -v` → FAIL.
- [ ] **Step 3: Implementáció.** A tool-okat egy `build_tools(service) -> SimpleNamespace` gyár hozza létre (tesztelhetőség), a modul-szintű `mcp` ezeket regisztrálja egy globális `Km003cService`-szel; `atexit`-ben `service.shutdown()`.
- [ ] **Step 4: Futtatás.** `.venv\Scripts\pytest -v` (teljes suite) → PASS, `hw` tesztek SKIPPED.
- [ ] **Step 5: README** — telepítés (`pip install -e .`), Windows megjegyzés (a vendor interfész WinUSB-vel megy, extra driver nem kell; a ChargerLab PC-szoftver nem futhat közben), konfigurációs env-ek, Claude Code regisztráció:
  `claude mcp add km003c -- <repo>\.venv\Scripts\km003c-mcp.exe`, tool-lista röviden, protokoll-források (MIT/Apache hivatkozással).
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: FastMCP server exposing measurement, streaming, PD capture and trigger tools"`

---

### Task 9: Hardveres validáció a valódi eszközzel

**Files:**
- Create: `tests/test_hw.py`
- Modify: a tesztek által feltárt eltérések szerint a megfelelő modul (+ a hozzá tartozó unit teszt)

**Interfaces:**
- Consumes: Task 8 `Km003cService`, `build_tools`.

- [ ] **Step 1: Hardveres tesztek** (`pytestmark = pytest.mark.hw`, `skipif(os.environ.get("KM003C_HW") != "1")`):
  - `test_hw_device_info`: `model == "KM003C"`, `fw_version` nem üres, `usb_serial` nem üres.
  - `test_hw_measure`: `0 <= vbus_v < 50`, `3.0 < vdd_v < 3.6`.
  - `test_hw_stream_1000sps`: `stream_start(1000)`, 2 s, `stream_stop()` → `samples >= 1500`, `dropped_samples <= samples * 0.01`.
  - `test_hw_pd_capture_idle`: start/read/stop hiba nélkül (töltő nélkül 0 esemény is elfogadott).
  - `test_hw_trigger_open_close`: `trigger_raw("pd pdo")` sorai között van `max power` kezdetű; `trigger_close()` → `["ok"]`.
  - `test_hw_trigger_and_usb_concurrently`: `stream_start(50)` mellett `trigger_raw("pd pdo")` működik, a stream nem kap hibát.
- [ ] **Step 2: Futtatás.** A ChargerLab szoftver bezárva: `$env:KM003C_HW="1"; .venv\Scripts\pytest tests/test_hw.py -v` → PASS; minden eltérést (pl. a hőmérsékletmező ezen a firmware-en) a megfelelő modulban + unit tesztben javítani.
- [ ] **Step 3: Kézi ellenőrzés töltővel** (a felhasználó csatlakoztat egy PD töltőt és egy terhelést/kábelt): `pd_capture_start` → csatlakoztatás → `pd_capture_read` mutat `connect` eseményt, `Source_Capabilities`-t értelmes PDO-kkal és `Request`-et a kiválasztott pozícióval; `trigger_pd_source_caps` + `trigger_pd_request(position=2)` után a `measure` VBUS-a a kért fix feszültség ±5%-án belül van. Egy elkapott valós PD eseményfolyam hex-dumpját fixture-ként felvenni a `tests/test_protocol.py`-ba (`test_parse_pd_real_capture`).
- [ ] **Step 4: Commit.** `git add -A && git commit -m "test: hardware validation against a real KM003C"`
- [ ] **Step 5: Regisztráció és élő próba Claude Code-ból.** `claude mcp add km003c -- <repo>\.venv\Scripts\km003c-mcp.exe`, majd új session-ben `device_info` és `measure` hívás.
