import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1] / "reliable_controls"))

#!/usr/bin/env python3
"""
test_rcp.py - verify rcp.py against the CML driver's logic by hand-computing
the values the original would produce. No controller needed.

    python3 test_rcp.py
"""

import struct
import sys
import unittest

import rcp


class TestFraming(unittest.TestCase):

    def test_header_layout_and_checksum(self):
        # Outputs, bank 1, subnet 0, controller 1
        f = rcp.build_frame(to=1, cmd=rcp.CMD_OUTPUTS, extra_l=1, extra_h=0, subctrl=0)
        self.assertEqual(len(f), 12)
        self.assertEqual(f[0], 1)                 # To
        self.assertEqual(f[1], 0)                 # From
        self.assertEqual(f[2], 1)                 # Command
        self.assertEqual(f[3], 1)                 # ExtraL = bank
        self.assertEqual(f[4], 0)                 # ExtraH
        self.assertEqual(f[5], 0)                 # SubController
        self.assertEqual(f[6], (1 + 0 + 1 + 1 + 0 + 0) & 0xFF)   # checksum
        self.assertEqual(f[7], 0x45)              # Port, constant
        self.assertEqual(f[8], 0)
        self.assertEqual(f[9], 0)
        self.assertEqual(f[10], 0)                # CountL, no payload
        self.assertEqual(f[11], 0)                # CountH always 0

    def test_checksum_covers_only_first_six_bytes(self):
        """Port/Reserved/Count must NOT contribute, or the panel drops frames."""
        f = rcp.build_frame(to=200, cmd=120, extra_l=0xFF, extra_h=0x09,
                            subctrl=5, data=b"\x00" * 38)
        self.assertEqual(f[6], (200 + 0 + 120 + 0xFF + 0x09 + 5) & 0xFF)
        self.assertEqual(f[10], 38)

    def test_checksum_wraps_at_256(self):
        f = rcp.build_frame(to=250, cmd=120, extra_l=200, subctrl=0)
        self.assertEqual(f[6], (250 + 120 + 200) & 0xFF)
        self.assertLess(f[6], 256)

    def test_parse_rejects_bad_checksum(self):
        buf = bytearray(rcp.build_frame(to=1, cmd=101))
        buf[6] ^= 0xFF
        with self.assertRaises(rcp.BadChecksum):
            rcp.parse_frame(bytes(buf))

    def test_response_type_strips_100(self):
        f = make_response(cmd=103, extra_l=1, subctrl=0, body=b"")
        fr = rcp.parse_frame(f)
        self.assertEqual(fr.response_type, rcp.CMD_VARIABLES)   # 3
        self.assertEqual(fr.bank, 1)


class TestBankGeometry(unittest.TestCase):

    def test_io_banks_are_flat_32(self):
        for bank in range(4):
            self.assertEqual(rcp.points_in_bank(rcp.TYPE_INPUTS, bank), (32, 32 * bank))
            self.assertEqual(rcp.points_in_bank(rcp.TYPE_OUTPUTS, bank), (32, 32 * bank))

    def test_variable_banks_are_48_then_32(self):
        # The CML special-cases banks 0 and 1 at 48 points, then 32 from 96.
        self.assertEqual(rcp.points_in_bank(rcp.TYPE_VARIABLES, 0), (48, 0))
        self.assertEqual(rcp.points_in_bank(rcp.TYPE_VARIABLES, 1), (48, 48))
        self.assertEqual(rcp.points_in_bank(rcp.TYPE_VARIABLES, 2), (32, 96))
        self.assertEqual(rcp.points_in_bank(rcp.TYPE_VARIABLES, 3), (32, 128))

    def test_record_sizes_match_cml(self):
        self.assertEqual(rcp.RECORD_SPEC[rcp.TYPE_OUTPUTS], (42, 21))
        self.assertEqual(rcp.RECORD_SPEC[rcp.TYPE_INPUTS], (38, 21))
        self.assertEqual(rcp.RECORD_SPEC[rcp.TYPE_VARIABLES], (38, 22))


class TestAddressPacking(unittest.TestCase):
    """
    Reproduces the CML shift arithmetic independently:
        main:   Point = ctrl;  <<4; if addr<=128 += 3;  <<7; += (addr-1)%128
        subnet: Point = (addr-1)%32; <<4; += type; <<7; += subnet
    """

    @staticmethod
    def cml_main(ctrl, addr):
        p = ctrl
        p <<= 4
        if addr <= 128:
            p += 3
        p <<= 7
        p += (addr - 1) % 128
        return p & 0xFFFF

    @staticmethod
    def cml_subnet(addr, subnet):
        p = (addr - 1) % 32
        p <<= 4
        if addr <= 32:
            p += 3
        elif addr <= 64:
            p += 13
        elif addr <= 96:
            p += 14
        else:
            p += 15
        p <<= 7
        p += subnet
        return p & 0xFFFF

    def test_main_matches_cml(self):
        for ctrl in (1, 2, 15, 31):
            for addr in (1, 2, 3, 48, 128, 129, 200, 256):
                self.assertEqual(
                    rcp.pack_point_address(addr, 0, ctrl),
                    self.cml_main(ctrl, addr),
                    f"ctrl={ctrl} addr={addr}")

    def test_subnet_matches_cml(self):
        for subnet in (1, 5, 62):
            for addr in (1, 32, 33, 64, 65, 96, 97, 128):
                self.assertEqual(
                    rcp.pack_point_address(addr, subnet, 1),
                    self.cml_subnet(addr, subnet),
                    f"subnet={subnet} addr={addr}")

    def test_known_main_value(self):
        # controller 1, variable address 3, main network:
        #   1<<11 | 3<<7 | 2  =  2048 + 384 + 2 = 2434 = 0x0982
        packed = rcp.pack_point_address(3, 0, 1)
        self.assertEqual(packed, 2434)
        self.assertEqual(packed & 0xFF, 0x82)          # ExtraL
        self.assertEqual((packed >> 8) & 0xFF, 0x09)   # ExtraH

    def test_known_subnet_value(self):
        # subnet 5, address 35 -> ((34)%32)=2, type 13, +5
        #   2<<11 | 13<<7 | 5 = 4096 + 1664 + 5 = 5765
        self.assertEqual(rcp.pack_point_address(35, 5, 1), 5765)

    def test_subnet_over_128_rejected(self):
        with self.assertRaises(rcp.RCPError):
            rcp.pack_point_address(129, 5, 1)


class TestPointParsing(unittest.TestCase):

    def test_variable_record_fields(self):
        rec = bytearray(38)
        rec[0:9] = b"SpaSetPt\x00"
        struct.pack_into("<f", rec, 22, 102.5)
        rec[26] = 0xA1          # aaoiu
        rec[27] = 0x04          # range
        rec[28:32] = b"degF"    # label
        rec[37] = 0x02          # prgctrl

        fr = rcp.parse_frame(make_response(103, extra_l=0, subctrl=0, body=bytes(rec)))
        pts = rcp.parse_points(fr, rcp.TYPE_VARIABLES, 0, 0)

        self.assertEqual(len(pts), 1)
        p = pts[0]
        self.assertAlmostEqual(p.value, 102.5, places=4)
        self.assertEqual(p.address, 1)
        self.assertEqual(p.aaoiu, 0xA1)
        self.assertEqual(p.vrange, 0x04)
        self.assertEqual(p.label, b"degF")
        self.assertEqual(p.prgctrl, 0x02)
        self.assertTrue(p.writable)
        self.assertEqual(p.name, "Main-Var001-SpaSetPt")

    def test_description_sanitizing(self):
        rec = bytearray(38)
        rec[0:11] = b"Pool Temp!\x00"
        struct.pack_into("<f", rec, 22, 80.0)
        fr = rcp.parse_frame(make_response(103, 0, 0, bytes(rec)))
        p = rcp.parse_points(fr, rcp.TYPE_VARIABLES, 0, 0)[0]
        # space and '!' both become underscores, as the CML does
        self.assertEqual(p.description, "Pool_Temp_")

    def test_empty_slots_skipped(self):
        body = bytearray(38 * 3)
        struct.pack_into("<f", body, 22, 1.0)          # slot 0 has no name
        body[38:44] = b"Real\x00\x00"                  # slot 1 named
        struct.pack_into("<f", body, 38 + 22, 55.0)
        fr = rcp.parse_frame(make_response(103, 0, 0, bytes(body)))
        pts = rcp.parse_points(fr, rcp.TYPE_VARIABLES, 0, 0)
        self.assertEqual([p.description for p in pts], ["Real"])
        self.assertEqual(pts[0].address, 2)            # 1-based, slot index 1

    def test_bank1_addresses_offset_by_48(self):
        rec = bytearray(38)
        rec[0:4] = b"Var\x00"
        struct.pack_into("<f", rec, 22, 7.0)
        fr = rcp.parse_frame(make_response(103, extra_l=1, subctrl=0, body=bytes(rec)))
        p = rcp.parse_points(fr, rcp.TYPE_VARIABLES, 0, 1)[0]
        self.assertEqual(p.address, 49)
        self.assertEqual(p.name, "Main-Var049-Var")

    def test_subnet_names_use_A_prefix(self):
        rec = bytearray(38)
        rec[0:5] = b"Pump\x00"
        struct.pack_into("<f", rec, 22, 1.0)
        fr = rcp.parse_frame(make_response(103, 0, subctrl=5, body=bytes(rec)))
        p = rcp.parse_points(fr, rcp.TYPE_VARIABLES, 5, 0)[0]
        self.assertEqual(p.name, "A5-Var001-Pump")

    def test_outputs_use_42_byte_stride(self):
        body = bytearray(42 * 2)
        body[0:4] = b"Out1"
        struct.pack_into("<f", body, 22, 11.0)
        body[42:46] = b"Out2"
        struct.pack_into("<f", body, 42 + 22, 22.0)
        fr = rcp.parse_frame(make_response(101, 0, 0, bytes(body)))
        pts = rcp.parse_points(fr, rcp.TYPE_OUTPUTS, 0, 0)
        self.assertEqual([p.description for p in pts], ["Out1", "Out2"])
        self.assertAlmostEqual(pts[1].value, 22.0, places=4)
        self.assertFalse(pts[0].writable)


class TestWritePayload(unittest.TestCase):

    def test_payload_preserves_everything_but_the_float(self):
        p = rcp.Point(ptype=rcp.TYPE_VARIABLES, subnet=0, bank=0, offset=0,
                      address=1, name="Main-Var001-SpaSetPt",
                      description="SpaSetPt", value=100.0, writable=True,
                      raw_description=b"SpaSetPt", aaoiu=0xA1, vrange=0x04,
                      label=b"degF", prgctrl=0x02)
        out = rcp.build_write_payload(p, 104.0)

        self.assertEqual(len(out), 38)
        self.assertEqual(out[0:8], b"SpaSetPt")
        self.assertEqual(out[8:22], b"\x00" * 14)      # zero-padded to the float
        self.assertAlmostEqual(struct.unpack_from("<f", out, 22)[0], 104.0, places=4)
        self.assertEqual(out[26], 0xA1)
        self.assertEqual(out[27], 0x04)
        self.assertEqual(out[28:32], b"degF")
        self.assertEqual(out[37], 0x02)

    def test_float_is_little_endian(self):
        p = rcp.Point(ptype=rcp.TYPE_VARIABLES, subnet=0, bank=0, offset=0,
                      address=1, name="x", description="x", writable=True,
                      raw_description=b"x")
        out = rcp.build_write_payload(p, 1.0)
        self.assertEqual(out[22:26], b"\x00\x00\x80\x3f")


class TestWriteGuards(unittest.TestCase):

    def _client(self, controller):
        c = rcp.MachClient.__new__(rcp.MachClient)   # no socket
        c.controller = controller
        c.points = {}
        return c

    def test_refuses_read_only_types(self):
        c = self._client(1)
        c.points["Main-In001-Temp"] = rcp.Point(
            ptype=rcp.TYPE_INPUTS, subnet=0, bank=0, offset=0, address=1,
            name="Main-In001-Temp", description="Temp", writable=False)
        with self.assertRaises(rcp.RCPError) as cm:
            c.write_variable("Main-In001-Temp", 1.0, dry_run=True)
        self.assertIn("only Variables are writable", str(cm.exception))

    def test_refuses_controller_above_31(self):
        c = self._client(32)
        c.points["Main-Var001-X"] = rcp.Point(
            ptype=rcp.TYPE_VARIABLES, subnet=0, bank=0, offset=0, address=1,
            name="Main-Var001-X", description="X", writable=True,
            raw_description=b"X")
        with self.assertRaises(rcp.RCPError) as cm:
            c.write_variable("Main-Var001-X", 1.0, dry_run=True)
        self.assertIn("not supported", str(cm.exception))

    def test_unknown_point_rejected(self):
        with self.assertRaises(rcp.RCPError):
            self._client(1).write_variable("Nope", 1.0, dry_run=True)


# ---------------------------------------------------------------------------

def make_response(cmd, extra_l=0, subctrl=0, body=b"", to=0, frm=1, extra_h=0):
    """Build a well-formed reply frame with a valid checksum."""
    h = bytearray(12)
    h[0], h[1], h[2] = to, frm, cmd
    h[3], h[4], h[5] = extra_l, extra_h, subctrl
    h[6] = sum(h[0:6]) & 0xFF
    h[7] = 0x45
    struct.pack_into("<H", h, 10, len(body))
    return bytes(h) + body


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if r.wasSuccessful() else 1)
