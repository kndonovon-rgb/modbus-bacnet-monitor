"""A BACnet/IP ReadProperty client, encoded by hand on a UDP socket.

There is no library here. The request and response bytes are built and parsed
in this file, because a controls integrator who cannot read an APDU in a packet
capture is holding a vendor tool, not a skill.

Only ReadProperty is implemented, and that is deliberate. BACnet writes go
through a **priority array**: writing present-value at a priority and never
releasing it leaves the point commanded, and the BMS can no longer control it.
Technicians have frozen coils and cooked rooms that way, and the override
survives the technician going home. A monitoring tool has no business owning
that risk, so WriteProperty does not exist in this module.

    BVLL              NPDU          APDU (confirmed-request)
    +----+----+----+  +----+----+   +----+----+----+----+
    0x81 func  len    ver  ctrl      type maxapdu invoke svc=12

Service data is context-tagged: tag 0 is the object identifier (10 bits of
object type, 22 bits of instance, packed into 4 bytes), tag 1 is the property
identifier. The reply wraps its value in opening/closing tag 3.
"""
import socket
import struct

BVLL_TYPE = 0x81
ORIGINAL_UNICAST = 0x0A

PDU_CONFIRMED_REQUEST = 0x00
PDU_COMPLEX_ACK = 0x30
PDU_ERROR = 0x50
PDU_REJECT = 0x60
PDU_ABORT = 0x70

SERVICE_READ_PROPERTY = 12

# Object types (ASHRAE 135, clause 21)
ANALOG_INPUT = 0
ANALOG_OUTPUT = 1
ANALOG_VALUE = 2
BINARY_INPUT = 3
BINARY_OUTPUT = 4
BINARY_VALUE = 5
DEVICE = 8
MULTI_STATE_VALUE = 19

OBJECT_TYPES = {
    "analog-input": ANALOG_INPUT, "analog-output": ANALOG_OUTPUT,
    "analog-value": ANALOG_VALUE, "binary-input": BINARY_INPUT,
    "binary-output": BINARY_OUTPUT, "binary-value": BINARY_VALUE,
    "device": DEVICE, "multi-state-value": MULTI_STATE_VALUE,
}

# Property identifiers
PROP_OBJECT_NAME = 77
PROP_OUT_OF_SERVICE = 81
PROP_PRESENT_VALUE = 85
PROP_RELIABILITY = 103
PROP_STATUS_FLAGS = 111
PROP_UNITS = 117

ERROR_CLASSES = {0: "device", 1: "object", 2: "property", 4: "resources",
                 5: "security", 6: "services", 7: "vt", 8: "communication"}
ERROR_CODES = {25: "unknown-object", 32: "unknown-property",
               31: "unknown-device", 42: "value-out-of-range",
               27: "no-space-to-write-property", 40: "write-access-denied"}

# Reliability is an enumeration where 0 means "no fault detected". Anything
# else means the device itself is telling you not to trust present-value.
RELIABILITY_NO_FAULT = 0


class BACnetError(Exception):
    """The device answered with an Error, Reject or Abort PDU."""


class BACnetTransport(Exception):
    """No answer, or an answer that is not a BACnet reply to our request."""


# ---------------------------------------------------------------- encoding
def _context_tag(number, length):
    if length > 4:
        raise ValueError("use opening/closing tags for lengths over 4")
    return bytes([(number << 4) | 0x08 | length])


def encode_object_id(obj_type, instance):
    if not 0 <= obj_type < 1024:
        raise ValueError("object type must fit 10 bits")
    if not 0 <= instance < 0x3FFFFF:
        raise ValueError("instance must fit 22 bits")
    return struct.pack(">I", (obj_type << 22) | instance)


def encode_read_property(invoke_id, obj_type, instance,
                         prop_id=PROP_PRESENT_VALUE, max_apdu=5):
    """Build a complete BACnet/IP Original-Unicast ReadProperty request."""
    apdu = bytes([PDU_CONFIRMED_REQUEST, max_apdu, invoke_id & 0xFF,
                  SERVICE_READ_PROPERTY])
    apdu += _context_tag(0, 4) + encode_object_id(obj_type, instance)
    apdu += _context_tag(1, 1) + bytes([prop_id])
    npdu = bytes([0x01, 0x04])                 # version 1, expecting-reply
    total = 4 + len(npdu) + len(apdu)
    bvll = struct.pack(">BBH", BVLL_TYPE, ORIGINAL_UNICAST, total)
    return bvll + npdu + apdu


# ---------------------------------------------------------------- decoding
def _strip_bvll_npdu(data):
    if len(data) < 6:
        raise BACnetTransport("datagram too short (%d bytes)" % len(data))
    if data[0] != BVLL_TYPE:
        raise BACnetTransport("not BACnet/IP (first byte 0x%02x)" % data[0])
    declared = struct.unpack(">H", data[2:4])[0]
    if declared != len(data):
        raise BACnetTransport(
            "BVLL says %d bytes, datagram is %d" % (declared, len(data)))
    npdu = data[4:]
    if npdu[0] != 0x01:
        raise BACnetTransport("NPDU version %d" % npdu[0])
    control = npdu[1]
    offset = 2
    if control & 0x20:                          # DNET present
        offset += 3 + npdu[offset + 2]
    if control & 0x08:                          # SNET present
        offset += 3 + npdu[offset + 2]
    if control & 0x20:
        offset += 1                             # hop count
    return npdu[offset:]


def _tag_length(buf):
    """Decode the length/value/type field, which is overloaded three ways.

    An LVT of 0-4 is a literal byte count. **An LVT of 5 does not mean five
    bytes** — it means the real length follows: one byte, or 254 then two
    bytes, or 255 then four bytes. Any string longer than four characters
    takes this path, so a decoder that reads 5 as a literal length consumes
    the wrong number of bytes and every tag after it is misaligned. The
    symptom is not "bad string", it is a parse failure further down the
    buffer, which sends you looking in the wrong place.

    Returns (length, header_bytes).
    """
    lvt = buf[0] & 0x07
    if lvt < 5:
        return lvt, 1
    if lvt > 5:
        raise BACnetTransport("LVT %d is an opening/closing tag" % lvt)
    if len(buf) < 2:
        raise BACnetTransport("truncated extended length")
    first = buf[1]
    if first < 254:
        return first, 2
    if first == 254:
        return int.from_bytes(buf[2:4], "big"), 4
    return int.from_bytes(buf[2:6], "big"), 6


def _decode_app_tagged(buf):
    """One application-tagged primitive. Returns (value, bytes_consumed)."""
    tag_byte = buf[0]
    if tag_byte & 0x08:
        raise BACnetTransport("expected an application tag, got context %d"
                              % (tag_byte >> 4))
    tag = tag_byte >> 4
    if tag == 1:        # Boolean carries its value in the LVT, not a length
        return (tag_byte & 0x07) == 1, 1
    if tag == 0:                                # Null
        return None, 1
    length, header = _tag_length(buf)
    body = buf[header:header + length]
    if len(body) != length:
        raise BACnetTransport("truncated value")
    if tag == 2:                                # Unsigned
        return int.from_bytes(body, "big"), header + length
    if tag == 3:                                # Signed
        return int.from_bytes(body, "big", signed=True), header + length
    if tag == 4:                                # Real
        if length != 4:
            raise BACnetTransport("Real with length %d" % length)
        return struct.unpack(">f", body)[0], header + length
    if tag == 5:                                # Double
        return struct.unpack(">d", body)[0], header + length
    if tag == 7:                                # CharacterString
        return body[1:].decode("utf-8", "replace"), header + length
    if tag == 8:                                # BitString
        unused = body[0]
        bits = []
        for byte in body[1:]:
            bits.extend((byte >> (7 - i)) & 1 for i in range(8))
        return bits[:len(bits) - unused] if unused else bits, header + length
    if tag == 9:                                # Enumerated
        return int.from_bytes(body, "big"), header + length
    raise BACnetTransport("unsupported application tag %d" % tag)


def decode_read_property_ack(data, expect_invoke=None):
    """Parse a reply. Returns the decoded property value.

    Raises BACnetError for Error/Reject/Abort, BACnetTransport for anything
    that is not an answer to the request we sent.
    """
    apdu = _strip_bvll_npdu(data)
    ptype = apdu[0] & 0xF0

    if ptype == PDU_ERROR:
        invoke = apdu[1]
        _check_invoke(invoke, expect_invoke)
        cls, consumed = _decode_app_tagged(apdu[3:])
        code, _ = _decode_app_tagged(apdu[3 + consumed:])
        raise BACnetError("error %s / %s" % (
            ERROR_CLASSES.get(cls, cls), ERROR_CODES.get(code, code)))
    if ptype == PDU_REJECT:
        _check_invoke(apdu[1], expect_invoke)
        raise BACnetError("reject, reason %d" % apdu[2])
    if ptype == PDU_ABORT:
        _check_invoke(apdu[1], expect_invoke)
        raise BACnetError("abort, reason %d" % apdu[2])
    if ptype != PDU_COMPLEX_ACK:
        raise BACnetTransport("PDU type 0x%02x is not a ReadProperty ack" % ptype)

    if apdu[0] & 0x08:
        raise BACnetTransport("segmented response; not supported")
    invoke, service = apdu[1], apdu[2]
    _check_invoke(invoke, expect_invoke)
    if service != SERVICE_READ_PROPERTY:
        raise BACnetTransport("ack for service %d, not ReadProperty" % service)

    i = 3
    # context 0: object identifier, context 1: property identifier
    if apdu[i] != 0x0C:
        raise BACnetTransport("expected object-id tag, got 0x%02x" % apdu[i])
    i += 5
    if apdu[i] & 0xF8 != 0x18:
        raise BACnetTransport("expected property-id tag, got 0x%02x" % apdu[i])
    i += 1 + (apdu[i] & 0x07)
    if apdu[i] == 0x29:                         # optional array index
        i += 2
    if apdu[i] != 0x3E:
        raise BACnetTransport("expected opening tag 3, got 0x%02x" % apdu[i])
    i += 1
    value, consumed = _decode_app_tagged(apdu[i:])
    i += consumed
    if i >= len(apdu) or apdu[i] != 0x3F:
        raise BACnetTransport("missing closing tag 3")
    return value


def _check_invoke(got, expected):
    """UDP has no ordering. A late reply to the previous poll looks exactly
    like a reply to this one, and believing it reports a stale value as fresh.
    """
    if expected is not None and got != expected:
        raise BACnetTransport("invoke id %d, expected %d" % (got, expected))


# ---------------------------------------------------------------- client
class BACnetIP:
    def __init__(self, host, port=47808, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._invoke = 0
        self._sock = None

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self.timeout)
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def read_property(self, obj_type, instance, prop_id=PROP_PRESENT_VALUE):
        if self._sock is None:
            raise BACnetTransport("not connected")
        if isinstance(obj_type, str):
            if obj_type not in OBJECT_TYPES:
                raise ValueError("unknown object type %r" % obj_type)
            obj_type = OBJECT_TYPES[obj_type]
        self._invoke = (self._invoke + 1) % 256
        invoke = self._invoke
        frame = encode_read_property(invoke, obj_type, instance, prop_id)
        try:
            self._sock.sendto(frame, (self.host, self.port))
            # Drain late datagrams from earlier polls rather than trusting the
            # first thing that arrives.
            for _ in range(4):
                data, _addr = self._sock.recvfrom(1500)
                try:
                    return decode_read_property_ack(data, expect_invoke=invoke)
                except BACnetTransport:
                    continue
            raise BACnetTransport("no reply matching invoke id %d" % invoke)
        except socket.timeout as e:
            raise BACnetTransport("timeout after %.1fs" % self.timeout) from e
        except OSError as e:
            raise BACnetTransport(str(e)) from e

    def trustworthy(self, obj_type, instance):
        """present-value, plus the two properties that say whether to believe it.

        A point can report a perfectly plausible number while the device has
        already flagged it as unreliable, or while it is out-of-service and the
        value is whatever a technician last typed in for commissioning. Reading
        present-value alone cannot tell those apart from a live measurement.
        """
        value = self.read_property(obj_type, instance, PROP_PRESENT_VALUE)
        try:
            reliability = self.read_property(obj_type, instance, PROP_RELIABILITY)
        except BACnetError:
            reliability = None          # optional property; absence is not a fault
        try:
            out_of_service = self.read_property(obj_type, instance,
                                                PROP_OUT_OF_SERVICE)
        except BACnetError:
            out_of_service = None
        return {
            "value": value,
            "reliability": reliability,
            "out_of_service": out_of_service,
            "trusted": (reliability in (None, RELIABILITY_NO_FAULT)
                        and out_of_service in (None, False)),
        }
