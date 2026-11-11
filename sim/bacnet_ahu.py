"""A BACnet/IP device that behaves like an air handler.

Answers ReadProperty for present-value, reliability, out-of-service,
object-name and units, and returns a proper Error PDU for objects and
properties it does not have — so the client's error path is exercised against
real bytes rather than a mock.

Run standalone:  python3 -m sim.bacnet_ahu --port 47808
"""
import argparse
import math
import socket
import struct
import threading
import time

from dcmon import bacnet

PROP_NAMES = {
    bacnet.PROP_PRESENT_VALUE: "present-value",
    bacnet.PROP_RELIABILITY: "reliability",
    bacnet.PROP_OUT_OF_SERVICE: "out-of-service",
    bacnet.PROP_OBJECT_NAME: "object-name",
    bacnet.PROP_UNITS: "units",
}


class AHU:
    """Four points, the ones a technician actually looks at first."""

    def __init__(self):
        self.t0 = time.time()
        self.points = {
            (bacnet.ANALOG_INPUT, 1): {
                "name": "SA-TEMP", "units": 64,       # degrees-fahrenheit
                "value": lambda s: s._wave(55.0, 1.5, 60),
                "reliability": 0, "out_of_service": False,
            },
            (bacnet.ANALOG_INPUT, 2): {
                "name": "RA-TEMP", "units": 64,
                "value": lambda s: s._wave(74.0, 2.0, 95),
                "reliability": 0, "out_of_service": False,
            },
            (bacnet.ANALOG_VALUE, 3): {
                "name": "SA-TEMP-SP", "units": 64,
                "value": lambda s: 55.0,
                "reliability": 0, "out_of_service": False,
            },
            (bacnet.BINARY_INPUT, 4): {
                "name": "SF-STATUS", "units": None,
                "value": lambda s: True,
                "reliability": 0, "out_of_service": False,
            },
        }

    def _wave(self, base, amp, period):
        return base + amp * math.sin(((time.time() - self.t0) / period) * 2 * math.pi)

    def set(self, key, field, value):
        self.points[key][field] = value if field != "value" else (lambda s: value)


def _app_real(v):
    return bytes([0x44]) + struct.pack(">f", v)


def _app_unsigned(v):
    if v <= 0xFF:
        return bytes([0x21, v])
    if v <= 0xFFFF:
        return bytes([0x22]) + struct.pack(">H", v)
    return bytes([0x24]) + struct.pack(">I", v)


def _app_enumerated(v):
    return bytes([0x91, v & 0xFF])


def _app_boolean(v):
    return bytes([0x11 if v else 0x10])


def _app_charstring(s):
    raw = s.encode("utf-8")
    body = bytes([0x00]) + raw            # character set 0 = UTF-8
    if len(body) < 5:
        return bytes([0x70 | len(body)]) + body
    return bytes([0x75, len(body)]) + body


def _encode_value(prop_id, point, ahu):
    if prop_id == bacnet.PROP_PRESENT_VALUE:
        v = point["value"](ahu)
        if isinstance(v, bool):
            return _app_enumerated(1 if v else 0)
        return _app_real(float(v))
    if prop_id == bacnet.PROP_RELIABILITY:
        return _app_enumerated(point["reliability"])
    if prop_id == bacnet.PROP_OUT_OF_SERVICE:
        return _app_boolean(point["out_of_service"])
    if prop_id == bacnet.PROP_OBJECT_NAME:
        return _app_charstring(point["name"])
    if prop_id == bacnet.PROP_UNITS:
        if point["units"] is None:
            return None
        return _app_unsigned(point["units"])
    return None


def _wrap(apdu):
    npdu = bytes([0x01, 0x00])
    total = 4 + len(npdu) + len(apdu)
    return struct.pack(">BBH", bacnet.BVLL_TYPE,
                       bacnet.ORIGINAL_UNICAST, total) + npdu + apdu


def _complex_ack(invoke, obj_type, instance, prop_id, encoded):
    apdu = bytes([bacnet.PDU_COMPLEX_ACK, invoke, bacnet.SERVICE_READ_PROPERTY])
    apdu += bytes([0x0C]) + bacnet.encode_object_id(obj_type, instance)
    apdu += bytes([0x19, prop_id])
    apdu += bytes([0x3E]) + encoded + bytes([0x3F])
    return _wrap(apdu)


def _error(invoke, err_class, err_code):
    apdu = bytes([bacnet.PDU_ERROR, invoke, bacnet.SERVICE_READ_PROPERTY])
    apdu += _app_enumerated(err_class) + _app_enumerated(err_code)
    return _wrap(apdu)


def handle_datagram(data, ahu):
    """Parse a ReadProperty request and build the reply. Returns bytes or None."""
    try:
        apdu = bacnet._strip_bvll_npdu(data)
    except bacnet.BACnetTransport:
        return None
    if not apdu or (apdu[0] & 0xF0) != bacnet.PDU_CONFIRMED_REQUEST:
        return None
    invoke, service = apdu[2], apdu[3]
    if service != bacnet.SERVICE_READ_PROPERTY:
        return _error(invoke, 6, 9)          # services / service-request-denied
    if apdu[4] != 0x0C:
        return None
    obj = struct.unpack(">I", apdu[5:9])[0]
    obj_type, instance = obj >> 22, obj & 0x3FFFFF
    prop_id = apdu[10]

    point = ahu.points.get((obj_type, instance))
    if point is None:
        return _error(invoke, 1, 25)         # object / unknown-object
    encoded = _encode_value(prop_id, point, ahu)
    if encoded is None:
        return _error(invoke, 2, 32)         # property / unknown-property
    return _complex_ack(invoke, obj_type, instance, prop_id, encoded)


class AHUServer:
    def __init__(self, host="127.0.0.1", port=0, ahu=None):
        self.ahu = ahu or AHU()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.dropped = 0          # set >0 to make it ignore the next N requests

    def serve_forever(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                return
            if self.dropped > 0:
                self.dropped -= 1
                continue
            reply = handle_datagram(data, self.ahu)
            if reply:
                self.sock.sendto(reply, addr)

    def serve_in_thread(self):
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self):
        self._stop.set()
        self.sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47808)
    args = ap.parse_args()
    srv = AHUServer(port=args.port)
    print("AHU on 127.0.0.1:%d — AI1 SA-TEMP, AI2 RA-TEMP, AV3 SP, BI4 fan"
          % srv.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
