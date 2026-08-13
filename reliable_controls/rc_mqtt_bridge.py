#!/usr/bin/env python3
"""
rc_mqtt_bridge.py - bridge one or many Reliable Controls MACH panels to
                    Home Assistant over MQTT Discovery.

Each panel becomes its own HA device with its own availability topic, polled by
its own thread on its own UDP socket. Panels are independent: one going offline
marks only its own entities unavailable.

    Variables       -> number  (read/write)
    Inputs/Outputs  -> sensor  (read-only)

Two ways to configure:

    # single panel, command line
    python3 rc_mqtt_bridge.py --host 10.83.106.161 --controller 1 \
        --mqtt-host 10.83.106.50 --read-only

    # many panels, JSON (this is what the HA app uses)
    python3 rc_mqtt_bridge.py --config /data/options.json \
        --mqtt-host core-mosquitto --mqtt-user x --mqtt-pass y

JSON config shape:

    {
      "panels": [
        {"name": "Pool House", "host": "10.83.106.161", "controller": 1},
        {"name": "Gym",        "host": "10.83.106.162", "controller": 1}
      ],
      "banks": 2, "poll_interval": 15, "read_only": true,
      "min_value": -10000, "max_value": 10000, "step": 0.1
    }

Requires: pip install paho-mqtt
"""

from __future__ import annotations

import argparse
import json
import logging
import fnmatch
import hashlib
import os
import queue
import re
import signal
import sys
import threading
import time
from typing import Dict, List, Optional

import rcp

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt is required:  pip install paho-mqtt")

LOG = logging.getLogger("bridge")

# Bumped with every release. Printed at startup so there is never any doubt
# about which code is actually running inside the container - "Restart" reuses
# the existing image, so replacing the files on disk does nothing until the
# add-on is uninstalled and reinstalled (or Supervisor rebuilds it).
__version__ = "1.5.3"

DISCOVERY_PREFIX = "homeassistant"
BASE = "reliable"

# Panel threads are staggered by this much so twenty panels don't all fire
# their first discovery in the same instant.
STAGGER_SECONDS = 0.35


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")


# ---------------------------------------------------------------------------
# One panel
# ---------------------------------------------------------------------------

class Panel:
    """
    A single controller, its socket, its thread, and its HA device.

    Owns its own rcp.MachClient. Nothing is shared with other panels except the
    MQTT client, so a slow or dead panel cannot stall its neighbours.
    """

    def __init__(self, name: str, host: str, controller: int, cfg, mq, stop: threading.Event):
        self.configured_name = (name or "").strip()
        self.display_name = name or f"{host} ctrl {controller}"
        self.host = host
        self.controller = controller
        self.cfg = cfg
        self.mq = mq
        self.stop = stop

        self.slug = slug(f"{host}_{controller}")
        self.device_id = f"{BASE}_{self.slug}"
        self.avail_topic = f"{BASE}/{self.device_id}/availability"
        # Prefix for entity_ids. Bridge overwrites this with a name-derived
        # value when panel names are unique, giving readable ids like
        # sensor.pool_house_main_in001_pooltemp instead of one starting with an
        # IP address. unique_id deliberately stays host-based, so renaming a
        # panel later never orphans its entities.
        self.id_prefix = self.slug

        # Serialises the UDP socket between the poll loop and MQTT command
        # callbacks, which arrive on paho's thread.
        self.lock = threading.Lock()
        self.rc = rcp.MachClient(host, controller, bind_port=0)
        self.online = False
        self.discovered = False
        self.thread: Optional[threading.Thread] = None
        # Availability is time-based, not attempt-based. Counting consecutive
        # failures doesn't work here: a dead panel makes every request burn its
        # full 1.5s + 3s retry, so one "failed cycle" can take 30 seconds and a
        # count of 3 would mean minutes of delay. Wall-clock staleness is
        # predictable regardless of how long the timeouts take.
        self.last_success = 0.0
        self.stale_after = max(3.0 * cfg.poll_interval, 60.0)

        # Writes are QUEUED, never performed on the MQTT callback thread.
        # paho runs on_message on its single network thread, so doing a UDP
        # write there - or worse, waiting on self.lock while the poll loop holds
        # it - stalls the entire MQTT client: no publishes, and keepalive can
        # expire and drop the connection. That looked exactly like "writes do
        # nothing and values stop updating".
        self.write_queue: "queue.Queue" = queue.Queue()
        # Signals the panel thread that work is waiting, so a command is picked
        # up immediately instead of on the next 0.5s tick.
        self.write_event = threading.Event()
        # command topic -> point name, so on_message is a dict lookup instead of
        # a linear scan over a couple of thousand points.
        self._cmd_index: Dict[str, str] = {}
        # Points matched by cfg.fast_points, and the banks they live in.
        # Populated after discovery.
        self.fast_names: set = set()
        self.fast_banks: List = []

    # -- topics -------------------------------------------------------------

    def state_topic(self, p: rcp.Point) -> str:
        return f"{BASE}/{self.device_id}/{slug(p.name)}/state"

    def command_topic(self, p: rcp.Point) -> str:
        return f"{BASE}/{self.device_id}/{slug(p.name)}/set"

    def writable(self, p: rcp.Point) -> bool:
        return p.writable and not self.cfg.read_only

    def compute_fast_set(self) -> None:
        """
        Work out which points to poll on the fast schedule, and which banks
        that requires reading.

        Polling a few points quickly costs a handful of requests; polling
        everything quickly costs hundreds. Splitting the two is what makes
        "responsive where it matters, gentle on the trunk" possible instead of
        being a single compromise interval.
        """
        patterns = [q.strip().lower() for q in (self.cfg.fast_points or []) if q.strip()]
        if not patterns:
            self.fast_names, self.fast_banks = set(), []
            return

        names, banks = set(), set()
        for pt in self.rc.points.values():
            low = pt.name.lower()
            for q in patterns:
                hit = fnmatch.fnmatch(low, q) if ("*" in q or "?" in q) else (q in low)
                if hit:
                    names.add(pt.name)
                    banks.add((pt.ptype, pt.subnet, pt.bank))
                    break
        self.fast_names = names
        self.fast_banks = sorted(banks)
        if names:
            LOG.info("[%s] fast poll: %d point(s) in %d bank(s) every %ss",
                     self.display_name, len(names), len(banks), self.cfg.fast_interval)
        else:
            LOG.warning("[%s] fast_points matched nothing; check the patterns "
                        "against the names in the log", self.display_name)

    # -- discovery ----------------------------------------------------------

    def _device_block(self, subnet: int) -> dict:
        """
        Home Assistant device for a given sub-controller.

        With split_subnets on, each SubLAN board becomes its own device linked
        to the main panel by via_device, so HA nests them. This matters purely
        for the UI: HA renders every entity on a device page at once, so a
        single device holding a couple of thousand points is painfully slow.
        Twenty devices of a hundred each is instant.

        Availability stays panel-wide on purpose - sub-boards are only
        reachable through the main panel, so they share its fate.
        """
        # Prefer the configured name over the panel's self-reported sysstatus
        # name. Two config entries aimed at the same physical board report the
        # same sysstatus name, producing two identically-named devices and
        # making the duplication invisible. The configured name is unique by
        # construction, so it surfaces the problem instead of masking it.
        base_name = self.configured_name or self.rc.panel_name or self.display_name
        if subnet == 0 or not self.cfg.split_subnets:
            return {
                "identifiers": [self.device_id],
                "name": base_name,
                "manufacturer": "Reliable Controls",
                "model": "MACH (RCP/UDP 21068)",
                "configuration_url": f"http://{self.host}/",
            }
        return {
            "identifiers": [f"{self.device_id}_a{subnet}"],
            "name": f"{base_name} A{subnet}",
            "manufacturer": "Reliable Controls",
            "model": f"MACH SubLAN controller {subnet}",
            "via_device": self.device_id,
        }

    def publish_discovery(self) -> int:
        # Built once per subnet rather than per point.
        devices = {s: self._device_block(s)
                   for s in {p.subnet for p in self.rc.points.values()}}
        # Pass 1: delete any retained config for the platform we are NOT going
        # to use. Discovery configs are retained on the broker, so a config
        # published under the other platform on a previous run still exists. It
        # carries the same unique_id, so Home Assistant keeps whichever it saw
        # first and silently ignores the new one - which leaves a writable
        # variable stuck as a read-only sensor forever after a single run with
        # read_only enabled. An empty retained payload deletes it.
        #
        # This is done as a separate pass, with a pause, so HA removes the old
        # entity before the replacement arrives. Interleaving them races.
        stale = 0
        for p in self.rc.points.values():
            uid = f"{self.device_id}_{slug(p.name)}"
            other = "sensor" if self.writable(p) else "number"
            self.mq.publish(f"{DISCOVERY_PREFIX}/{other}/{uid}/config",
                            "", retain=True)
            stale += 1
        if stale:
            LOG.debug("[%s] cleared %d stale discovery config(s)",
                      self.display_name, stale)
            time.sleep(1.0)

        # Pass 2: publish the configs we actually want.
        count = 0
        for p in self.rc.points.values():
            uid = f"{self.device_id}_{slug(p.name)}"
            writable = self.writable(p)
            platform = "number" if writable else "sensor"

            cfg = {
                "name": p.name,
                "unique_id": uid,
                # object_id drives entity_id. Without the panel slug, two
                # panels that both have Main-Var001-Setpoint would collide and
                # HA would append _2, _3 in arbitrary startup order.
                "object_id": f"{self.id_prefix}_{slug(p.name)}",
                "state_topic": self.state_topic(p),
                "availability_topic": self.avail_topic,
                # unique_id above stays panel-based deliberately. Only the
                # device block varies by subnet, so existing entities are
                # re-parented on the next discovery rather than recreated as
                # duplicates under new ids.
                "device": devices[p.subnet],
            }
            if p.label:
                unit = p.label.decode("latin-1").strip()
                if unit:
                    cfg["unit_of_measurement"] = unit
            if writable:
                cfg.update({
                    "command_topic": self.command_topic(p),
                    "min": self.cfg.min_value,
                    "max": self.cfg.max_value,
                    "step": self.cfg.step,
                    "mode": "box",
                })
                self._cmd_index[self.command_topic(p)] = p.name
                self.mq.subscribe(self.command_topic(p))
            else:
                cfg["state_class"] = "measurement"

            self.mq.publish(f"{DISCOVERY_PREFIX}/{platform}/{uid}/config",
                            json.dumps(cfg), retain=True)
            count += 1
        return count

    def publish_states(self, values: Dict[str, float]) -> None:
        for name, val in values.items():
            p = self.rc.points.get(name)
            if p is not None:
                self.mq.publish(self.state_topic(p), f"{val:g}", retain=True)

    def set_online(self, online: bool) -> None:
        if online != self.online:
            self.online = online
            self.mq.publish(self.avail_topic, "online" if online else "offline",
                            retain=True)
            LOG.info("[%s] %s", self.display_name, "online" if online else "OFFLINE")

    def report_poll(self, got_values: bool) -> None:
        """
        Online the moment anything is read; offline once nothing has been read
        for stale_after seconds. One dropped datagram never flaps HA, but a
        genuinely dead panel is reported within a predictable window.
        """
        now = time.time()
        if got_values:
            self.last_success = now
            self.set_online(True)
            return
        stale = now - self.last_success
        if stale > self.stale_after:
            self.set_online(False)
        else:
            LOG.debug("[%s] no data for %.0fs (offline at %.0fs)",
                      self.display_name, stale, self.stale_after)

    # -- writes -------------------------------------------------------------

    def handle_command(self, topic: str, payload: bytes) -> bool:
        """
        Claim and enqueue a write. Returns True if this panel owned the topic.

        Must stay fast and non-blocking: this runs on paho's network thread.
        """
        name = self._cmd_index.get(topic)
        if name is None:
            return False
        try:
            value = float(payload.decode())
        except (ValueError, UnicodeDecodeError):
            LOG.warning("[%s] bad payload on %s: %r", self.display_name, topic, payload)
            return True
        self.write_queue.put((name, value))

        # Optimistic echo. The datagram reaches the board within a fraction of a
        # second; the delay the user actually sees is HA's number box snapping
        # back to the old value while we wait for the read-back. Publishing the
        # requested value now makes the UI feel instant, and the read-back a
        # moment later corrects it if the panel rejected the write.
        target = self.rc.points.get(name)
        if target is not None:
            self.mq.publish(self.state_topic(target), f"{value:g}", retain=True)

        self.write_event.set()   # wake the panel thread now
        LOG.info("[%s] queued write %s = %s", self.display_name, name, value)
        return True

    def sweep(self, banks, only: Optional[set] = None) -> bool:
        """
        Read the given banks, publishing as each one returns.

        The lock is taken per request rather than for the whole sweep, so writes
        interleave. 'only' restricts which point names get published, used by
        the fast schedule to avoid re-publishing a whole bank every few seconds.
        """
        got_any = False
        for ptype, subnet, bank in banks:
            if self.stop.is_set():
                break
            if self.write_event.is_set():
                self.drain_writes()
            with self.lock:
                points = self.rc.read_bank(ptype, subnet, bank)
            if not points:
                continue
            got_any = True
            updates = {}
            for pt in points:
                known = self.rc.points.get(pt.name)
                if known is None:
                    continue
                known.value = pt.value
                if only is None or pt.name in only:
                    updates[pt.name] = pt.value
            if updates:
                self.publish_states(updates)
        return got_any

    def drain_writes(self) -> int:
        """
        Perform any queued writes. Called from the panel's own thread between
        bank reads, so a setpoint change lands within about a second even
        though a full poll sweep takes far longer.
        """
        self.write_event.clear()
        done = 0
        while not self.stop.is_set():
            try:
                name, value = self.write_queue.get_nowait()
            except queue.Empty:
                break
            target = self.rc.points.get(name)
            if target is None:
                LOG.warning("[%s] queued write for unknown point %s",
                            self.display_name, name)
                continue
            try:
                with self.lock:
                    self.rc.write_variable(name, value)
                    time.sleep(0.25)   # let the panel apply it before re-reading
                    # Report what the panel says, not what we asked for, so a
                    # rejected write shows the old value instead of pretending.
                    for chk in self.rc.read_bank(target.ptype, target.subnet,
                                                 target.bank):
                        if chk.name == name:
                            target.value = chk.value
                            break
                self.mq.publish(self.state_topic(target),
                                f"{target.value:g}", retain=True)
                ok = abs(target.value - value) < 1e-3
                LOG.info("[%s] wrote %s = %s, panel reports %s%s",
                         self.display_name, name, value, target.value,
                         "" if ok else "  <- MISMATCH, write may have been rejected")
                done += 1
            except rcp.RCPError as e:
                LOG.error("[%s] write failed: %s", self.display_name, e)
        return done

    # -- thread body --------------------------------------------------------

    def run(self, delay: float) -> None:
        self.stop.wait(delay)
        backoff = 5.0
        next_full = 0.0
        next_fast = 0.0

        while not self.stop.is_set():
            # ---- discover (once, retried on failure) ----
            if not self.discovered:
                try:
                    with self.lock:
                        points = self.rc.discover(num_banks=self.cfg.banks,
                                                  subnet_mode=self.cfg.subnet_mode)
                    if not points:
                        raise rcp.NoResponse("no points returned")
                    self.discovered = True
                    backoff = 5.0
                    self.compute_fast_set()
                    n = self.publish_discovery()
                    self.last_success = time.time()
                    self.set_online(True)
                    LOG.info("[%s] panel '%s': %d points, %d entities",
                             self.display_name, self.rc.panel_name, len(points), n)
                    with self.lock:
                        self.publish_states({k: p.value for k, p in points.items()})
                except (rcp.RCPError, OSError) as e:
                    self.set_online(False)
                    LOG.warning("[%s] discovery failed (%s); retry in %.0fs",
                                self.display_name, e, backoff)
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 300.0)
                    continue

            # ---- poll ----
            # Wait in small slices, servicing writes, so a setpoint change is
            # not stuck behind the whole poll_interval.
            # Two schedules: a small fast set and the full sweep. Wait until
            # whichever is due next, waking immediately for writes.
            fast_due = False
            while not self.stop.is_set():
                now = time.time()
                if self.fast_banks and now >= next_fast:
                    fast_due = True
                    next_fast = now + self.cfg.fast_interval
                    break
                if now >= next_full:
                    next_full = now + self.cfg.poll_interval
                    break
                horizon = next_full if not self.fast_banks else min(next_full, next_fast)
                if self.write_event.wait(max(0.05, min(horizon - now, 1.0))):
                    self.drain_writes()
            if self.stop.is_set():
                break

            try:
                if fast_due:
                    got_any = self.sweep(self.fast_banks, only=self.fast_names)
                else:
                    got_any = self.sweep(self.rc.bank_list(self.cfg.banks))
                self.drain_writes()
                self.report_poll(got_any)
            except (rcp.RCPError, OSError) as e:
                LOG.warning("[%s] poll error: %s", self.display_name, e)
                self.report_poll(False)

        self.set_online(False)
        self.rc.close()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.panels: List[Panel] = []
        self._warned_same_board = False

        client_id = f"{BASE}_bridge_{int(time.time())}"
        # paho 2.x wants an explicit callback API version; 1.x has no such
        # argument. Pin VERSION1 so the existing on_* signatures stay correct.
        try:
            self.mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                  client_id=client_id)
        except AttributeError:
            self.mq = mqtt.Client(client_id=client_id)
        if cfg.mqtt_user:
            self.mq.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass or None)
        self.mq.on_connect = self._on_connect
        self.mq.on_message = self._on_message

        for spec in cfg.panels:
            self.panels.append(Panel(spec.get("name", ""), spec["host"],
                                     int(spec.get("controller", 1)),
                                     cfg, self.mq, self.stop))
        self._assign_id_prefixes()

    def _assign_id_prefixes(self) -> None:
        """
        Use the panel name for entity_ids when every name is present and
        unique. With twenty boards that's the difference between
        sensor.pool_house_main_in001_pooltemp and an id starting with an IP,
        which matters a lot for recorder globs and dashboard templating.
        Any duplicate or blank name falls the whole set back to host-based ids
        so behaviour stays predictable rather than half-and-half.
        """
        names = [slug(p.display_name) for p in self.panels]
        if all(names) and len(set(names)) == len(names):
            for panel, name in zip(self.panels, names):
                panel.id_prefix = name
            LOG.info("entity ids use panel names, e.g. '%s_...'", names[0])
        else:
            LOG.warning("panel names are blank or duplicated; entity ids will "
                        "use host_controller instead. Give every panel a unique "
                        "name for readable entity ids.")

    def _on_connect(self, client, userdata, flags, rc_code):
        if rc_code != 0:
            LOG.error("MQTT connect failed, code %s", rc_code)
            return
        LOG.info("MQTT connected to %s:%d", self.cfg.mqtt_host, self.cfg.mqtt_port)
        # Re-subscribe after a reconnect; discovery may already have run.
        for panel in self.panels:
            for p in panel.rc.points.values():
                if panel.writable(p):
                    client.subscribe(panel.command_topic(p))

    def _on_message(self, client, userdata, msg):
        # Discovery configs are never commands. A stray subscription used to
        # funnel thousands of them here, one warning each.
        if msg.topic.startswith(DISCOVERY_PREFIX + "/"):
            return
        for panel in self.panels:
            if panel.handle_command(msg.topic, msg.payload):
                return
        LOG.warning("command on unclaimed topic %s", msg.topic)

    def purge_discovery(self, settle: float = 3.0, cap: float = 90.0) -> int:
        """
        Delete every retained discovery config in this bridge's namespace.

        Deliberately matches on the "reliable_" prefix rather than on the
        currently-configured panels. Orphans are, by definition, configs whose
        panel is no longer configured - a host that was renumbered, a board
        removed from the list. Scoping the delete to current panels means the
        orphans are the exact set that never gets cleaned, which is the bug this
        method existed to fix and did not.

        Collection runs until the broker stops delivering for 'settle' seconds
        rather than for a fixed window; thousands of retained messages do not
        arrive in six seconds.
        """
        found: Dict[str, bytes] = {}
        last_msg = [time.time()]

        def on_msg(client, userdata, msg):
            if msg.payload:
                found[msg.topic] = msg.payload
            last_msg[0] = time.time()

        prev = self.mq.on_message
        self.mq.on_message = on_msg
        for platform in ("sensor", "number"):
            self.mq.subscribe(f"{DISCOVERY_PREFIX}/{platform}/+/config")

        LOG.info("purge: collecting retained configs (until quiet for %.0fs, "
                 "max %.0fs)...", settle, cap)
        deadline = time.time() + cap
        while time.time() < deadline:
            time.sleep(0.25)
            if time.time() - last_msg[0] > settle and found:
                break
        LOG.info("purge: collected %d retained config(s)", len(found))

        # Unsubscribe BEFORE restoring the real handler. The other order lets
        # late retained messages - and the echoes of our own deletions - land in
        # _on_message, which logged a warning for every one of them. That both
        # floods the log and blocks paho's network thread.
        for platform in ("sensor", "number"):
            self.mq.unsubscribe(f"{DISCOVERY_PREFIX}/{platform}/+/config")
        time.sleep(1.0)
        self.mq.on_message = prev

        deleted = kept = 0
        hosts: Dict[str, int] = {}
        for topic, payload in found.items():
            try:
                cfg = json.loads(payload)
            except (ValueError, TypeError):
                kept += 1
                continue
            uid = str(cfg.get("unique_id", ""))
            ids = [str(i) for i in (cfg.get("device", {}).get("identifiers") or [])]
            mine = uid.startswith(f"{BASE}_") or any(i.startswith(f"{BASE}_") for i in ids)
            if not mine:
                kept += 1
                continue
            # Tally by device prefix so the log names which hosts were cleaned.
            key = "_".join((ids[0] if ids else uid).split("_")[:6])
            hosts[key] = hosts.get(key, 0) + 1
            self.mq.publish(topic, "", retain=True)
            deleted += 1

        LOG.info("purge: deleted %d config(s) in the '%s_' namespace, left %d "
                 "belonging to other integrations", deleted, BASE, kept)
        for key, n in sorted(hosts.items()):
            LOG.info("purge:   %-40s %5d config(s)", key, n)
        if deleted:
            LOG.info("purge: waiting 8s for Home Assistant to process removals")
            time.sleep(8.0)
        return deleted

    @staticmethod
    def log_version() -> None:
        """
        Fingerprint the running code.

        Version alone is not enough - the number lives in the source file, and a
        stale container has a stale copy of that file too. The modification time
        and content hash of the actually-loaded module are what prove whether the
        image was rebuilt.
        """
        try:
            path = os.path.abspath(__file__)
            mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(os.path.getmtime(path)))
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:12]
            with open(os.path.abspath(rcp.__file__), "rb") as fh:
                rcp_digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            mtime = digest = rcp_digest = "unknown"

        LOG.info("=" * 68)
        LOG.info("Reliable Controls MACH Bridge  v%s", __version__)
        LOG.info("  bridge file : %s  (sha %s)", mtime, digest)
        LOG.info("  rcp module  : sha %s", rcp_digest)
        LOG.info("  If this version is not the one you just installed, the")
        LOG.info("  container was NOT rebuilt. Uninstall the app and install")
        LOG.info("  it again - Restart alone reuses the old image.")
        LOG.info("=" * 68)

    def log_inventory(self) -> None:
        """
        Print every device identifier this bridge publishes, so "why does HA
        show more devices than I configured" is answerable without guessing.
        """
        LOG.info("=" * 68)
        LOG.info("DEVICE INVENTORY - what this bridge publishes")
        LOG.info("=" * 68)
        total = 0
        for panel in self.panels:
            if not panel.discovered:
                LOG.info("  %-24s (not yet discovered)", panel.display_name)
                continue
            for subnet in sorted({p.subnet for p in panel.rc.points.values()}):
                dev = panel._device_block(subnet)
                n = sum(1 for p in panel.rc.points.values() if p.subnet == subnet)
                LOG.info("  %-28s %-34s %3d entities",
                         dev["name"], dev["identifiers"][0], n)
                total += 1
        LOG.info("-" * 68)
        LOG.info("  %d device(s), %d entit(ies) from %d configured panel(s)",
                 total, sum(len(p.rc.points) for p in self.panels), len(self.panels))
        LOG.info("  If Home Assistant shows more devices than the number above,")
        LOG.info("  the extra ones are retained MQTT configs, not live output.")
        LOG.info("  Run once with purge_on_start: true to clear them.")
        LOG.info("=" * 68)

    def _warn_same_board(self) -> None:
        """
        Flag config entries that reach the same physical controller by two
        addresses - each publishes a complete duplicate set.
        """
        if self._warned_same_board:
            return
        seen: Dict[tuple, str] = {}
        for panel in self.panels:
            if not panel.discovered or not panel.rc.points:
                continue
            fingerprint = (panel.rc.panel_name, frozenset(panel.rc.points.keys()))
            prior = seen.get(fingerprint)
            if prior:
                LOG.warning("=" * 68)
                LOG.warning("'%s' and '%s' returned IDENTICAL points and the "
                            "same panel name.", prior, panel.display_name)
                LOG.warning("They are almost certainly the SAME physical "
                            "controller reached by two addresses, so every "
                            "entity is being published twice.")
                LOG.warning("Remove one of them from the panels list, then run "
                            "once with purge_on_start: true.")
                LOG.warning("=" * 68)
                self._warned_same_board = True
            else:
                seen[fingerprint] = panel.display_name

    def run(self) -> int:
        self.log_version()
        LOG.info("%d panel(s), banks=%d, interval=%ss, subnet_mode=%s, %s",
                 len(self.panels), self.cfg.banks, self.cfg.poll_interval,
                 self.cfg.subnet_mode,
                 "READ-ONLY" if self.cfg.read_only else "WRITE ENABLED")

        self.mq.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.mq.loop_start()
        time.sleep(1.0)

        if self.cfg.purge_on_start:
            self.purge_discovery()

        # One thread per panel. Different IPs mean no contention, so twenty
        # panels take about as long per cycle as one.
        for i, panel in enumerate(self.panels):
            t = threading.Thread(target=panel.run, args=(i * STAGGER_SECONDS,),
                                 name=f"panel-{panel.slug}", daemon=True)
            panel.thread = t
            t.start()

        # Wait for discovery to actually finish before printing the inventory.
        # A panel with 30 sub-controllers takes around a minute to enumerate, so
        # a fixed short delay printed "(not yet discovered)" and told us nothing.
        deadline = time.time() + 600.0
        while not self.stop.is_set() and time.time() < deadline:
            if all(p.discovered for p in self.panels):
                break
            self.stop.wait(2.0)
        if not self.stop.is_set():
            self.log_inventory()

        try:
            while not self.stop.is_set():
                self.stop.wait(30.0)
                if self.stop.is_set():
                    break
                up = sum(1 for p in self.panels if p.online)
                total = sum(len(p.rc.points) for p in self.panels)
                LOG.info("status: %d/%d panels online, %d points",
                         up, len(self.panels), total)
                self._warn_same_board()
        finally:
            LOG.info("shutting down")
            self.stop.set()
            for panel in self.panels:
                if panel.thread:
                    panel.thread.join(timeout=5.0)
            time.sleep(0.3)
            self.mq.loop_stop()
            self.mq.disconnect()
        return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Merged view of JSON file and command line. CLI wins."""

    def __init__(self, args):
        data = {}
        if args.config:
            with open(args.config, encoding="utf-8") as fh:
                data = json.load(fh)

        if args.host:
            self.panels = [{"name": args.name or "", "host": args.host,
                            "controller": args.controller}]
        else:
            self.panels = data.get("panels") or []
        if not self.panels:
            raise SystemExit("No panels configured. Use --host or a "
                             "'panels' list in the JSON config.")

        seen = set()
        for p in self.panels:
            if "host" not in p:
                raise SystemExit(f"panel entry missing 'host': {p}")
            key = (p["host"], int(p.get("controller", 1)))
            if key in seen:
                raise SystemExit(f"duplicate panel {key[0]} controller {key[1]}")
            seen.add(key)

        def pick(name, cli, default):
            return cli if cli is not None else data.get(name, default)

        self.banks = int(pick("banks", args.banks, rcp.NUM_BANKS))
        self.poll_interval = float(pick("poll_interval", args.interval, 15.0))
        self.read_only = bool(pick("read_only", args.read_only or None, True))
        self.min_value = float(pick("min_value", args.min, -10000.0))
        self.max_value = float(pick("max_value", args.max, 10000.0))
        self.step = float(pick("step", args.step, 0.1))
        self.purge_on_start = bool(pick("purge_on_start",
                                        True if args.purge else None, False))
        self.split_subnets = bool(pick("split_subnets",
                                       True if args.split_subnets else None, True))
        self.fast_points = pick("fast_points", args.fast_point or None, []) or []
        self.fast_interval = float(pick("fast_interval", args.fast_interval, 10.0))
        self.subnet_mode = str(pick("subnet_mode", args.subnet_mode, "auto"))
        if self.subnet_mode not in ("auto", "bitmap", "probe"):
            raise SystemExit(f"bad subnet_mode {self.subnet_mode!r}; use auto, bitmap or probe")

        self.mqtt_host = args.mqtt_host or data.get("mqtt_host") or ""
        self.mqtt_port = int(args.mqtt_port or data.get("mqtt_port") or 1883)
        self.mqtt_user = args.mqtt_user or data.get("mqtt_user") or ""
        self.mqtt_pass = args.mqtt_pass or data.get("mqtt_pass") or ""
        if not self.mqtt_host:
            raise SystemExit("No MQTT broker. Set --mqtt-host or mqtt_host.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON config with a 'panels' list")

    ap.add_argument("--host", help="single-panel mode: controller IP")
    ap.add_argument("--name", help="friendly name for --host")
    ap.add_argument("--controller", type=int, default=1)

    ap.add_argument("--banks", type=int)
    ap.add_argument("--interval", type=float)
    ap.add_argument("--read-only", action="store_true")
    ap.add_argument("--min", type=float)
    ap.add_argument("--max", type=float)
    ap.add_argument("--step", type=float)
    ap.add_argument("--fast-point", action="append", metavar="PATTERN",
                    help="point name substring or glob to poll on the fast "
                         "schedule; repeatable")
    ap.add_argument("--fast-interval", type=float,
                    help="seconds between fast-set polls (default 10)")
    ap.add_argument("--purge", action="store_true",
                    help="delete all retained discovery configs before starting")
    ap.add_argument("--split-subnets", action="store_true",
                    help="give each sub-controller its own HA device (default on)")
    ap.add_argument("--subnet-mode", choices=("auto", "bitmap", "probe"),
                    help="how to find sub-controllers behind each panel")

    ap.add_argument("--mqtt-host")
    ap.add_argument("--mqtt-port", type=int)
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-pass")

    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if args.verbose >= 2:
        logging.getLogger("rcp").setLevel(logging.DEBUG)

    bridge = Bridge(Config(args))
    signal.signal(signal.SIGINT, lambda s, f: bridge.stop.set())
    signal.signal(signal.SIGTERM, lambda s, f: bridge.stop.set())
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
