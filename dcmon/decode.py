"""Turning raw 16-bit registers into engineering units.

This module is small and it is where most real integration bugs live. Modbus
has no type system: a register is 16 bits and the meaning is entirely in the
vendor's documentation, which is often wrong.
"""
import struct

WORD_ORDERS = ("high_first", "low_first")


def decode_u16(regs, scale=1.0):
    return regs[0] * scale


def decode_s16(regs, scale=1.0):
    v = regs[0]
    if v >= 0x8000:
        v -= 0x10000
    return v * scale


def decode_u32(regs, word_order="high_first", scale=1.0):
    hi, lo = _order(regs, word_order)
    return ((hi << 16) | lo) * scale


def decode_s32(regs, word_order="high_first", scale=1.0):
    hi, lo = _order(regs, word_order)
    v = (hi << 16) | lo
    if v >= 0x80000000:
        v -= 0x100000000
    return v * scale


def decode_f32(regs, word_order="high_first", scale=1.0):
    """IEEE-754 single precision spread across two registers.

    `word_order` is not pedantry. The IEEE bytes within each register are
    big-endian on the wire, but which register holds the high word is a vendor
    decision, and plenty of meters put it second. Get it wrong and a 480 V
    reading does not come back as garbage — it comes back as something like
    2.3e-41 or 1.2e13, which a naive threshold check will happily compare
    against 500 and call normal.
    """
    hi, lo = _order(regs, word_order)
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0] * scale


def _order(regs, word_order):
    if len(regs) < 2:
        raise ValueError("need two registers, got %d" % len(regs))
    if word_order == "high_first":
        return regs[0], regs[1]
    if word_order == "low_first":
        return regs[1], regs[0]
    raise ValueError("word_order must be one of %r" % (WORD_ORDERS,))


DECODERS = {
    "u16": decode_u16,
    "s16": decode_s16,
    "u32": decode_u32,
    "s32": decode_s32,
    "f32": decode_f32,
}

# How many registers each type consumes.
WIDTH = {"u16": 1, "s16": 1, "u32": 2, "s32": 2, "f32": 2}


def decode(kind, regs, word_order="high_first", scale=1.0):
    if kind not in DECODERS:
        raise ValueError("unknown type %r" % kind)
    fn = DECODERS[kind]
    if kind in ("u16", "s16"):
        return fn(regs, scale=scale)
    return fn(regs, word_order=word_order, scale=scale)


def data_address(documented, base=40001):
    """Vendor docs say 40001; the wire wants 0.

    The 4xxxx / 3xxxx prefixes in vendor manuals are a 1-based *reference*
    convention from Modicon, not the address that goes in the PDU. Holding
    register 40001 is address 0 with function code 3. This one line is
    responsible for an enormous share of "the meter is returning the wrong
    value" integration calls, and the usual symptom is every reading being
    one register off, so voltage reads as current.
    """
    addr = documented - base
    if addr < 0:
        raise ValueError("%d is below the %d reference base" % (documented, base))
    return addr
