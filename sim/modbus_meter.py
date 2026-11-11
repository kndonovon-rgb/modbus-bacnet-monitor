"""A Modbus TCP server that behaves like a three-phase power meter.

Exists so the monitor can be run and tested without a meter, a gateway or a
VLAN. It answers function codes 3 and 4, raises exception 2 for addresses
outside its map, and deliberately publishes its floats **low word first**,
because a good share of real meters do and a client that assumes otherwise
reads 480 V as about 3.6e-41.

Run standalone:  python3 -m sim.modbus_meter --port 5020
"""
import argparse
import socketserver
import struct
import threading
import time

# Documented (4xxxx) -> (type, value provider). The wire address is this
# minus 40001.
START = 40001


def _f32_low_first(value):
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return [lo, hi]


class Meter:
    """The register map, plus a little drift so trends are not flat lines."""

    def __init__(self, seed=0.0):
        self.t0 = time.time()
        self.seed = seed
        self.overrides = {}

    def _wave(self, base, amplitude, period):
        import math
        phase = (time.time() - self.t0 + self.seed) / period
        return base + amplitude * math.sin(phase * 2 * math.pi)

    def registers(self):
        regs = {}

        def put(documented, words):
            for i, w in enumerate(words):
                regs[documented - START + i] = w & 0xFFFF

        put(40001, _f32_low_first(self.overrides.get("voltage",
                                                     self._wave(480.0, 3.0, 90))))
        put(40003, _f32_low_first(self.overrides.get("current",
                                                     self._wave(220.0, 15.0, 70))))
        put(40005, _f32_low_first(self.overrides.get("kw",
                                                     self._wave(158.0, 12.0, 120))))
        put(40007, [int(round(self.overrides.get("frequency", 60.0) * 100))])
        kwh = int(self.overrides.get("kwh", 1_250_000 + (time.time() - self.t0) * 4))
        put(40009, [(kwh >> 16) & 0xFFFF, kwh & 0xFFFF])
        return regs

    def set(self, name, value):
        self.overrides[name] = value


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5.0)
        while True:
            try:
                head = self._recv(7)
            except (OSError, EOFError):
                return
            if head is None:
                return
            txn, proto, length, unit = struct.unpack(">HHHB", head)
            body = self._recv(length - 1)
            if body is None:
                return
            fc = body[0]
            if fc not in (3, 4):
                self._exception(txn, unit, fc, 1)
                continue
            addr, count = struct.unpack(">HH", body[1:5])
            regs = self.server.meter.registers()
            if any((addr + i) not in regs for i in range(count)):
                self._exception(txn, unit, fc, 2)
                continue
            values = [regs[addr + i] for i in range(count)]
            data = struct.pack(">%dH" % count, *values)
            pdu = bytes([fc, len(data)]) + data
            self.request.sendall(
                struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu)

    def _exception(self, txn, unit, fc, code):
        pdu = bytes([fc | 0x80, code])
        self.request.sendall(
            struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.request.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf


class MeterServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=5020, meter=None):
        super().__init__((host, port), _Handler)
        self.meter = meter or Meter()


def serve_in_thread(port=0):
    """Start on an ephemeral port. Returns (server, thread, port)."""
    srv = MeterServer(port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, srv.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5020)
    args = ap.parse_args()
    srv = MeterServer(port=args.port)
    print("power meter on 127.0.0.1:%d (floats are LOW WORD FIRST)" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
