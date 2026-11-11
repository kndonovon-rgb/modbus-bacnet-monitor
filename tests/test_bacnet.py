"""The BACnet client, byte for byte, against a device speaking real APDUs.

There is no mock in this file. The simulator encodes genuine BVLL/NPDU/APDU
frames and the client parses them, so a mistake in either side's tag handling
shows up here rather than on site.
"""
import socket
import struct
import threading
import unittest

from dcmon import bacnet
from dcmon.bacnet import BACnetIP, BACnetError, BACnetTransport
from sim.bacnet_ahu import AHU, AHUServer, handle_datagram


class Encoding(unittest.TestCase):
    """Check the bytes we put on the wire against the spec by hand."""

    def test_object_identifier_packing(self):
        """10 bits of type, 22 bits of instance, in one 32-bit field."""
        raw = bacnet.encode_object_id(bacnet.ANALOG_INPUT, 1)
        self.assertEqual(raw, struct.pack(">I", (0 << 22) | 1))
        raw = bacnet.encode_object_id(bacnet.ANALOG_VALUE, 3)
        self.assertEqual(struct.unpack(">I", raw)[0], (2 << 22) | 3)

    def test_oversized_object_fields_are_refused(self):
        with self.assertRaises(ValueError):
            bacnet.encode_object_id(2000, 1)
        with self.assertRaises(ValueError):
            bacnet.encode_object_id(0, 0x400000)

    def test_read_property_frame_is_well_formed(self):
        f = bacnet.encode_read_property(42, bacnet.ANALOG_INPUT, 7)
        self.assertEqual(f[0], 0x81)                       # BACnet/IP
        self.assertEqual(f[1], 0x0A)                       # original-unicast
        self.assertEqual(struct.unpack(">H", f[2:4])[0], len(f))
        self.assertEqual(f[4], 0x01)                       # NPDU version
        self.assertEqual(f[5] & 0x04, 0x04)                # expecting reply
        self.assertEqual(f[6] & 0xF0, 0x00)                # confirmed request
        self.assertEqual(f[8], 42)                         # invoke id
        self.assertEqual(f[9], 12)                         # readProperty
        self.assertEqual(f[10], 0x0C)                      # context 0, len 4
        self.assertEqual(f[15], 0x19)                      # context 1, len 1
        self.assertEqual(f[16], bacnet.PROP_PRESENT_VALUE)

    def test_declared_length_must_match_the_datagram(self):
        f = bytearray(bacnet.encode_read_property(1, 0, 1))
        f[3] = 0xFF
        with self.assertRaises(BACnetTransport) as ctx:
            bacnet.decode_read_property_ack(bytes(f))
        self.assertIn("BVLL says", str(ctx.exception))


class AgainstTheSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ahu = AHU()
        cls.srv = AHUServer(port=0, ahu=cls.ahu)
        cls.port = cls.srv.port
        cls.srv.serve_in_thread()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self, timeout=2.0):
        return BACnetIP("127.0.0.1", self.port, timeout=timeout)

    def test_reads_a_real_present_value(self):
        self.ahu.set((bacnet.ANALOG_INPUT, 1), "value", 56.25)
        with self.client() as c:
            v = c.read_property("analog-input", 1)
        self.assertAlmostEqual(v, 56.25, places=3)

    def test_reads_a_binary_point_as_an_enumeration(self):
        with self.client() as c:
            v = c.read_property(bacnet.BINARY_INPUT, 4)
        self.assertEqual(v, 1)

    def test_reads_a_character_string_property(self):
        with self.client() as c:
            name = c.read_property("analog-input", 1,
                                   bacnet.PROP_OBJECT_NAME)
        self.assertEqual(name, "SA-TEMP")

    def test_reads_an_unsigned_property(self):
        with self.client() as c:
            units = c.read_property("analog-input", 2, bacnet.PROP_UNITS)
        self.assertEqual(units, 64)             # degrees-fahrenheit

    def test_reads_a_boolean_property(self):
        with self.client() as c:
            oos = c.read_property("analog-value", 3,
                                  bacnet.PROP_OUT_OF_SERVICE)
        self.assertIs(oos, False)

    def test_unknown_object_is_an_error_pdu_not_a_value(self):
        with self.client() as c:
            with self.assertRaises(BACnetError) as ctx:
                c.read_property("analog-input", 99)
        self.assertIn("unknown-object", str(ctx.exception))

    def test_unknown_property_is_an_error_pdu(self):
        with self.client() as c:
            with self.assertRaises(BACnetError) as ctx:
                c.read_property(bacnet.BINARY_INPUT, 4, bacnet.PROP_UNITS)
        self.assertIn("unknown-property", str(ctx.exception))

    def test_a_silent_device_times_out(self):
        self.srv.dropped = 10
        try:
            with self.client(timeout=0.5) as c:
                with self.assertRaises(BACnetTransport):
                    c.read_property("analog-input", 1)
        finally:
            self.srv.dropped = 0

    def test_invoke_ids_advance_and_wrap(self):
        with self.client() as c:
            c._invoke = 254
            c.read_property("analog-input", 1)
            self.assertEqual(c._invoke, 255)
            c.read_property("analog-input", 1)
            self.assertEqual(c._invoke, 0)

    def test_out_of_service_makes_a_plausible_value_untrustworthy(self):
        """The number looks fine. The device is telling you it is fiction.

        An out-of-service point reports whatever was last written to it for
        commissioning. Reading present-value alone cannot distinguish that
        from a live measurement, which is why `trustworthy` reads the flags.
        """
        key = (bacnet.ANALOG_INPUT, 2)
        self.ahu.set(key, "value", 72.0)
        self.ahu.set(key, "out_of_service", True)
        try:
            with self.client() as c:
                r = c.trustworthy(bacnet.ANALOG_INPUT, 2)
            self.assertAlmostEqual(r["value"], 72.0, places=2)
            self.assertIs(r["out_of_service"], True)
            self.assertFalse(r["trusted"])
        finally:
            self.ahu.set(key, "out_of_service", False)

    def test_a_fault_flag_makes_a_plausible_value_untrustworthy(self):
        key = (bacnet.ANALOG_INPUT, 1)
        self.ahu.set(key, "value", 55.0)
        self.ahu.set(key, "reliability", 7)      # unreliable-other
        try:
            with self.client() as c:
                r = c.trustworthy(bacnet.ANALOG_INPUT, 1)
            self.assertEqual(r["reliability"], 7)
            self.assertFalse(r["trusted"])
        finally:
            self.ahu.set(key, "reliability", 0)

    def test_a_healthy_point_is_trusted(self):
        with self.client() as c:
            r = c.trustworthy(bacnet.ANALOG_INPUT, 1)
        self.assertTrue(r["trusted"])


class StaleReplies(unittest.TestCase):
    """UDP has no ordering, so a late reply must not be believed."""

    def test_a_reply_for_another_invoke_id_is_not_accepted(self):
        ahu = AHU()
        ahu.set((bacnet.ANALOG_INPUT, 1), "value", 99.0)
        request = bacnet.encode_read_property(5, bacnet.ANALOG_INPUT, 1)
        reply = handle_datagram(request, ahu)
        self.assertIsNotNone(reply)
        # Correct for invoke 5 ...
        self.assertAlmostEqual(
            bacnet.decode_read_property_ack(reply, expect_invoke=5), 99.0, places=2)
        # ... and refused when we are waiting on invoke 6.
        with self.assertRaises(BACnetTransport) as ctx:
            bacnet.decode_read_property_ack(reply, expect_invoke=6)
        self.assertIn("invoke id", str(ctx.exception))

    def test_client_skips_a_late_datagram_and_finds_the_right_one(self):
        """Inject a stale reply ahead of the real one and insist on the real one."""
        ahu = AHU()
        ahu.set((bacnet.ANALOG_INPUT, 1), "value", 61.5)
        srv = AHUServer(port=0, ahu=ahu)
        stale = handle_datagram(
            bacnet.encode_read_property(200, bacnet.ANALOG_INPUT, 1), ahu)

        def run():
            srv.sock.settimeout(2.0)
            try:
                data, addr = srv.sock.recvfrom(1500)
                srv.sock.sendto(stale, addr)                 # the late one first
                srv.sock.sendto(handle_datagram(data, ahu), addr)
            except OSError:
                pass

        threading.Thread(target=run, daemon=True).start()
        try:
            with BACnetIP("127.0.0.1", srv.port, timeout=2.0) as c:
                v = c.read_property(bacnet.ANALOG_INPUT, 1)
            self.assertAlmostEqual(v, 61.5, places=2)
        finally:
            srv.shutdown()


class ExtendedLength(unittest.TestCase):
    """A length field of 5 means "the length follows", not "five bytes".

    This was a real bug in this decoder. Every string over four characters
    takes the extended path, and reading 5 as a literal length misaligns
    every tag after it — so the failure surfaced as "missing closing tag",
    pointing at the end of the buffer rather than at the string.
    """

    def test_literal_lengths_are_unchanged(self):
        self.assertEqual(bacnet._tag_length(bytes([0x21, 0x40])), (1, 1))
        self.assertEqual(bacnet._tag_length(bytes([0x44, 0, 0, 0, 0])), (4, 1))

    def test_lvt_of_five_reads_the_following_byte(self):
        self.assertEqual(bacnet._tag_length(bytes([0x75, 0x08])), (8, 2))

    def test_two_and_four_byte_extended_forms(self):
        self.assertEqual(
            bacnet._tag_length(bytes([0x75, 254, 0x01, 0x00])), (256, 4))
        self.assertEqual(
            bacnet._tag_length(bytes([0x75, 255, 0, 0x01, 0x00, 0x00])),
            (65536, 6))

    def test_opening_and_closing_tags_are_not_lengths(self):
        with self.assertRaises(BACnetTransport):
            bacnet._tag_length(bytes([0x3E]))

    def test_a_long_string_round_trips(self):
        ahu = AHU()
        name = "AHU-1-SUPPLY-AIR-TEMPERATURE-SENSOR-PRIMARY"
        ahu.points[(bacnet.ANALOG_INPUT, 1)]["name"] = name
        reply = handle_datagram(
            bacnet.encode_read_property(11, bacnet.ANALOG_INPUT, 1,
                                        bacnet.PROP_OBJECT_NAME), ahu)
        self.assertEqual(
            bacnet.decode_read_property_ack(reply, expect_invoke=11), name)

    def test_a_short_string_round_trips(self):
        ahu = AHU()
        reply = handle_datagram(
            bacnet.encode_read_property(12, bacnet.ANALOG_INPUT, 1,
                                        bacnet.PROP_OBJECT_NAME), ahu)
        self.assertEqual(
            bacnet.decode_read_property_ack(reply, expect_invoke=12), "SA-TEMP")


class NoWritePath(unittest.TestCase):
    """Checked against the parse tree, not the text.

    An earlier version of this test grepped the source for "WriteProperty"
    and failed on the docstring that explains why there is no WriteProperty.
    A guard that cannot tell code from prose is a guard that gets deleted.
    """

    def test_no_write_callable_is_defined(self):
        import ast
        with open("dcmon/bacnet.py") as fh:
            tree = ast.parse(fh.read())
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertTrue(names, "parsed no functions at all")
        offenders = [n for n in names if "write" in n.lower()]
        self.assertEqual(offenders, [], "write callables present: %r" % offenders)

    def test_no_write_service_constant_is_assigned(self):
        import ast
        with open("dcmon/bacnet.py") as fh:
            tree = ast.parse(fh.read())
        assigned = [t.id for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)]
        self.assertNotIn("SERVICE_WRITE_PROPERTY", assigned)

    def test_the_client_exposes_no_write_attribute(self):
        self.assertFalse(hasattr(BACnetIP, "write_property"))
        self.assertFalse([a for a in dir(BACnetIP) if "write" in a.lower()])


if __name__ == "__main__":
    unittest.main()
