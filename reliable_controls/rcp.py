#!/usr/bin/env python3
"""
rcp.py - Reliable Controls MACH protocol client (UDP 21068).

Python port of the CQC CML driver
    MEng.User.CQC.Drivers.ReliableControls.Mach.Driver
by Mark Stega (2007-2010). All wire-format knowledge here comes from that
driver's source; this is a reimplementation, not a wrapper.

    Transport : UDP, remote port 21068
    Framing   : 12-byte header + optional payload
    Values    : IEEE Float4, little-endian
    Writable  : Variables only. Inputs and Outputs are read-only.

CLI
    python3 rcp.py discover --host 10.83.106.161 --controller 1
    python3 rcp.py watch    --host 10.83.106.161 --controller 1
    python3 rcp.py write    --host 10.83.106.161 --controller 1 \
                            --field Main-Var003-SpaSetpoint --value 102.0

Stdlib only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterator, List, Optional, Tuple

LOG = logging.getLogger("rcp")

# ---------------------------------------------------------------------------
# Protocol constants (from the CML Literals blocks)
# ---------------------------------------------------------------------------

RCP_PORT = 21068
HEADER_LEN = 12
PORT_BYTE = 0x45  # kProtocol_MACH_PORT, constant in every frame the driver sends

# Header byte offsets
OFF_TO, OFF_FROM, OFF_COMMAND = 0, 1, 2
OFF_EXTRAL, OFF_EXTRAH, OFF_SUBCTRL = 3, 4, 5
OFF_CHECKSUM, OFF_PORT = 6, 7
OFF_RESERVED1, OFF_RESERVED2 = 8, 9
OFF_COUNTL, OFF_COUNTH = 10, 11
OFF_DATA = 12

# Request commands
CMD_OUTPUTS = 1
CMD_INPUTS = 2
CMD_VARIABLES = 3
CMD_SYSSTATUS = 12
CMD_POINT_READ = 20
CMD_POINT_WRITE = 120  # kProtocol_Command_IndividualPoint + 100
CMD_FIFTY = 50
FIFTY_SUBNET_STATUS = 8  # goes in ExtraH

# Responses are request command + 100
RESP_OFFSET = 100

# Point types (Points.kProto_*)
TYPE_OUTPUTS = 0
TYPE_INPUTS = 1
TYPE_VARIABLES = 2

TYPE_NAMES = {TYPE_OUTPUTS: "Outputs", TYPE_INPUTS: "Inputs", TYPE_VARIABLES: "Variables"}
TYPE_PREFIX = {TYPE_OUTPUTS: "Out", TYPE_INPUTS: "In", TYPE_VARIABLES: "Var"}
TYPE_FOR_COMMAND = {CMD_OUTPUTS: TYPE_OUTPUTS, CMD_INPUTS: TYPE_INPUTS, CMD_VARIABLES: TYPE_VARIABLES}
COMMAND_FOR_TYPE = {v: k for k, v in TYPE_FOR_COMMAND.items()}

# Record geometry, per type: (record_size, description_length)
RECORD_SPEC = {
    TYPE_OUTPUTS: (42, 21),
    TYPE_INPUTS: (38, 21),
    TYPE_VARIABLES: (38, 22),
}
VALUE_OFFSET = 22  # Float4 at record + 22, all three types

# Variable-only trailing fields, relative to record start.
# Derived in CML as DescriptionLength(22) + 4 (+1 ...), i.e. fixed offsets.
VAR_OFF_AAOIU = 26
VAR_OFF_RANGE = 27
VAR_OFF_LABEL = 28
VAR_LABEL_MAX = 9
VAR_OFF_PRGCTRL = 37
VAR_RECORD_LEN = 38

MAX_SUBNETS = 63          # kProto_MaxAllowedSubNetworks -> subnets 0..62
SUBNET_SCAN_STRIDE = 6    # 6 bytes per subnet controller in the cmd-50 reply
SUBNET_SCAN_COUNT = 62
SYSSTATUS_STRIDE = 26     # 26 bytes per panel
SYSSTATUS_NAME_OFF = 4
SYSSTATUS_NAME_MAX = 18

# The shipped driver polls only banks 0..1 (kProto_NumBanksStandard was
# reduced from 4 to 2 on 11JAN2008). Raise NUM_BANKS to see more.
NUM_BANKS = 2
MAX_BANKS_PROTOCOL = 7

RECV_TIMEOUT_1 = 1.5   # first attempt, matches the CML
RECV_TIMEOUT_2 = 3.0   # retry for slower high-address controllers
RECV_BUFFER = 4096

WRITE_MAX_CONTROLLER = 31  # protocol restriction noted in the CML header


class RCPError(Exception):
    pass


class BadChecksum(RCPError):
    pass


class NoResponse(RCPError):
    pass


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def build_frame(to: int, cmd: int, extra_l: int = 0, extra_h: int = 0,
                subctrl: int = 0, data: bytes = b"", frm: int = 0) -> bytes:
    """
    Assemble a request. Checksum is the low byte of the sum of header bytes
    0..5 only - it does NOT cover Port, Reserved, Count, or the payload.
    """
    h = bytearray(HEADER_LEN)
    h[OFF_TO] = to & 0xFF
    h[OFF_FROM] = frm & 0xFF
    h[OFF_COMMAND] = cmd & 0xFF
    h[OFF_EXTRAL] = extra_l & 0xFF
    h[OFF_EXTRAH] = extra_h & 0xFF
    h[OFF_SUBCTRL] = subctrl & 0xFF
    h[OFF_CHECKSUM] = sum(h[0:6]) & 0xFF
    h[OFF_PORT] = PORT_BYTE
    h[OFF_RESERVED1] = 0
    h[OFF_RESERVED2] = 0
    # The driver writes the count as a low byte with high byte hard-zero.
    # Payloads are at most 38 bytes, so this is safe.
    h[OFF_COUNTL] = len(data) & 0xFF
    h[OFF_COUNTH] = 0
    return bytes(h) + data


@dataclass
class Frame:
    to: int
    frm: int
    command: int
    extra_l: int
    extra_h: int
    subctrl: int
    checksum: int
    count: int
    data: bytes
    raw: bytes

    @property
    def response_type(self) -> int:
        """Command with the +100 response offset removed."""
        return self.command - RESP_OFFSET

    @property
    def bank(self) -> int:
        return self.extra_l


def parse_frame(buf: bytes) -> Frame:
    if len(buf) < HEADER_LEN:
        raise RCPError(f"short frame: {len(buf)} bytes")
    to, frm, cmd, extra_l, extra_h, subctrl, chk, _port = struct.unpack_from("<8B", buf, 0)
    (count,) = struct.unpack_from("<H", buf, OFF_COUNTL)

    # Validate exactly as the CML does: sum of the six addressing bytes.
    expect = (to + frm + cmd + extra_l + extra_h + subctrl) & 0xFF
    if expect != chk:
        raise BadChecksum(f"checksum {chk} != computed {expect}")

    return Frame(to=to, frm=frm, command=cmd, extra_l=extra_l, extra_h=extra_h,
                 subctrl=subctrl, checksum=chk, count=count,
                 data=buf[OFF_DATA:], raw=buf)


# ---------------------------------------------------------------------------
# Field naming (mirrors HandleMessage_Points so names match CQC exactly)
# ---------------------------------------------------------------------------

_ILLEGAL = re.compile(rb"[^0-9A-Za-z]")


def sanitize_description(raw: bytes) -> str:
    """Anything outside [0-9A-Za-z] becomes '_', as the CML does."""
    return _ILLEGAL.sub(b"_", raw).decode("latin-1")


def read_cstring(buf: bytes, offset: int, maxlen: int) -> bytes:
    """Bytes up to the first NUL, capped at maxlen."""
    out = bytearray()
    for i in range(maxlen):
        if offset + i >= len(buf):
            break
        b = buf[offset + i]
        if b == 0:
            break
        out.append(b)
    return bytes(out)


def subnet_prefix(subnet: int) -> str:
    return "Main-" if subnet == 0 else f"A{subnet}-"


def points_in_bank(ptype: int, bank: int) -> Tuple[int, int]:
    """
    Return (points_in_this_bank, address_offset_of_bank).

    Inputs/Outputs are a flat 32 per bank. Variables hold 48 in banks 0 and 1,
    then 32 from bank 2 on, starting at address offset 96.
    """
    if ptype in (TYPE_INPUTS, TYPE_OUTPUTS):
        return 32, 32 * bank
    if bank in (0, 1):
        return 48, 48 * bank
    return 32, 96 + (bank - 2) * 32


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

@dataclass
class Point:
    ptype: int
    subnet: int
    bank: int
    offset: int          # index within the bank
    address: int         # 1-based RC address within the type
    name: str            # e.g. Main-Var003-SpaSetpoint
    description: str     # sanitized
    value: float = 0.0
    writable: bool = False

    # Variables echo these back verbatim on write; without them you cannot
    # construct a valid write frame, which is why discovery is mandatory.
    raw_description: bytes = b""
    aaoiu: int = 0
    vrange: int = 0
    label: bytes = b""
    prgctrl: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["raw_description"] = self.raw_description.decode("latin-1")
        d["label"] = self.label.decode("latin-1")
        d["type_name"] = TYPE_NAMES[self.ptype]
        return d


def parse_points(frame: Frame, ptype: int, subnet: int, bank: int) -> List[Point]:
    """Split a 101/102/103 reply into Point records."""
    rec_len, desc_len = RECORD_SPEC[ptype]
    max_points, bank_offset = points_in_bank(ptype, bank)
    body = frame.data
    out: List[Point] = []

    for idx in range(max_points):
        base = idx * rec_len
        if base + rec_len > len(body):
            break

        raw_desc = read_cstring(body, base, desc_len)
        if not raw_desc:
            continue  # empty slot; the CML skips these

        (value,) = struct.unpack_from("<f", body, base + VALUE_OFFSET)
        address = 1 + idx + bank_offset
        desc = sanitize_description(raw_desc)
        name = f"{subnet_prefix(subnet)}{TYPE_PREFIX[ptype]}{address:03d}-{desc}"

        p = Point(ptype=ptype, subnet=subnet, bank=bank, offset=idx,
                  address=address, name=name, description=desc, value=value,
                  writable=(ptype == TYPE_VARIABLES))

        if ptype == TYPE_VARIABLES:
            p.raw_description = raw_desc
            p.aaoiu = body[base + VAR_OFF_AAOIU]
            p.vrange = body[base + VAR_OFF_RANGE]
            p.label = read_cstring(body, base + VAR_OFF_LABEL, VAR_LABEL_MAX)
            p.prgctrl = body[base + VAR_OFF_PRGCTRL]

        out.append(p)

    return out


# ---------------------------------------------------------------------------
# Write address packing
# ---------------------------------------------------------------------------

def pack_point_address(address: int, subnet: int, controller: int) -> int:
    """
    Pack a variable's address into the 16-bit value that rides in ExtraL/ExtraH.

    Main network (subnet 0)   CCCCCTTTTNNNNNNN
        controller << 11 | type << 7 | (address-1) % 128
        type = 3 for addresses 1-128, else 0

    Subnet                    NNNNNTTTTCCCCCCC
        ((address-1) % 32) << 11 | type << 7 | subnet
        type = 3 / 13 / 14 / 15 for the 1-32 / 33-64 / 65-96 / 97-128 blocks
    """
    if subnet == 0:
        ptype_bits = 3 if address <= 128 else 0
        return ((controller & 0x1F) << 11) | (ptype_bits << 7) | ((address - 1) % 128)

    if address <= 32:
        ptype_bits = 3
    elif address <= 64:
        ptype_bits = 13
    elif address <= 96:
        ptype_bits = 14
    elif address <= 128:
        ptype_bits = 15
    else:
        raise RCPError(f"address {address} exceeds the 128-variable subnet limit")

    return (((address - 1) % 32) << 11) | (ptype_bits << 7) | (subnet & 0x7F)


def build_write_payload(point: Point, new_value: float) -> bytes:
    """
    38-byte variable record: the original, with only the float replaced.
    Everything else is echoed back exactly as read.
    """
    buf = bytearray(VAR_RECORD_LEN)
    buf[0:len(point.raw_description)] = point.raw_description
    struct.pack_into("<f", buf, VALUE_OFFSET, float(new_value))
    buf[VAR_OFF_AAOIU] = point.aaoiu
    buf[VAR_OFF_RANGE] = point.vrange
    buf[VAR_OFF_LABEL:VAR_OFF_LABEL + len(point.label)] = point.label
    buf[VAR_OFF_PRGCTRL] = point.prgctrl
    return bytes(buf)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MachClient:
    def __init__(self, host: str, controller: int = 1, port: int = RCP_PORT,
                 bind_port: Optional[int] = RCP_PORT, timeout: float = RECV_TIMEOUT_1):
        self.host = host
        self.port = port
        self.controller = controller
        self.timeout = timeout
        self.panel_name: str = ""
        self.subnets: List[int] = []
        self.points: Dict[str, Point] = {}

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind_port:
            # CQC binds local 21068. Matching it is safest, but it means you
            # cannot run this and the CQC driver on the same host at once.
            try:
                self.sock.bind(("", bind_port))
            except OSError as e:
                raise RCPError(
                    f"cannot bind local UDP {bind_port}: {e}. "
                    f"CQC may already hold it. Retry with --bind-port 0."
                ) from e
        self.sock.settimeout(self.timeout)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- addressing ---------------------------------------------------------

    def _dest(self, subnet: int) -> Tuple[int, int]:
        """(to, subctrl). Subnet traffic sets the high bit of the To byte."""
        if subnet == 0:
            return self.controller, 0
        return (self.controller + 128) & 0xFF, subnet

    # -- transport ----------------------------------------------------------

    def send(self, frame: bytes) -> None:
        LOG.debug("TX %s", frame.hex(" "))
        self.sock.sendto(frame, (self.host, self.port))

    def recv(self, timeout: Optional[float] = None) -> Optional[Frame]:
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            buf, _ = self.sock.recvfrom(RECV_BUFFER)
        except socket.timeout:
            return None
        except ConnectionRefusedError:
            # ICMP port-unreachable came back: nothing is listening. Treat it
            # as a timeout so callers see one consistent "no answer" path.
            LOG.debug("connection refused (no listener on %s:%d)", self.host, self.port)
            return None
        LOG.debug("RX %s", buf[:64].hex(" "))
        try:
            return parse_frame(buf)
        except BadChecksum as e:
            LOG.warning("dropping frame: %s", e)
            return None

    def drain(self) -> int:
        """
        Discard datagrams already sitting in the receive buffer.

        This matters more than it looks. Every request may be sent twice (the
        retry), so a slow panel that eventually answers both copies leaves a
        stale reply queued. The next request then reads that stale frame,
        mismatches, and can burn its whole timeout window - which shows up as
        spurious "panel offline" flapping rather than as an obvious error.
        """
        dropped = 0
        self.sock.settimeout(0)
        try:
            while True:
                try:
                    self.sock.recvfrom(RECV_BUFFER)
                    dropped += 1
                except (socket.timeout, BlockingIOError):
                    break
                except OSError:
                    # ECONNREFUSED from a previous send to a dead port; Linux
                    # reports it here on unconnected UDP sockets.
                    break
        finally:
            self.sock.settimeout(self.timeout)
        if dropped:
            LOG.debug("drained %d stale datagram(s)", dropped)
        return dropped

    def request(self, to: int, cmd: int, extra_l: int = 0, extra_h: int = 0,
                subctrl: int = 0, data: bytes = b"",
                expect=None) -> Optional[Frame]:
        """
        Send, then wait for a matching reply. Retries once with the longer
        timeout, mirroring the CML's 1.5s-then-3s behaviour for slow
        high-address controllers.

        'expect' is a response type (command - 100), or an iterable of
        acceptable types. Some replies legitimately come back under more than
        one command byte - see scan_subnets.
        """
        if expect is None:
            wanted = None
        elif isinstance(expect, int):
            wanted = {expect}
        else:
            wanted = set(expect)

        self.drain()
        for attempt, tmo in enumerate((RECV_TIMEOUT_1, RECV_TIMEOUT_2)):
            try:
                self.send(build_frame(to, cmd, extra_l, extra_h, subctrl, data))
            except OSError as e:
                LOG.debug("send failed: %s", e)
                return None
            deadline = time.time() + tmo
            while time.time() < deadline:
                fr = self.recv(timeout=max(0.05, deadline - time.time()))
                if fr is None:
                    break
                if wanted is not None and fr.response_type not in wanted:
                    # Stale reply from an earlier request. Keep waiting rather
                    # than abandoning this one.
                    LOG.debug("ignoring response type %d (cmd %d), want %s",
                              fr.response_type, fr.command, sorted(wanted))
                    continue
                return fr
            if attempt == 0:
                LOG.debug("retrying cmd %d with %.1fs timeout", cmd, RECV_TIMEOUT_2)
        return None

    # -- operations ---------------------------------------------------------

    def read_sysstatus(self) -> str:
        fr = self.request(self.controller, CMD_SYSSTATUS, expect=CMD_SYSSTATUS)
        if fr is None:
            raise NoResponse("no reply to system status (command 12)")
        base = (self.controller - 1) * SYSSTATUS_STRIDE
        raw = read_cstring(fr.data, base + SYSSTATUS_NAME_OFF, SYSSTATUS_NAME_MAX)
        self.panel_name = raw.decode("latin-1")
        return self.panel_name

    def scan_subnets(self) -> List[int]:
        """
        Subnet 0 is always present. 1..62 come from the command-50 bitmap.

        The CML maps BOTH response type 8 and response type 50 to
        SubNetCtrl, meaning real panels answer this under either command 108
        or command 150. Accepting only one of them silently yields "no
        sub-controllers found", which looks like a wiring problem rather than
        a parsing one.
        """
        fr = self.request(self.controller, CMD_FIFTY, extra_h=FIFTY_SUBNET_STATUS,
                          expect=(FIFTY_SUBNET_STATUS, CMD_FIFTY))
        found = [0]
        if fr is None:
            LOG.warning("no reply to the subnet scan (command 50/ExtraH 8); "
                        "assuming main controller only")
            self.subnets = found
            return found

        LOG.info("subnet scan replied with command %d, %d payload bytes",
                 fr.command, len(fr.data))
        for n in range(1, SUBNET_SCAN_COUNT + 1):
            off = (n - 1) * SUBNET_SCAN_STRIDE
            if off >= len(fr.data):
                break
            if fr.data[off] & 0x01:
                found.append(n)

        if len(found) == 1:
            # Show the bytes. If sub-controllers exist but no bit 0 is set,
            # the flag lives somewhere else in the 6-byte stride and the hex
            # is the only way to tell.
            LOG.warning("subnet bitmap reports no sub-controllers active. "
                        "First 48 bytes: %s", fr.data[:48].hex(" "))
        self.subnets = found
        return found

    def _point_signature(self, subnet: int):
        """
        Identity of a controller's point set, ignoring live values.

        Used to spot a sub-controller that is really just the main controller
        answering under a different SubController byte.
        """
        return frozenset(
            (p.ptype, p.address, p.description)
            for p in self.points.values() if p.subnet == subnet
        )

    def probe_subnets(self, lo: int = 1, hi: int = SUBNET_SCAN_COUNT,
                      timeout: float = 0.7) -> List[int]:
        """
        Find sub-controllers by asking each one directly instead of trusting
        the command-50 bitmap.

        Slower than the bitmap but far more reliable: a sub-controller that
        answers a Variables read demonstrably exists, whatever the bitmap says.
        Single attempt with a short timeout, since we only need presence.
        """
        found = [0]
        for subnet in range(lo, hi + 1):
            if not self._alive(subnet, timeout):
                continue
            found.append(subnet)
            LOG.info("  sub-controller %d responded", subnet)
        return found

    def _alive(self, subnet: int, timeout: float) -> bool:
        """One short Variables-bank-0 read. True if anything sane comes back."""
        to, subctrl = self._dest(subnet)
        self.drain()
        try:
            self.send(build_frame(to, CMD_VARIABLES, extra_l=0, subctrl=subctrl))
        except OSError:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            fr = self.recv(timeout=max(0.05, deadline - time.time()))
            if fr is None:
                return False
            if fr.response_type == CMD_VARIABLES and fr.subctrl == subnet:
                return True
        return False

    def read_point(self, address: int, subnet: int = 0) -> Optional[Frame]:
        """
        Read one variable by address using command 20 (individual point).

        Independent of bank reads, so it answers a question bank reads cannot:
        does this variable exist on this sub-controller at all? An empty
        Variables bank could mean "none defined" or "my bank geometry is wrong",
        and this distinguishes them.
        """
        packed = pack_point_address(address, subnet, self.controller)
        to, subctrl = self._dest(subnet)
        return self.request(to, CMD_POINT_READ, extra_l=packed & 0xFF,
                            extra_h=(packed >> 8) & 0xFF, subctrl=subctrl,
                            expect=CMD_POINT_READ)

    def read_bank(self, ptype: int, subnet: int, bank: int) -> List[Point]:
        to, subctrl = self._dest(subnet)
        fr = self.request(to, COMMAND_FOR_TYPE[ptype], extra_l=bank,
                          subctrl=subctrl, expect=COMMAND_FOR_TYPE[ptype])
        if fr is None:
            return []
        # Trust the reply's own bank/subnet over what we asked for; the CML
        # re-derives them from the header for exactly this reason.
        return parse_points(fr, ptype, fr.subctrl, fr.bank)

    def discover(self, num_banks: int = NUM_BANKS,
                 subnet_mode: str = "auto") -> Dict[str, Point]:
        """
        Full configuration query: panel name, sub-controllers, then every bank.

        subnet_mode:
          "bitmap" - trust the command-50 reply only (fastest, what CQC does)
          "probe"  - ignore the bitmap, ask each of 1..62 directly (~45s)
          "auto"   - bitmap first, then probe if it found nothing
        """
        self.read_sysstatus()
        LOG.info("panel: %s", self.panel_name or "(unnamed)")

        if subnet_mode == "probe":
            LOG.info("probing sub-controllers 1-%d directly...", SUBNET_SCAN_COUNT)
            self.subnets = self.probe_subnets()
        else:
            self.scan_subnets()
            if subnet_mode == "auto" and len(self.subnets) == 1:
                LOG.info("bitmap found no sub-controllers; probing directly "
                         "as a cross-check (this takes ~45s)...")
                probed = self.probe_subnets()
                if len(probed) > 1:
                    LOG.warning("probe found %d sub-controller(s) the bitmap "
                                "missed: %s", len(probed) - 1, probed[1:])
                    self.subnets = probed
                else:
                    LOG.info("probe agrees: main controller only")

        LOG.info("subnets: %s", ", ".join(str(s) for s in self.subnets))

        self.points.clear()
        # Per-subnet, per-type tally. Worth logging: "sub-board points exist
        # but none are writable" almost always means its Variables banks came
        # back empty, and that is invisible from a plain total.
        tally: Dict[int, Dict[int, int]] = {}
        for subnet in self.subnets:
            tally[subnet] = {TYPE_OUTPUTS: 0, TYPE_INPUTS: 0, TYPE_VARIABLES: 0}
            for bank in range(num_banks):
                for ptype in (TYPE_OUTPUTS, TYPE_INPUTS, TYPE_VARIABLES):
                    found = self.read_bank(ptype, subnet, bank)
                    for p in found:
                        self.points[p.name] = p
                    tally[subnet][ptype] += len(found)
                    time.sleep(0.05)  # be gentle; this is a live controller

        # Drop sub-controllers that merely echo the main controller's points.
        #
        # A panel may answer a request addressed to a non-existent
        # sub-controller by echoing the SubController byte and returning its own
        # data. Probing then "finds" dozens of phantom boards, each contributing
        # a duplicate copy of every point. The signature comparison below is the
        # only reliable way to tell a real sub-board from an echo, since the
        # header looks identical either way.
        main_sig = self._point_signature(0)
        if main_sig:
            for subnet in [s for s in self.subnets if s != 0]:
                if self._point_signature(subnet) == main_sig:
                    LOG.warning("sub-controller %d returned the SAME points as "
                                "the main controller - treating it as an echo, "
                                "not a real board, and discarding it", subnet)
                    for name in [n for n, p in self.points.items()
                                 if p.subnet == subnet]:
                        del self.points[name]
                    tally.pop(subnet, None)
                    self.subnets.remove(subnet)

        for subnet, counts in tally.items():
            label = "main" if subnet == 0 else f"sub {subnet}"
            LOG.info("  %-8s outputs=%-4d inputs=%-4d variables=%-4d %s",
                     label, counts[TYPE_OUTPUTS], counts[TYPE_INPUTS],
                     counts[TYPE_VARIABLES],
                     "<- NO WRITABLE POINTS" if counts[TYPE_VARIABLES] == 0 else "")
            if counts[TYPE_VARIABLES] == 0 and sum(counts.values()) > 0:
                LOG.warning("  %s answered for inputs/outputs but returned no "
                            "variables. Only variables are writable, so this "
                            "controller will be read-only in HA. Try raising "
                            "'banks' - its variables may live in a higher bank.",
                            label)

        writable = sum(1 for p in self.points.values() if p.writable)
        LOG.info("discovered %d points (%d writable variables)",
                 len(self.points), writable)
        return self.points

    def bank_list(self, num_banks: int = NUM_BANKS) -> List[Tuple[int, int, int]]:
        """
        Every (type, subnet, bank) that holds points, in a stable order.

        Exposed so a caller can drive the poll one bank at a time and do other
        work in between - servicing writes, publishing partial results - instead
        of being stuck inside a single long poll() call. On a panel with 30
        sub-controllers a full sweep is close to a minute, which is far too long
        to hold a lock or defer a setpoint change.
        """
        return sorted({(p.ptype, p.subnet, p.bank)
                       for p in self.points.values() if p.bank < num_banks})

    def poll(self, num_banks: int = NUM_BANKS) -> Dict[str, float]:
        """Re-read known banks and return {name: value} for changed reads."""
        values: Dict[str, float] = {}
        banks = {(p.ptype, p.subnet, p.bank) for p in self.points.values()}
        for ptype, subnet, bank in sorted(banks):
            if bank >= num_banks:
                continue
            for p in self.read_bank(ptype, subnet, bank):
                values[p.name] = p.value
                if p.name in self.points:
                    self.points[p.name].value = p.value
        return values

    def write_variable(self, name: str, value: float, dry_run: bool = False) -> bytes:
        p = self.points.get(name)
        if p is None:
            raise RCPError(f"unknown point '{name}'. Run discover first.")
        if not p.writable:
            raise RCPError(f"'{name}' is type {TYPE_NAMES[p.ptype]}; only Variables are writable")
        if self.controller > WRITE_MAX_CONTROLLER:
            raise RCPError(
                f"controller {self.controller} > {WRITE_MAX_CONTROLLER}: "
                "variable writes are not supported by the protocol"
            )

        packed = pack_point_address(p.address, p.subnet, self.controller)
        payload = build_write_payload(p, value)
        to, subctrl = self._dest(p.subnet)
        frame = build_frame(to, CMD_POINT_WRITE, extra_l=packed & 0xFF,
                           extra_h=(packed >> 8) & 0xFF, subctrl=subctrl, data=payload)

        if dry_run:
            LOG.info("DRY RUN, would send: %s", frame.hex(" "))
            return frame

        self.send(frame)
        # The driver does not wait for an ack on writes; confirm by re-reading.
        return frame


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_discover(args) -> int:
    with MachClient(args.host, args.controller, port=args.port,
                    bind_port=args.bind_port) as c:
        pts = c.discover(num_banks=args.banks,
                         subnet_mode=getattr(args, "subnet_mode", "auto"))
        if not pts:
            print("No points found. Check --controller (this is the RC panel "
                  "number, not an IP) and that UDP 21068 is reachable.")
            return 1
        for name in sorted(pts):
            p = pts[name]
            flag = "RW" if p.writable else "R "
            print(f"{flag} {name:<44} = {p.value:>12.3f}")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"panel": c.panel_name,
                           "controller": args.controller,
                           "points": [p.to_dict() for p in pts.values()]},
                          fh, indent=2)
            print(f"\nWrote {len(pts)} points to {args.out}")
    return 0


def cmd_subnets(args) -> int:
    """Diagnose sub-controller discovery: raw bitmap vs direct probe."""
    with MachClient(args.host, args.controller, port=args.port,
                    bind_port=args.bind_port) as c:
        print(f"Panel at {args.host}, controller {args.controller}\n")

        try:
            name = c.read_sysstatus()
            print(f"system status OK, panel name: '{name}'")
        except NoResponse as e:
            print(f"system status FAILED: {e}")
            print("Nothing else will work until this does. Check --controller.")
            return 1

        print("\n--- 1. bitmap (command 50, ExtraH 8) ---")
        fr = c.request(c.controller, CMD_FIFTY, extra_h=FIFTY_SUBNET_STATUS,
                       expect=(FIFTY_SUBNET_STATUS, CMD_FIFTY))
        if fr is None:
            print("  no reply at all")
        else:
            print(f"  reply command : {fr.command} (response type {fr.response_type})")
            print(f"  payload bytes : {len(fr.data)}")
            print("  raw payload, 6 bytes per sub-controller:")
            for n in range(1, 21):
                off = (n - 1) * SUBNET_SCAN_STRIDE
                if off + 6 > len(fr.data):
                    break
                chunk = fr.data[off:off + 6]
                mark = "ACTIVE" if chunk[0] & 0x01 else ""
                print(f"    subnet {n:>2}: {chunk.hex(' ')}  {mark}")
            active = [n for n in range(1, SUBNET_SCAN_COUNT + 1)
                      if (n - 1) * SUBNET_SCAN_STRIDE < len(fr.data)
                      and fr.data[(n - 1) * SUBNET_SCAN_STRIDE] & 0x01]
            print(f"  bitmap says active: {active or 'none'}")

        print(f"\n--- 2. direct probe of subnets 1-{args.max_subnet} ---")
        print("    (asking each one for Variables bank 0; slow but definitive)")
        probed = c.probe_subnets(1, args.max_subnet, timeout=args.probe_timeout)
        print(f"  responded: {probed[1:] or 'none'}")

        print("\n--- verdict ---")
        if len(probed) > 1:
            print(f"  {len(probed)-1} sub-controller(s) are reachable.")
            print("  Set subnet_mode to 'probe' in the app config to use these.")
        else:
            print("  No sub-controllers answered a direct request either.")
            print("  So they are genuinely not reachable at this IP - they may")
            print("  be separate panels with their own IPs, or on Subnet B,")
            print("  which this protocol implementation does not support.")
    return 0


def cmd_point(args) -> int:
    """
    Read individual variables directly, bypassing bank reads entirely.

    Use this when a sub-controller shows inputs and outputs but no variables:
    if a direct read returns a named variable, the bank geometry is wrong and
    that's my bug. If every address comes back empty, the controller genuinely
    has no variables defined and nothing could ever write to it.
    """
    with MachClient(args.host, args.controller, port=args.port,
                    bind_port=args.bind_port) as c:
        print(f"Direct variable reads, subnet {args.subnet}, "
              f"addresses {args.first}-{args.last}\n")
        hits = 0
        for addr in range(args.first, args.last + 1):
            try:
                fr = c.read_point(addr, args.subnet)
            except RCPError as e:
                print(f"  addr {addr:>3}: {e}")
                continue
            if fr is None:
                print(f"  addr {addr:>3}: no reply")
                continue

            desc = read_cstring(fr.data, 0, 22)
            if not desc:
                print(f"  addr {addr:>3}: reply but empty slot")
                continue
            (value,) = struct.unpack_from("<f", fr.data, VALUE_OFFSET)
            label = read_cstring(fr.data, VAR_OFF_LABEL, VAR_LABEL_MAX)
            hits += 1
            print(f"  addr {addr:>3}: {desc.decode('latin-1'):<24} = {value:>12.3f} "
                  f"{label.decode('latin-1')}")
            if args.hex:
                print(f"            raw: {fr.data[:VAR_RECORD_LEN].hex(' ')}")

        print(f"\n{hits} variable(s) found on subnet {args.subnet}")
        if hits:
            print("They exist. If the bank scan missed them, that's a bug in the")
            print("bank geometry - send me this output.")
        else:
            print("None defined. This controller has no writable variables, so")
            print("read-only in HA is correct - CQC could not write to it either.")
    return 0


def cmd_watch(args) -> int:
    with MachClient(args.host, args.controller, port=args.port,
                    bind_port=args.bind_port) as c:
        c.discover(num_banks=args.banks)
        last: Dict[str, float] = {}
        print("Watching. Ctrl-C to stop.\n")
        try:
            while True:
                for name, val in sorted(c.poll(num_banks=args.banks).items()):
                    if name not in last:
                        last[name] = val
                    elif abs(last[name] - val) > 1e-6:
                        print(f"{time.strftime('%H:%M:%S')}  {name:<44} "
                              f"{last[name]:>10.3f} -> {val:<10.3f}")
                        last[name] = val
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def cmd_write(args) -> int:
    with MachClient(args.host, args.controller, port=args.port,
                    bind_port=args.bind_port) as c:
        c.discover(num_banks=args.banks)
        p = c.points.get(args.field)
        if p is None:
            print(f"No such point: {args.field}")
            matches = [n for n in c.points if args.field.lower() in n.lower()]
            if matches:
                print("Did you mean:")
                for m in sorted(matches)[:10]:
                    print("   ", m)
            return 1

        print(f"{p.name}\n  current: {p.value}\n  new:     {args.value}")
        if not args.yes and not args.dry_run:
            if input("Write it? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1

        c.write_variable(args.field, args.value, dry_run=args.dry_run)
        if args.dry_run:
            return 0

        time.sleep(1.0)
        for chk in c.read_bank(p.ptype, p.subnet, p.bank):
            if chk.name == p.name:
                ok = abs(chk.value - args.value) < 1e-3
                print(f"  readback: {chk.value}  {'OK' if ok else 'MISMATCH'}")
                return 0 if ok else 2
        print("  readback: point not found in re-read")
        return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="count", default=0)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", required=True, help="controller IP")
    common.add_argument("--controller", type=int, default=1,
                        help="RC communication controller number (CQC's "
                             "CommunicationController prompt), default 1")
    common.add_argument("--banks", type=int, default=NUM_BANKS,
                        help=f"banks to scan per type (default {NUM_BANKS}, "
                             f"protocol max {MAX_BANKS_PROTOCOL})")
    common.add_argument("--bind-port", type=int, default=RCP_PORT,
                        help="local UDP port; 0 for ephemeral if CQC holds 21068")
    common.add_argument("--port", type=int, default=RCP_PORT,
                        help=f"remote UDP port (default {RCP_PORT}; change only "
                             f"when talking to rc_mock_panel.py)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", parents=[common], help="enumerate all points")
    d.add_argument("--out", help="also write JSON here (feeds the MQTT bridge)")
    d.add_argument("--subnet-mode", choices=("auto", "bitmap", "probe"),
                   default="auto",
                   help="how to find sub-controllers (default auto)")
    d.set_defaults(func=cmd_discover)

    pt = sub.add_parser("point", parents=[common],
                        help="read individual variables directly (bypasses banks)")
    pt.add_argument("--subnet", type=int, default=0,
                    help="0 = main controller, else the sub-controller number")
    pt.add_argument("--first", type=int, default=1)
    pt.add_argument("--last", type=int, default=16)
    pt.add_argument("--hex", action="store_true", help="dump raw record bytes")
    pt.set_defaults(func=cmd_point)

    s2 = sub.add_parser("subnets", parents=[common],
                        help="diagnose sub-controller discovery")
    s2.add_argument("--max-subnet", type=int, default=SUBNET_SCAN_COUNT,
                    help=f"highest subnet to probe (default {SUBNET_SCAN_COUNT})")
    s2.add_argument("--probe-timeout", type=float, default=0.7,
                    help="seconds to wait per subnet (default 0.7)")
    s2.set_defaults(func=cmd_subnets)

    w = sub.add_parser("watch", parents=[common], help="poll and print changes")
    w.add_argument("--interval", type=float, default=5.0)
    w.set_defaults(func=cmd_watch)

    x = sub.add_parser("write", parents=[common], help="write one variable")
    x.add_argument("--field", required=True)
    x.add_argument("--value", type=float, required=True)
    x.add_argument("--yes", action="store_true", help="skip confirmation")
    x.add_argument("--dry-run", action="store_true", help="print the frame, send nothing")
    x.set_defaults(func=cmd_write)

    args = ap.parse_args()
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)-7s %(message)s")
    try:
        return args.func(args)
    except RCPError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
