"""A Modbus TCP read client, on stdlib sockets.

Only the read path exists. There is no function code 6 or 16 in this file and
there is not going to be: a monitoring tool that can write to a power meter or
a chiller controller is a tool that can take a data centre down, and "it only
writes when you tell it to" is not a safety argument anyone should accept at
3am. The absence is the feature.

Wire format (function 3 / 4):

    MBAP header        PDU
    +----+----+----+   +----+----+----+
    txn  proto len  uid  fc   addr  qty
     2    2    2    1    1     2     2

The response repeats the header, then `fc`, then a byte count, then the data.
An exception sets the high bit of the function code (fc | 0x80) and returns a
single exception byte instead.
"""
import socket
import struct

READ_HOLDING = 3
READ_INPUT = 4

EXCEPTIONS = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "slave device failure",
    5: "acknowledge",
    6: "slave device busy",
    8: "memory parity error",
    10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}

MAX_REGISTERS = 125          # 125 * 2 bytes fits the 253-byte PDU limit


class ModbusError(Exception):
    """A protocol-level refusal: the device answered, and said no."""


class ModbusTransport(Exception):
    """The device did not answer, or answered something that is not Modbus."""


class ModbusTCP:
    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self._txn = 0
        self._sock = None

    # -- connection -------------------------------------------------------
    def connect(self):
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
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

    # -- reads ------------------------------------------------------------
    def read(self, address, count, function=READ_HOLDING):
        """Read `count` registers starting at `address`. Returns a list of ints."""
        if not 1 <= count <= MAX_REGISTERS:
            raise ValueError("count must be 1..%d, got %d" % (MAX_REGISTERS, count))
        if self._sock is None:
            raise ModbusTransport("not connected")

        self._txn = (self._txn + 1) % 0x10000
        txn = self._txn
        pdu = struct.pack(">BHH", function, address, count)
        frame = struct.pack(">HHHB", txn, 0, len(pdu) + 1, self.unit) + pdu
        try:
            self._sock.sendall(frame)
            head = self._recv_exactly(7)
            r_txn, proto, length, _unit = struct.unpack(">HHHB", head)
            if proto != 0:
                raise ModbusTransport("protocol id %d is not Modbus" % proto)
            if length < 1:
                raise ModbusTransport("declared length %d is impossible" % length)
            body = self._recv_exactly(length - 1)
        except socket.timeout as e:
            raise ModbusTransport("timeout after %.1fs" % self.timeout) from e
        except OSError as e:
            raise ModbusTransport(str(e)) from e

        # A mismatched transaction id means we are reading someone else's
        # answer — on a shared gateway that silently returns another point's
        # data, which is worse than an error.
        if r_txn != txn:
            raise ModbusTransport("transaction id %d, expected %d" % (r_txn, txn))

        fc = body[0]
        if fc & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise ModbusError("%s (exception %d)" % (EXCEPTIONS.get(code, "unknown"), code))
        if fc != function:
            raise ModbusTransport("function %d in reply to %d" % (fc, function))

        byte_count = body[1]
        data = body[2:2 + byte_count]
        if len(data) != byte_count or byte_count != count * 2:
            raise ModbusTransport(
                "expected %d data bytes, got %d" % (count * 2, len(data)))
        return list(struct.unpack(">%dH" % count, data))

    def _recv_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusTransport("connection closed mid-frame")
            buf += chunk
        return buf
