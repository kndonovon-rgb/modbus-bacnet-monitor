"""The Modbus client, driven against a real socket server speaking real frames."""
import socket
import struct
import threading
import time
import unittest

from dcmon import decode
from dcmon.modbus import ModbusTCP, ModbusError, ModbusTransport
from sim.modbus_meter import Meter, MeterServer, serve_in_thread


class AgainstTheSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.meter = Meter()
        cls.srv = MeterServer(port=0, meter=cls.meter)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self):
        return ModbusTCP("127.0.0.1", self.port, unit=1, timeout=3.0)

    def test_reads_voltage_through_the_whole_stack(self):
        """Pin the meter, read it, decode it, and get the number back.

        The simulator publishes low word first, as many real meters do, so
        this also proves the client is not quietly assuming the easy case.
        """
        self.meter.set("voltage", 481.5)
        with self.client() as c:
            regs = c.read(decode.data_address(40001), 2)
        self.assertEqual(len(regs), 2)
        self.assertAlmostEqual(
            decode.decode_f32(regs, "low_first"), 481.5, places=2)

    def test_the_same_registers_with_the_wrong_word_order_pass_a_high_limit(self):
        self.meter.set("voltage", 480.0)
        with self.client() as c:
            regs = c.read(decode.data_address(40001), 2)
        wrong = decode.decode_f32(regs, "high_first")
        self.assertLess(wrong, 500.0)
        self.assertNotAlmostEqual(wrong, 480.0, places=1)

    def test_scaled_integer_register(self):
        self.meter.set("frequency", 59.97)
        with self.client() as c:
            regs = c.read(decode.data_address(40007), 1)
        self.assertAlmostEqual(decode.decode_u16(regs, scale=0.01), 59.97, places=2)

    def test_illegal_data_address_raises_a_protocol_error(self):
        """The device answered and said no. That is different from silence."""
        with self.client() as c:
            with self.assertRaises(ModbusError) as ctx:
                c.read(9000, 2)
        self.assertIn("illegal data address", str(ctx.exception))

    def test_illegal_function_is_reported(self):
        with self.client() as c:
            with self.assertRaises(ModbusError):
                c.read(0, 1, function=99)

    def test_count_bounds_are_checked_before_the_wire(self):
        with self.client() as c:
            with self.assertRaises(ValueError):
                c.read(0, 0)
            with self.assertRaises(ValueError):
                c.read(0, 126)

    def test_reading_without_connecting_is_a_transport_error(self):
        with self.assertRaises(ModbusTransport):
            ModbusTCP("127.0.0.1", self.port).read(0, 1)

    def test_many_sequential_reads_keep_their_transaction_ids(self):
        with self.client() as c:
            for _ in range(40):
                self.assertEqual(len(c.read(0, 2)), 2)


class WhenTheDeviceMisbehaves(unittest.TestCase):
    """Hand-built bad replies. A monitor must not accept these as readings."""

    def _server(self, reply_fn):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def run():
            try:
                conn, _ = srv.accept()
                head = conn.recv(7)
                body = conn.recv(256)
                out = reply_fn(head, body)
                if out:
                    conn.sendall(out)
                    conn.close()
                else:
                    # Stay silent with the connection OPEN. Closing it would
                    # give the client a clean EOF, which is a different failure
                    # from a device that accepts the frame and never answers —
                    # and the latter is what a hung controller actually does.
                    time.sleep(3.0)
                    conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        threading.Thread(target=run, daemon=True).start()
        return port

    def test_mismatched_transaction_id_is_rejected(self):
        """On a shared gateway this is another point's data, not ours."""
        def reply(head, body):
            txn, proto, length, unit = struct.unpack(">HHHB", head)
            data = struct.pack(">HH", 0x4380, 0x0000)
            pdu = bytes([3, len(data)]) + data
            return struct.pack(">HHHB", txn + 7, 0, len(pdu) + 1, unit) + pdu

        port = self._server(reply)
        with ModbusTCP("127.0.0.1", port, timeout=2.0) as c:
            with self.assertRaises(ModbusTransport) as ctx:
                c.read(0, 2)
        self.assertIn("transaction id", str(ctx.exception))

    def test_short_data_is_rejected_rather_than_padded(self):
        def reply(head, body):
            txn, proto, length, unit = struct.unpack(">HHHB", head)
            data = struct.pack(">H", 0x4380)        # one register, two asked for
            pdu = bytes([3, 4]) + data              # byte count lies
            return struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu

        port = self._server(reply)
        with ModbusTCP("127.0.0.1", port, timeout=2.0) as c:
            with self.assertRaises(ModbusTransport):
                c.read(0, 2)

    def test_non_modbus_protocol_id_is_rejected(self):
        def reply(head, body):
            txn, proto, length, unit = struct.unpack(">HHHB", head)
            pdu = bytes([3, 2, 0x12, 0x34])
            return struct.pack(">HHHB", txn, 99, len(pdu) + 1, unit) + pdu

        port = self._server(reply)
        with ModbusTCP("127.0.0.1", port, timeout=2.0) as c:
            with self.assertRaises(ModbusTransport) as ctx:
                c.read(0, 1)
        self.assertIn("not Modbus", str(ctx.exception))

    def test_silence_times_out_instead_of_hanging(self):
        port = self._server(lambda head, body: None)
        with ModbusTCP("127.0.0.1", port, timeout=0.6) as c:
            with self.assertRaises(ModbusTransport) as ctx:
                c.read(0, 1)
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_there_is_no_write_path(self):
        """Design constraint, checked against the parse tree rather than the
        text, so the comment explaining the constraint cannot trip it."""
        import ast
        with open("dcmon/modbus.py") as fh:
            tree = ast.parse(fh.read())
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertTrue(names)
        self.assertEqual([n for n in names if "write" in n.lower()], [])
        assigned = [t.id for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)]
        for forbidden in ("WRITE_SINGLE_REGISTER", "WRITE_MULTIPLE_REGISTERS"):
            self.assertNotIn(forbidden, assigned)
        self.assertFalse([a for a in dir(ModbusTCP) if "write" in a.lower()])


if __name__ == "__main__":
    unittest.main()
