# MACH Bridge for Home Assistant

Brings Reliable Controls® MACH building controllers into Home Assistant over
their proprietary protocol on UDP 21068 — no BACnet, no gateway hardware, no
vendor software.

Inputs and Outputs become **sensors**. Variables become **number** entities you
can write. Each SubLAN controller appears as its own Home Assistant device.

> **Not affiliated with, endorsed by, or supported by Reliable Controls
> Corporation.** "Reliable Controls" and "MACH" are their trademarks, used here
> only to say what this talks to.

---

## ⚠️ Read this before installing

**This writes to live building control systems.**

- It ships **read-only by default** (`read_only: true`). Leave it that way until
  you have compared values against your own workstation software.
- Writes go to Variables via an undocumented protocol. There is no
  acknowledgement in the protocol — the add-on confirms by re-reading.
- Many Variables are **program-controlled**. Writing to them appears to work and
  is then overwritten by the control program within a scan. The log tells you:
  `panel reports <different value> <- MISMATCH`.
- Polling traverses the MS/TP trunk shared with the panel's own sequences. The
  defaults are deliberately gentle. Do not set `poll_interval` low without
  understanding what that does to your trunk.
- Test on non-critical equipment first. Do not point this at life-safety,
  medical, process or data-centre systems.

No warranty. See LICENSE.

---

## Credit where it belongs

**All protocol knowledge in this project comes from Mark Stega's CQC driver**
`MEng.User.CQC.Drivers.ReliableControls.Mach.Driver`, written in CML for
[Charmed Quark Controller](https://www.charmedquark.com/) between 2007 and 2010.

Nobody here reverse-engineered the Reliable Controls protocol. Mark did that
work, nearly two decades ago, and this project is a Python port of his findings:
the 12-byte frame, the checksum over bytes 0–5, the command numbering and its
+100 response convention, the record geometry, and — the hard part — the two
different bit layouts for packing a point address on the main network versus a
subnet.

If this is useful to you, the credit is his.

`PROTOCOL.md` documents the wire format as reconstructed from that driver.

---

## Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/YOUR-GITHUB-USERNAME/ha-mach-bridge`
3. Install **MACH Bridge**, then **Start**

You also need an MQTT broker — the Mosquitto broker add-on is fine, and this
finds it automatically.

---

## Configuration

```yaml
panels:
  - name: Main House          # drives entity ids; make it unique
    host: 10.83.106.150       # the controller's IP
    controller: 1             # RC panel number, NOT an IP
banks: 2
subnet_mode: auto
split_subnets: true
poll_interval: 300
fast_points: []
fast_interval: 10
read_only: true
```

| Option | What it does |
|---|---|
| `panels` | One entry per controller. `controller` is the panel address from your workstation software's communication-controller setting. |
| `banks` | Point banks to scan per type. `2` matches the original CQC driver. Raise if points are missing. |
| `subnet_mode` | `bitmap` trusts the panel's sub-controller map; `probe` asks 1–62 directly; `auto` tries bitmap then probes. |
| `split_subnets` | Each SubLAN board becomes its own HA device. Strongly recommended — HA renders every entity on a device page at once. |
| `poll_interval` | Gap **between** sweeps, not a rate. A 30-sub-controller panel takes ~60s to sweep. |
| `fast_points` | Name substrings/globs to refresh on the short interval. Keep the list small. |
| `read_only` | `true` publishes Variables as sensors and never writes. |
| `purge_on_start` | One-shot cleanup of orphaned MQTT discovery configs. See below. |

### Entity naming

```
number.<panel name>_<point name>      both slugified

Main House + A11-Var001-MH11_CU1_SETPOINT
  -> number.main_house_a11_var001_mh11_cu1_setpoint
```

---

## Things that will confuse you

**Changing `host` or `controller` orphans every entity.** Identity is derived
from them, so a renumbered panel produces a complete second set and the old set
lingers as retained MQTT configs. Fix: run once with `purge_on_start: true`,
then set it back to false.

**Writes are instant; reads are not.** A setpoint change reaches the panel in
well under a second regardless of `poll_interval`. That interval only governs
how quickly HA notices changes made *at* the panel.

**It's UDP.** A socket "connects" whether or not anything is listening. If the
`controller` number is wrong you get silence, not an error.

**Only Variables are writable.** Inputs and Outputs are read-only in the
protocol itself. Writes also only work on controllers 1–31.

---

## Command-line tools

`rcp.py` runs standalone with no dependencies for diagnosing a panel:

```bash
python3 rcp.py discover --host 10.83.106.150 --controller 1 --out points.json
python3 rcp.py subnets  --host 10.83.106.150 --controller 1
python3 rcp.py point    --host 10.83.106.150 --controller 1 --subnet 5 --last 16
python3 rcp.py watch    --host 10.83.106.150 --controller 1
python3 rcp.py write    --host 10.83.106.150 --controller 1 \
                        --field Main-Var003-SpaSetpoint --value 102 --dry-run
```

`rc_mock_panel.py` is a fake panel that speaks the same protocol, so you can
test Home Assistant wiring without touching real equipment.

---

## Known limitations

Inherited from the original driver, and honest about it:

1. Subnet bank "B" is not implemented.
2. Variable writes only work on controllers 1–31 (protocol restriction).
3. 62 sub-controllers scanned.
4. The `aaoiu`, `range` and `prgctrl` bytes on each Variable round-trip
   correctly but nobody knows what they mean. `range` probably holds the
   engineering range, which would give real bounds for number entities.
   `prgctrl` may flag program-controlled points. Both are open questions —
   contributions welcome.

---

## Contributing

Bug reports benefit hugely from the add-on log: it prints a version banner with
file hashes, a per-subnet point tally, and a device inventory.

If you can decode `prgctrl` or `range`, that would be the most valuable
contribution to this project.

## License

MIT — see [LICENSE](LICENSE).
