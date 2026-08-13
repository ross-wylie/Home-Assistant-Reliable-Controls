#!/usr/bin/env python3
"""
rc_mock_panel.py - a fake Reliable Controls MACH panel, so you can test the
                   Home Assistant side without touching the real pool.

Speaks the same UDP protocol as the real thing: answers system status, subnet
scan, and bank reads, and accepts variable writes (command 120), remembering
what you wrote so a re-read reflects it.

This exists to split one hard problem into two easy ones. If entities appear
in HA when pointed at this, your MQTT and HA config are correct, and any
remaining failure is network or panel. If they don't appear, stop looking at
the controller.

    # terminal 1
    python3 rc_mock_panel.py

    # terminal 2
    python3 rcp.py discover --host 127.0.0.1 --controller 1 --bind-port 0

    # terminal 3 - the real test
    python3 rc_mqtt_bridge.py --host 127.0.0.1 --controller 1 --bind-port 0 \
        --mqtt-host <your-broker>

Stdlib only.
"""

from __future__ import annotations

import argparse
import logging
import socket
import struct
import sys
from typing import Dict, List, Tuple

LOG = logging.getLogger("mock")

PORT_BYTE = 0x45
PANEL_NAME = b"PoolHouse"

# Synthetic points, deliberately pool-flavoured so the output is recognisable.
# (description, value, label) - variables also get aaoiu/range/prgctrl.
VARIABLES: List[Tuple[bytes, float, bytes]] = [
    (b"PoolSetpoint", 82.0, b"degF"),
    (b"SpaSetpoint", 102.0, b"degF"),
    (b"PumpSpeed", 65.0, b"pct"),
    (b"Pool Mode!", 1.0, b""),          # exercises the character sanitiser
]
INPUTS: List[Tuple[bytes, float, bytes]] = [
    (b"PoolTemp", 79.4, b"degF"),
    (b"SpaTemp", 99.1, b"degF"),
    (b"AirTemp", 71.2, b"degF"),
    (b"FlowSwitch", 1.0, b""),
]
OUTPUTS: List[Tuple[bytes, float, bytes]] = [
    (b"PoolPump", 1.0, b""),
    (b"SpaHeater", 0.0, b""),
    (b"BoosterPump", 0.0, b""),
]

TYPE_TABLE = {
    1: (OUTPUTS, 42, 21),   # command 1 -> Outputs
    2: (INPUTS, 38, 21),    # command 2 -> Inputs
    3: (VARIABLES, 38, 22),  # command 3 -> Variables
}


def build_frame(to: int, frm: int, cmd: int, extra_l: int, extra_h: int,
                subctrl: int, data: bytes) -> bytes:
    h = bytearray(12)
    h[0], h[1], h[2] = to & 0xFF, frm & 0xFF, cmd & 0xFF
    h[3], h[4], h[5] = extra_l & 0xFF, extra_h & 0xFF, subctrl & 0xFF
    h[6] = sum(h[0:6]) & 0xFF
    h[7] = PORT_BYTE
    struct.pack_into("<H", h, 10, len(data))
    return bytes(h) + data


class MockPanel:
    def __init__(self, controller: int, subnets: List[int]):
        self.controller = controller
        self.subnets = subnets
        # (subnet, ptype_cmd, address) -> overridden value, from writes
        self.written: Dict[Tuple[int, int, int], float] = {}

    # -- payload builders ---------------------------------------------------

    def sysstatus(self) -> bytes:
        """26 bytes per panel; name at +4. Pad up to our controller index."""
        panels = max(self.controller, 1)
        buf = bytearray(26 * panels)
        base = (self.controller - 1) * 26
        buf[base + 4:base + 4 + len(PANEL_NAME)] = PANEL_NAME
        return bytes(buf)

    def subnet_scan(self) -> bytes:
        """6 bytes per subnet, bit 0 = active. Subnet 0 is implicit."""
        buf = bytearray(6 * 62)
        for n in self.subnets:
            if n == 0:
                continue
            buf[(n - 1) * 6] |= 0x01
        return bytes(buf)

    def bank(self, cmd: int, bank_no: int, subnet: int) -> bytes:
        table, rec_len, _desc_len = TYPE_TABLE[cmd]
        # Only bank 0 is populated; higher banks come back empty, which is
        # exactly how a real panel signals "nothing here".
        if bank_no != 0:
            return b""

        buf = bytearray(rec_len * len(table))
        for i, (desc, value, label) in enumerate(table):
            base = i * rec_len
            buf[base:base + len(desc)] = desc
            addr = i + 1
            val = self.written.get((subnet, cmd, addr), value)
            struct.pack_into("<f", buf, base + 22, val)
            if cmd == 3:  # variables carry the extra trailing bytes
                buf[base + 26] = 0xA1              # aaoiu
                buf[base + 27] = 0x04              # range
                buf[base + 28:base + 28 + len(label)] = label[:9]
                buf[base + 37] = 0x02              # prgctrl
        return bytes(buf)

    # -- request handling ---------------------------------------------------

    def handle(self, buf: bytes) -> bytes | None:
        if len(buf) < 12:
            LOG.warning("runt frame, %d bytes", len(buf))
            return None

        to, frm, cmd, extra_l, extra_h, subctrl, chk, port = struct.unpack_from("<8B", buf, 0)
        expect = (to + frm + cmd + extra_l + extra_h + subctrl) & 0xFF
        if chk != expect:
            LOG.warning("BAD CHECKSUM: got %d want %d - a real panel would "
                        "ignore this frame", chk, expect)
            return None
        if port != PORT_BYTE:
            LOG.warning("port byte is 0x%02X, expected 0x45", port)

        subnet = subctrl

        if cmd == 12:
            LOG.info("<- system status")
            return build_frame(0, self.controller, 112, 0, 0, 0, self.sysstatus())

        if cmd == 50 and extra_h == 8:
            LOG.info("<- subnet scan (active: %s)",
                     ",".join(str(s) for s in self.subnets))
            return build_frame(0, self.controller, 108, 0, 8, 0, self.subnet_scan())

        if cmd in TYPE_TABLE:
            names = {1: "outputs", 2: "inputs", 3: "variables"}
            body = self.bank(cmd, extra_l, subnet)
            LOG.info("<- %s bank %d subnet %d (%d bytes)",
                     names[cmd], extra_l, subnet, len(body))
            return build_frame(0, self.controller, cmd + 100, extra_l, 0, subnet, body)

        if cmd == 20:
            # Individual point read. Unpack the address, return one 38-byte
            # variable record if that address exists.
            packed = extra_l | (extra_h << 8)
            addr = self._unpack_addr(packed, subnet)
            if 1 <= addr <= len(VARIABLES):
                desc, value, label = VARIABLES[addr - 1]
                val = self.written.get((subnet, 3, addr), value)
                rec = bytearray(38)
                rec[0:len(desc)] = desc
                struct.pack_into("<f", rec, 22, val)
                rec[26] = 0xA1
                rec[27] = 0x04
                rec[28:28 + len(label[:9])] = label[:9]
                rec[37] = 0x02
                LOG.info("<- individual point addr=%d subnet=%d (%s=%g)",
                         addr, subnet, desc.decode("latin-1"), val)
                return build_frame(0, self.controller, 120, extra_l, extra_h,
                                   subnet, bytes(rec))
            LOG.info("<- individual point addr=%d subnet=%d: no such variable",
                     addr, subnet)
            return build_frame(0, self.controller, 120, extra_l, extra_h,
                               subnet, bytes(38))

        if cmd == 120:
            packed = extra_l | (extra_h << 8)
            payload = buf[12:]
            if len(payload) < 38:
                LOG.warning("write payload only %d bytes, expected 38", len(payload))
                return None
            (value,) = struct.unpack_from("<f", payload, 22)
            desc = payload[0:22].split(b"\x00")[0]

            # Unpack the address the same way the client packed it.
            if subnet == 0:
                number = packed & 0x7F
                tbits = (packed >> 7) & 0x0F
                ctrl = (packed >> 11) & 0x1F
                addr = number + 1
                LOG.info("-> WRITE main ctrl=%d type=%d addr=%d  %s = %g",
                         ctrl, tbits, addr, desc.decode("latin-1"), value)
            else:
                ctrl = packed & 0x7F
                tbits = (packed >> 7) & 0x0F
                number = (packed >> 11) & 0x1F
                base = {3: 0, 13: 32, 14: 64, 15: 96}.get(tbits, 0)
                addr = base + number + 1
                LOG.info("-> WRITE subnet=%d type=%d addr=%d  %s = %g",
                         ctrl, tbits, addr, desc.decode("latin-1"), value)

            LOG.info("   echoed bytes: aaoiu=0x%02X range=0x%02X label=%r prgctrl=0x%02X",
                     payload[26], payload[27],
                     payload[28:37].split(b"\x00")[0].decode("latin-1"), payload[37])
            self.written[(subnet, 3, addr)] = value
            return None  # real panel sends no acknowledgement

        LOG.warning("unhandled command %d", cmd)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=21068)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="0.0.0.0 to accept from other hosts")
    ap.add_argument("--controller", type=int, default=1)
    ap.add_argument("--subnets", default="0",
                    help="comma-separated active subnets, e.g. 0,5")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    subnets = sorted({int(s) for s in args.subnets.split(",") if s.strip() != ""})
    panel = MockPanel(args.controller, subnets)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.bind, args.port))
    except OSError as e:
        print(f"cannot bind {args.bind}:{args.port} - {e}")
        return 1

    LOG.info("mock MACH panel on %s:%d  controller=%d subnets=%s",
             args.bind, args.port, args.controller, subnets)
    LOG.info("%d variables, %d inputs, %d outputs in bank 0",
             len(VARIABLES), len(INPUTS), len(OUTPUTS))

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            LOG.debug("from %s: %s", addr, data[:16].hex(" "))
            reply = panel.handle(data)
            if reply:
                sock.sendto(reply, addr)
    except KeyboardInterrupt:
        LOG.info("stopped")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
