# Reliable Controls MACH protocol (UDP 21068)

Reconstructed from the CQC CML driver `MEng.User.CQC.Drivers.ReliableControls.Mach.Driver`
by Mark Stega, versions 0.1 (13 JUN 2007) through 1.3 (01 JAN 2010).

This is not vendor documentation. Everything below is inferred from a
third-party driver implementation, and the field names are that driver's
guesses. Two bytes (`aaoiu`, `range`) are named but never interpreted, so
their meaning is unknown. Treat this as a working description, not a spec.

---

## Transport

| | |
|---|---|
| Protocol | UDP (**not** TCP) |
| Remote port | 21068 |
| Local port | 21068 — the driver binds it explicitly |
| Broadcasts | disabled |
| Byte order | little-endian |
| Read timeout | 1500 ms, retried once at 3000 ms |
| Poll cadence | 500 ms fast / 5000 ms slow; config query delayed 2 s after start |

Because it's UDP, a socket "connects" successfully whether or not anything is
listening. Silence is the only failure signal.

Binding local 21068 means only one client per host — you cannot run a port of
this driver and CQC on the same machine simultaneously.

---

## Frame layout

12-byte header, then an optional payload.

| Offset | Name | Notes |
|---|---|---|
| 0 | To | destination controller; **+128** when addressing a subnet |
| 1 | From | always 0 |
| 2 | Command | see below |
| 3 | ExtraL | bank number, or packed point address low byte |
| 4 | ExtraH | packed point address high byte, or command-50 subfunction |
| 5 | SubController | 0 = main controller, else subnet number |
| 6 | Checksum | `sum(bytes 0..5) & 0xFF` |
| 7 | Port | constant `0x45` |
| 8 | Reserved1 | 0 |
| 9 | Reserved2 | 0 |
| 10 | CountL | payload byte count |
| 11 | CountH | driver always writes 0; parsed as a LE uint16 |
| 12+ | Data | payload |

**The checksum covers only bytes 0–5.** Port, Reserved, Count and payload are
excluded. Getting this wrong is the most likely reason a panel ignores you.

Total datagram length is `12 + payload`.

---

## Commands

### Requests

| Cmd | Meaning | ExtraL | ExtraH |
|---|---|---|---|
| 1 | Read Outputs bank | bank | 0 |
| 2 | Read Inputs bank | bank | 0 |
| 3 | Read Variables bank | bank | 0 |
| 12 | System status | 0 | 0 |
| 20 | Read individual point | addr low | addr high |
| 50 | Multiplex | 0 | 8 = subnetwork status |
| 120 | **Write** individual point | addr low | addr high |

### Responses

Responses echo the request command **plus 100**: 101 Outputs, 102 Inputs,
103 Variables, 108 subnet status, 112 system status, 120 individual point.

The reply's own ExtraL and SubController carry the bank and subnet actually
returned. The driver re-derives both from the header rather than trusting what
it asked for — worth copying, since replies can arrive out of order on UDP.

### Addressing

| Target | To | SubController |
|---|---|---|
| Main controller | `controller` | 0 |
| Subnet *n* | `controller + 128` | *n* |

---

## Point records

Bulk read replies (101/102/103) contain fixed-size records starting at payload
offset 0. A record whose description begins with NUL is an empty slot.

| Type | Code | Record size | Description length | Access |
|---|---|---|---|---|
| Outputs | 0 | 42 bytes | 21 | read-only |
| Inputs | 1 | 38 bytes | 21 | read-only |
| Variables | 2 | 38 bytes | 22 | **read/write** |

Common to all three: the value is an IEEE **Float4 at record offset +22**.

Variables carry additional fields, and you must capture them to write:

| Record offset | Field | Notes |
|---|---|---|
| 0 | Description | NUL-terminated, up to 22 bytes |
| 22 | Value | Float4 |
| 26 | `aaoiu` | purpose unknown, echoed verbatim on write |
| 27 | `range` | purpose unknown; probably engineering range |
| 28 | Label | NUL-terminated, up to 9 bytes; often the unit |
| 37 | `prgctrl` | purpose unknown, echoed verbatim on write |

### Bank geometry

| Type | Points per bank | Address offset |
|---|---|---|
| Inputs, Outputs | 32 | `32 × bank` |
| Variables, banks 0–1 | 48 | `48 × bank` |
| Variables, bank ≥ 2 | 32 | `96 + (bank−2) × 32` |

Point address is `1 + slot_index + bank_offset`, 1-based.

The shipped driver reads **only banks 0 and 1** — `kProto_NumBanksStandard`
was cut from 4 to 2 on 11 JAN 2008. Per subnet that caps discovery at 64
inputs, 64 outputs, 96 variables. The protocol allows 7 banks. If a point
exists in the panel but never appears, raise the bank count first.

---

## System status (command 12 → 112)

26 bytes per panel. For communication controller *c*:

```
base       = (c - 1) * 26
panel name = NUL-terminated string at base + 4, max 18 bytes
```

## Subnetwork scan (command 50, ExtraH 8 → 108)

6 bytes per subnet controller. Subnet *n* (1–62) is active when
`payload[(n-1) * 6] & 0x01`. Subnet 0 (the main controller) is always present
and is not represented in the bitmap.

The driver scans 62 subnets (reduced from 124). Subnet bank "B" is
unimplemented — noted as a known restriction in the source.

---

## Writing a variable

Only Variables are writable. Only controllers **1–31** accept writes; the
source calls this a protocol restriction.

**You cannot write blind.** The write payload is the original 38-byte variable
record with only the float replaced — description, `aaoiu`, `range`, label and
`prgctrl` must all be echoed back exactly as read. So a discovery pass is
mandatory before any write.

### 1. Pack the address into ExtraL/ExtraH

A 16-bit value, split low byte into ExtraL and high byte into ExtraH. The bit
layout differs by network, which is unusual enough to be worth flagging.

**Main network (subnet 0)** — `CCCCCTTTTNNNNNNN`

```
packed = (controller << 11) | (type << 7) | ((address - 1) % 128)
type   = 3 for addresses 1-128, otherwise 0
```

**Subnet** — `NNNNNTTTTCCCCCCC`

```
packed = (((address - 1) % 32) << 11) | (type << 7) | subnet
type   = 3   for addresses 1-32
         13  for 33-64
         14  for 65-96
         15  for 97-128
```

Addresses above 128 on a subnet are not addressable.

Note the field order inverts between the two forms: controller occupies the
high bits on the main network and the low bits on a subnet.

### 2. Build the 38-byte payload

| Offset | Content |
|---|---|
| 0 | original description bytes |
| 22 | **new value**, Float4 LE |
| 26 | original `aaoiu` |
| 27 | original `range` |
| 28 | original label |
| 37 | original `prgctrl` |

Everything else zero-filled.

### 3. Send command 120

No acknowledgement is expected. The driver does not wait for one. Confirm by
re-reading the bank.

### Worked example

Controller 1, main network, variable address 3, description `SpaSetPt`,
`aaoiu=0xA1`, `range=0x04`, label `degF`, `prgctrl=0x02`, new value 104.0:

```
packed = (1 << 11) | (3 << 7) | 2 = 0x0982   ->  ExtraL 0x82, ExtraH 0x09

header:  01 00 78 82 09 00 04 45 00 00 26 00
payload: 53 70 61 53 65 74 50 74 00 00 00 00 00 00 00 00 00 00
         00 00 00 00 d0 42 a1 04 64 65 67 46 00 00 00 00 00 02
```

Checksum `0x04` = `(1 + 0 + 120 + 130 + 9 + 0) & 0xFF`. Count `0x26` = 38.
Float at offset 22 of the payload: `00 00 d0 42` = 104.0.

---

## Field naming

The CQC driver builds names as `<subnet><prefix><addr:03d>-<description>`:

- subnet: `Main-` for 0, else `A<n>-`
- prefix: `In`, `Out`, `Var`
- description with every character outside `[0-9A-Za-z]` replaced by `_`

For example `Main-Var003-SpaSetPt`, `A5-In012-Pool_Temp_`.

Reproducing this exactly is worth doing — it keeps names consistent between
CQC and anything new, which matters if both run during a migration.

---

## Known limitations, inherited from the driver

1. Subnet bank "B" unimplemented.
2. Variable writes only on controllers 1–31.
3. 62 subnets scanned (protocol allows more).
4. Banks 0–1 only, so higher banks are invisible.
5. No write acknowledgement, so no delivery guarantee — it's UDP.
6. `aaoiu`, `range` and `prgctrl` are opaque. They round-trip correctly but
   nothing is known about what they mean. In particular, `range` likely holds
   the engineering min/max, which would give proper bounds for HA `number`
   entities — currently a guess.

## Bugs in the original worth knowing

- `Points.QueryPointRAWprgctrl` returns `m_PointRAWaaoiu`. Latent only because
  the write path reads the arrays directly via `QueryPointByFieldId`.
- The high-verbosity logging blocks guard printing of `range` and `prgctrl`
  with `If (RAWaaoiu = 0)`, so when `aaoiu` is zero those two bytes are
  reported as `00` regardless of their real value. `RCChatter.txt` is not
  trustworthy for those fields.
- `SingleController.Driver` has an empty `FloatFldChanged` — writes through
  that variant silently do nothing. It also never marks subnet 0 active, so it
  discovers no points when configured for the main controller.
