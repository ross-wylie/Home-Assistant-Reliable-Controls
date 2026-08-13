# Quick start

Your Home Assistant calls these **Apps** (they used to be called Add-ons), so
that's the wording used below.

---

## If you already tried installing v1.0.0

That build failed because of a missing `build.yaml`. This is v1.0.1, which
fixes it. Before continuing:

1. **Settings → Apps** → open **Reliable Controls MACH Bridge** →
   **Uninstall**
2. Delete the old `addons\reliable_controls` folder
3. Copy in the new folder, then carry on from step 4 below

The uninstall matters — Supervisor caches the failed build, and reinstalling
over the top can reuse it.

---

## 1. Install the MQTT broker

**Settings → Apps → App Store**, search **Mosquitto broker**, click
**Install**, then **Start**.

Then **Settings → Devices & Services**. An **MQTT** card should appear as
discovered — click **Configure** → **Submit**.

*If no MQTT card appears:* **Add Integration → MQTT**, broker
`core-mosquitto`, port `1883`.

---

## 2. Install Samba

App Store → search **Samba share** → **Install**. Pick the official one named
exactly *Samba share*, not *Samba NAS*.

Open its **Configuration** tab, set a `username` and `password`, **Save**,
then **Start**.

---

## 3. Copy the folder in

In Windows Explorer, type in the address bar:

```
\\homeassistant
```

Log in with the username and password from step 2. You'll see folders
including **addons**, **config** and **share**.

Unzip `reliable_controls.zip` into **addons**, so you get:

```
addons\reliable_controls\config.yaml
addons\reliable_controls\build.yaml
addons\reliable_controls\Dockerfile
addons\reliable_controls\run.sh
addons\reliable_controls\rcp.py
addons\reliable_controls\rc_mqtt_bridge.py
addons\reliable_controls\rc_mock_panel.py
```

> Goes in `addons`, not `config`. And it's the **folder** you copy, not the
> loose files.

---

## 4. Make Home Assistant see it

**Settings → Apps → App Store** → top-right **⋮** → **Check for updates**.
Refresh the page.

A **Local apps** section appears at the top with **Reliable Controls MACH
Bridge**. Click it.

*If it doesn't appear:* the folder is in the wrong place, or `config.yaml`
didn't copy. Re-check step 3.

---

## 5. Configure your first panel

Click **Install**. The first build takes a few minutes — it downloads a base
image and installs Python.

Open the **Configuration** tab. You'll see a `panels` list. Set just one entry
to start with:

```yaml
panels:
  - name: Main House
    host: 10.83.106.161
    controller: 1
```

`controller` is the panel number from CQC's `CommunicationController` setting,
usually `1`. It is **not** an IP address.

Leave everything else alone. `read_only` stays `true`.

**Save**.

> Got more boards? Get one working first, then see `MANY_PANELS.md`. Adding
> twenty at once makes it much harder to tell what went wrong.

---

## 6. Start it and read the log

**Start**, then open the **Log** tab. You want:

```
Auto-detected MQTT broker: core-mosquitto:1883
discovered 47 points
published discovery for 47 entities
```

*If it says `no points found`:* the **controller** number is wrong. It's the
RC panel address, not an IP. Try 1, then 2, then 3. Because the protocol is
UDP there's no error to see — it just goes quiet.

Once it works, on the app's **Info** tab turn on **Start on boot** and
**Watchdog**.

---

## 7. See your entities

**Settings → Devices & Services → MQTT** → click **1 device**.

Your panel appears with entities like `Main-In001-SupplyAirTemp` and
`A17-Var001-M15A_CU1_SETPOINT`. Add them to a dashboard however you like.

---

# Turning on control

It's read-only right now — HA sees everything, changes nothing. On purpose.

**Check the numbers first.** Open the same few points in CQC and confirm HA
shows the same values. This is the only real test that the protocol was
reverse-engineered correctly. If a temperature reads 80.2 in both, good. If HA
shows something like `1.4e38`, stop and send me what you see.

When they match:

1. **Configuration** → set `read_only` to `false` → **Save** → **Restart**
2. Variables become editable number boxes
3. Change one harmless setpoint, confirm in CQC that it landed

The app re-reads the panel after every write and publishes what the panel
actually reports — so a failed write shows the old value rather than
pretending it worked.

---

# The one thing that could stop all of this

Home Assistant has to be able to reach `10.83.106.161`. CQC talks to it from
`10.83.106.155`, so if HA is on a different VLAN, none of this works no matter
how it's configured.

Symptom: step 6 finds no points whichever controller number you try.

Fix: run the same bridge on the CQC server instead — see the end of
`INSTALL.md`. The entities appear in HA identically.

---

# More than one board?

`MANY_PANELS.md` covers running twenty of them: one app instance, one thread
per panel, plus the recorder configuration you'll want before publishing
thousands of entities.
