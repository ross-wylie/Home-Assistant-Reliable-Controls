# Running 20 boards

Twenty separate panels, each with its own IP, publishing everything.

One app instance handles all of them. Each panel gets its own thread, its own
UDP socket, its own Home Assistant device, and its own availability topic — so
one board going down marks only its own entities unavailable.

---

## 1. Configuration

In the app's **Configuration** tab, add one entry per board:

```yaml
panels:
  - name: Main House
    host: 10.83.106.161
    controller: 1
  - name: Plant Room
    host: 10.83.106.162
    controller: 1
  - name: Front Lobby
    host: 10.83.106.163
    controller: 1
  # ... 17 more
banks: 2
poll_interval: 30
read_only: true
```

**Give every panel a unique name.** Names drive entity ids, so
`name: Main House` gets you `sensor.main_house_main_in001_pooltemp`. Leave a
name blank or duplicate one and the whole set falls back to IP-based ids like
`sensor.10_83_106_161_1_main_in001_pooltemp` — still unique and still correct,
just miserable to write dashboard templates against. The log tells you which
mode it picked.

`controller` is per-board and is usually `1` when each board is its own panel.
Check CQC if unsure.

---

## 2. Expect a lot of entities

Per panel, at `banks: 2`, the ceiling is 64 inputs + 64 outputs + 96 variables
= **224 entities**. Across 20 boards that's up to **4,480**. Real panels are
never fully populated, so the true number is lower, but plan for thousands.

Verified timing: 20 panels discover concurrently in about 1.4 seconds, because
separate IPs mean separate sockets and no contention. Polling is likewise
parallel, so `poll_interval` is the actual refresh period, not something you
multiply by 20.

I'd still raise `poll_interval` to **30** for a fleet this size. Not because the
bridge can't keep up, but because 4,480 entities updating every 15 seconds is a
lot of database writes for no operational benefit on a building system.

---

## 2b. Sub-boards become their own devices

`split_subnets: true` (the default) gives every SubLAN board its own Home
Assistant device, nested under its main panel via `via_device`. So instead of
one device holding 2,000 entities you get:

```
Main House                 (main controller points)
  |- Main House A1         (sub-board 1)
  |- Main House A2         (sub-board 2)
  |- Main House A5         (sub-board 5)
```

This is purely a UI fix, and a big one: HA renders every entity on a device
page in one go, so a device with thousands of points takes forever to open.
Split into a hundred or so per device it's instant.

`unique_id` is deliberately unchanged by this setting, so flipping it
re-parents your existing entities rather than creating duplicates. Entity ids,
history and automations all survive.

Availability stays panel-wide: sub-boards are only reachable through the main
panel, so when it goes offline they all do.

Set `split_subnets: false` to go back to one device per panel.

---

## 3. Fix the recorder before you go wide

This is the step people skip and regret. By default Home Assistant records
every state change of every entity to its database. Thousands of BAS points at
30-second intervals will grow the database by gigabytes a week and make the
history UI unusable.

Add this to `configuration.yaml`:

```yaml
recorder:
  # Keep less history than the 10-day default; BAS trends belong in
  # RC-Archive or InfluxDB, not HA's SQLite file.
  purge_keep_days: 3

  exclude:
    entity_globs:
      # Drop every point from every panel by default...
      - sensor.main_house_*
      - number.main_house_*
      - sensor.plant_room_*
      - number.plant_room_*
      # ...one pair of lines per panel name.

  include:
    entities:
      # ...then re-admit only what you actually want to graph or automate on.
      - sensor.main_house_main_in001_pooltemp
      - sensor.main_house_a17_in002_space_temp
      - number.main_house_a17_var001_m15a_setpoint
```

Entities stay fully visible and controllable in HA either way — excluding from
the recorder only stops history being written. That's the right trade for
points you glance at but never chart.

Restart HA after editing, then check **Settings → System → Storage** over the
next day or two to confirm the database has stopped growing.

If you genuinely want long-term trends on hundreds of points, send them to
InfluxDB rather than the recorder. That's what it's for.

---

## 4. Roll out gradually

Don't paste all 20 in at once.

1. Start with **one** board. Confirm entities appear and values match CQC.
2. Add a **second**. Confirm both devices appear separately and entity ids
   don't collide.
3. Add the rest.
4. Add the recorder config.
5. Only then consider `read_only: false`.

Each step is a `Save` plus `Restart` on the app.

---

## 5. Reading the log with 20 panels

Every line is prefixed with the panel name:

```
[Main House] panel 'POOLHOUSE': 187 points, 187 entities
[Plant Room] panel 'PLANTROOM': 94 points, 94 entities
[Front Lobby] discovery failed (no reply to system status (command 12)); retry in 5s
```

Failed panels retry with exponential backoff from 5 seconds up to 5 minutes, so
one unreachable board doesn't spam the log or block the others.

Every 30 seconds you get a summary line:

```
status: 19/20 panels online, 3140 points
```

That's the line to watch. If it says 19/20 and stays there, one board needs
attention and the log above will name it.

---

## 6. Availability timing

A panel is marked online the instant any value is read. It's marked offline
once nothing has been read for `max(3 × poll_interval, 60)` seconds — so 90
seconds at `poll_interval: 30`.

That lag is deliberate. Availability is time-based rather than
attempt-based because a dead panel makes every request burn its full 1.5-second
timeout plus a 3-second retry; with six requests per cycle, a single "failed
cycle" can take 30 seconds. Counting attempts would mean minutes of unpredictable
delay, and counting a single dropped datagram as failure would flap every
entity in and out of `unavailable`.

Tested: killing one board out of 20 produces exactly one availability
transition after ~65 seconds at `poll_interval: 15`, with no effect on the
other 19. Recovery is detected within one poll cycle.

---

## 7. Writes across 20 boards

`read_only: false` enables writing on **all** configured panels at once.
There's no per-panel switch.

If you want some boards writable and others strictly read-only, run two app
instances: copy the folder to `/addons/reliable_controls_ro/`, change the
`slug` and `name` in its `config.yaml`, and give each instance its own panel
list. Two containers, two configs, complete separation.

Also remember: writes only work on controllers **1–31**, and only Variables are
writable — Inputs and Outputs are read-only in the protocol itself.
