"""Register decoding, including the two bugs that cause most bad readings."""
import struct
import unittest

from dcmon import decode


class WordOrder(unittest.TestCase):
    """The failure mode here is not garbage, it is a plausible-looking number."""

    def setUp(self):
        hi, lo = struct.unpack(">HH", struct.pack(">f", 480.0))
        self.high_first = [hi, lo]
        self.low_first = [lo, hi]

    def test_correct_order_reads_the_value(self):
        self.assertAlmostEqual(
            decode.decode_f32(self.high_first, "high_first"), 480.0, places=3)
        self.assertAlmostEqual(
            decode.decode_f32(self.low_first, "low_first"), 480.0, places=3)

    def test_wrong_order_does_not_merely_look_wrong(self):
        """It produces a number a naive high-limit check calls normal.

        Read a 480 V meter with the word order reversed and you get a
        denormal near zero — which is below every high limit anyone would
        set, so the alarm never fires and the point reads 'healthy'.
        """
        wrong = decode.decode_f32(self.low_first, "high_first")
        self.assertNotAlmostEqual(wrong, 480.0, places=1)
        self.assertLess(abs(wrong), 1.0)
        self.assertLess(abs(wrong), 500.0)      # i.e. a high limit would pass it

    def test_unknown_order_is_refused(self):
        with self.assertRaises(ValueError):
            decode.decode_f32(self.high_first, "middle_endian")

    def test_two_registers_are_required(self):
        with self.assertRaises(ValueError):
            decode.decode_f32([0x4380], "high_first")


class IntegerTypes(unittest.TestCase):
    def test_unsigned_and_signed_16(self):
        self.assertEqual(decode.decode_u16([0xFFFF]), 65535)
        self.assertEqual(decode.decode_s16([0xFFFF]), -1)
        self.assertEqual(decode.decode_s16([0x8000]), -32768)
        self.assertEqual(decode.decode_s16([0x7FFF]), 32767)

    def test_unsigned_and_signed_32(self):
        self.assertEqual(decode.decode_u32([0x0001, 0x0000]), 65536)
        self.assertEqual(decode.decode_u32([0x0000, 0x0001], "low_first"), 65536)
        self.assertEqual(decode.decode_s32([0xFFFF, 0xFFFF]), -1)

    def test_scale_is_applied(self):
        # A frequency register published as 6000 meaning 60.00 Hz.
        self.assertAlmostEqual(decode.decode_u16([6000], scale=0.01), 60.0)

    def test_dispatch_and_width(self):
        self.assertEqual(decode.WIDTH["f32"], 2)
        self.assertEqual(decode.WIDTH["u16"], 1)
        self.assertAlmostEqual(decode.decode("u16", [6000], scale=0.01), 60.0)
        with self.assertRaises(ValueError):
            decode.decode("f64", [0, 0])


class DocumentedAddressing(unittest.TestCase):
    """40001 in the manual is address 0 on the wire."""

    def test_reference_base_is_removed(self):
        self.assertEqual(decode.data_address(40001), 0)
        self.assertEqual(decode.data_address(40007), 6)

    def test_off_by_one_reads_the_neighbouring_point(self):
        """Using the documented number directly is the classic integration bug.

        It does not error. It silently returns a different register, which is
        how voltage ends up reported as current.
        """
        self.assertNotEqual(decode.data_address(40003), 40003)

    def test_input_registers_have_their_own_base(self):
        self.assertEqual(decode.data_address(30001, base=30001), 0)

    def test_below_the_base_is_refused(self):
        with self.assertRaises(ValueError):
            decode.data_address(39999)


if __name__ == "__main__":
    unittest.main()
