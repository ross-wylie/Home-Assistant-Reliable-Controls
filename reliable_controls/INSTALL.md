# Installing on Home Assistant OS

You cannot run Python scripts directly on HAOS — there is no general shell and
nothing in `/config` executes. This folder wraps the bridge as a **local
add-on**, which gets a configuration UI, logs, auto-start on boot, and survives
updates.

You will not be creating a custom integration. MQTT Discovery produces real
entities — device page, history, automations, dashboard cards — that behave
exactly like an integration's. A `custom_components` integration would be a
much larger rewrite with an identical end result.

---

## Prerequisite: an MQTT broker

Skip if you already have MQTT working.

1. **Settings → Add-ons → Add-on Store**
2. Search **Mosquitto broker** → **Install** → **Start**
3. **Settings → Devices & Services**. HA normally offers to set up MQTT by
   itself — click **Configure** and accept. If not, **Add Integration → MQTT**,
   host `core-mosquitto`, port `1883`.

Mosquitto authenticates against Home Assistant users. The add-on here
auto-detects the broker via Supervisor, so you can leave all four MQTT fields
blank.

---

## Step 1 — get file access to the HAOS machine

Pick one. Samba is easiest if you're on Windows.

### Option A: Samba (recommended)

1. **Settings → Add-ons → Add-on Store** → search **Samba share**
2. **Install**, open **Configuration**, set a username and password
3. **Start**
4. In Windows Explorer, open `\\homeassistant\` (or `\\<HA-IP>\`)

You should see shares including `addons`, `config`, and `share`.

### Option B: Advanced SSH & Web Terminal

1. Add-on Store → **Advanced SSH & Web Terminal** → Install
2. In **Configuration**, add your SSH key or a password, and turn
   **Protection mode OFF** (required to see `/addons`)
3. Start, then open the Terminal from the sidebar

> The plain "Terminal & SSH" add-on is a different one and is more restricted.
> You want the **Advanced** version.

---

## Step 2 — copy this folder into place

Copy the whole `reliable_controls` folder into the **`addons`** share, so you
end up with:

```
addons/
└── reliable_controls/
    ├── config.yaml
    ├── Dockerfile
    ├── run.sh
    ├── rcp.py
    ├── rc_mqtt_bridge.py
    └── rc_mock_panel.py
```

That is `/addons/reliable_controls/` as the Supervisor sees it.

Common mistakes:

- Putting it in `config/` instead of `addons/` — Supervisor won't find it
- Nesting it one level too deep, e.g. `addons/myaddons/reliable_controls/`
- Copying only the `.py` files without `config.yaml`

---

## Step 3 — make Supervisor notice it

1. **Settings → Add-ons → Add-on Store**
2. Top-right **⋮** menu → **Check for updates**
3. Reload the page

A **Local add-ons** section appears at the top with **Reliable Controls MACH
Bridge**. If it doesn't, see Troubleshooting below.

---

## Step 4 — configure

Open the add-on → **Configuration** tab.

| Option | Set it to |
|---|---|
| `rc_host` | your controller's IP, e.g. `10.83.106.161` |
| `controller` | the RC panel number from CQC's `CommunicationController` prompt — **not** an IP |
| `banks` | `2` to match CQC. Raise to `4` if expected points are missing |
| `bind_port` | `0`. Only use `21068` to mimic CQC exactly, and never alongside CQC on one host |
| `read_only` | **leave `true`** for now |
| `poll_interval` | `15` seconds is a sensible start |
| MQTT fields | leave blank to auto-detect Mosquitto |

**Save**.

---

## Step 5 — first run

1. **Install** (the first build takes a few minutes — it's compiling a
   container image), then **Start**
2. Open the **Log** tab

A healthy start looks like:

```
[INFO] Auto-detected MQTT broker: core-mosquitto:1883
[WARNING] READ-ONLY mode: variables publish as sensors and
[WARNING] nothing can be written to the panel.
[INFO] Connecting to Reliable Controls panel at 10.83.106.161 (controller 1)
discovering points on 10.83.106.161 (controller 1)...
panel: <your panel name>
subnets: 0
discovered 47 points
published discovery for 47 entities
polling every 15.0s. Ctrl-C to stop.
```

Turn on **Start on boot** and **Watchdog** on the add-on's Info tab once it's
working.

---

## Step 6 — find your entities

**Settings → Devices & Services → MQTT → devices**

A device named after your panel appears, with entities like
`Main-In001-SupplyAirTemp` and `A17-Var001-M15A_CU1_SETPOINT`.

**Verify against CQC before trusting any of it.** Open the same points in CQC
and confirm the values match. That's the real test of whether the protocol port
is correct — everything up to here only proves the plumbing works.

---

## Step 7 — enable writing

Only after values check out against CQC.

1. Configuration tab → set `read_only` to `false` → **Save** → **Restart**
2. Variables now appear as **number** entities with editable boxes
3. Change one harmless setpoint from HA
4. Confirm in CQC that the same value landed
5. Watch the add-on log — it re-reads after every write and publishes what the
   panel actually reports, not what you asked for

Writes only work on controllers 1–31. The add-on will tell you if yours is out
of range.

---

## Testing without touching the pool

`rc_mock_panel.py` is included. To validate MQTT and HA independently of the
real panel, SSH into the add-on's container or run the mock on any machine and
point `rc_host` at it. Fake pool and spa points appear in HA, and writes get
logged and decoded instead of reaching hardware.

---

## Troubleshooting

**Local add-ons section never appears.** The folder is in the wrong place, or
`config.yaml` has a YAML error. Check it's exactly `/addons/reliable_controls/`
and that Supervisor logs (Settings → System → Logs → Supervisor) don't show a
parse error.

**`No MQTT broker found`.** Install and start Mosquitto, or fill in the
`mqtt_host` field manually.

**`no points found; nothing to publish`.** The bridge reached the network but
the panel didn't answer. Almost always one of:

- wrong `controller` number — this is the panel address, not an IP
- HAOS can't route to the controller's subnet
- something else holds UDP 21068 (set `bind_port` to `0`)

Since it's UDP there is no connection error to see — silence is the only
symptom.

**Values look wrong or wildly scaled.** Likely a misread of the record layout.
Compare against CQC and send the discrepancy back; the raw bytes are the
authority.

**Entities go unavailable.** The add-on stopped. It publishes `offline` to its
availability topic on exit, which is working as intended. Check the Log tab.

---

## If HAOS can't reach the controller

Nothing above helps if there's no network path. CQC reaches the panel from
10.83.106.155, so if HA sits on a different VLAN you need routing before any of
this works. In that case run the bridge directly on the CQC server instead:

```bash
pip install paho-mqtt
python3 rc_mqtt_bridge.py --host 10.83.106.161 --controller 1 \
    --bind-port 0 --mqtt-host <ha-ip> --mqtt-user <u> --mqtt-pass <p> --read-only
```

Same entities in HA, no add-on required.
