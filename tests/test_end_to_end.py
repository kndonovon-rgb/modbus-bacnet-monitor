"""The whole stack against both simulators: poll, store, decide, recover."""
import os
import tempfile
import threading
import time
import unittest

from dcmon import bacnet
from dcmon.poller import Poller, Point, point_from_config, rule_from_config
from dcmon.rules import ALARM, OK, STALE, UNKNOWN, Rule, RuleSet
from dcmon.store import Store
from sim.bacnet_ahu import AHU, AHUServer
from sim.modbus_meter import Meter, MeterServer


class Stack(unittest.TestCase):
    def setUp(self):
        self.meter = Meter()
        self.msrv = MeterServer(port=0, meter=self.meter)
        self.mport = self.msrv.server_address[1]
        threading.Thread(target=self.msrv.serve_forever, daemon=True).start()

        self.ahu = AHU()
        self.asrv = AHUServer(port=0, ahu=self.ahu)
        self.asrv.serve_in_thread()

        self.dir = tempfile.mkdtemp(prefix="dcmon-e2e-")
        self.store = Store(os.path.join(self.dir, "e2e.db"))

        self.points = [
            Point("kw", "modbus", host="127.0.0.1", port=self.mport,
                  documented=40005, type="f32", word_order="low_first"),
            Point("sat", "bacnet", host="127.0.0.1", port=self.asrv.port,
                  object_type="analog-input", instance=1, timeout=1.0),
        ]
        self.rules = RuleSet([
            Rule("kw", trip=180.0, clear=170.0, strikes=2, max_age=30),
            Rule("sat", trip=60.0, clear=57.0, strikes=2, max_age=2),
        ])
        self.alarms = []
        self.poller = Poller(self.points, self.store, self.rules,
                             notify=lambda **kw: self.alarms.append(kw))

    def tearDown(self):
        self.poller.close()
        self.store.close()
        self.msrv.shutdown()
        self.msrv.server_close()
        try:
            self.asrv.shutdown()
        except OSError:
            pass

    def test_both_protocols_in_one_cycle(self):
        self.meter.set("kw", 158.0)
        self.ahu.set((bacnet.ANALOG_INPUT, 1), "value", 55.0)
        rows = {r["point"]: r for r in self.poller.cycle()}
        self.assertTrue(rows["kw"]["ok"])
        self.assertTrue(rows["sat"]["ok"])
        self.assertAlmostEqual(rows["kw"]["value"], 158.0, places=1)
        self.assertAlmostEqual(rows["sat"]["value"], 55.0, places=1)

    def test_readings_are_persisted(self):
        self.meter.set("kw", 160.0)
        self.poller.cycle()
        self.poller.cycle()
        self.assertEqual(len(self.store.series("kw")), 2)

    def test_an_alarm_fires_once_and_notifies_once(self):
        self.meter.set("kw", 195.0)
        for _ in range(5):
            self.poller.cycle()
        kw_alarms = [a for a in self.alarms
                     if a["point"] == "kw" and a["state"] == ALARM]
        self.assertEqual(len(kw_alarms), 1, "an alarm must not re-notify")
        self.assertEqual(self.rules.rules["kw"].state, ALARM)

    def test_recovery_notifies_once(self):
        self.meter.set("kw", 195.0)
        self.poller.cycle(); self.poller.cycle()
        self.meter.set("kw", 160.0)
        for _ in range(4):
            self.poller.cycle()
        oks = [a for a in self.alarms if a["point"] == "kw" and a["state"] == OK]
        self.assertEqual(len(oks), 1)

    def test_a_dead_device_becomes_stale_and_the_other_keeps_working(self):
        """One device failing must not take the cycle, or the other point, down."""
        self.meter.set("kw", 160.0)
        self.poller.cycle(); self.poller.cycle()
        self.assertEqual(self.rules.rules["sat"].state, OK)

        self.asrv.shutdown()
        time.sleep(0.1)
        self.poller.cycle()
        time.sleep(2.2)                       # past sat's 2s staleness horizon
        rows = {r["point"]: r for r in self.poller.cycle()}

        self.assertEqual(rows["sat"]["state"], STALE)
        self.assertFalse(rows["sat"]["ok"])
        self.assertEqual(rows["kw"]["state"], OK)
        self.assertTrue(rows["kw"]["ok"], "the healthy point must still read")

    def test_a_failed_poll_is_stored_as_a_failure_not_as_zero(self):
        self.asrv.shutdown()
        time.sleep(0.1)
        self.poller.cycle()
        row = self.store.latest("sat")
        self.assertEqual(row["ok"], 0)
        self.assertIsNone(row["value"])
        self.assertIn("no answer", row["detail"])

    def test_a_disclaimed_value_is_refused_even_though_it_looks_fine(self):
        key = (bacnet.ANALOG_INPUT, 1)
        self.ahu.set(key, "value", 55.0)
        self.ahu.set(key, "out_of_service", True)
        rows = {r["point"]: r for r in self.poller.cycle()}
        self.assertFalse(rows["sat"]["ok"])
        self.assertIn("disclaims", rows["sat"]["detail"])

    def test_a_modbus_exception_is_recorded_as_a_refusal(self):
        bad = Point("nope", "modbus", host="127.0.0.1", port=self.mport,
                    address=9000, type="f32")
        p = Poller([bad], self.store, RuleSet())
        try:
            rows = p.cycle()
        finally:
            p.close()
        self.assertFalse(rows[0]["ok"])
        self.assertIn("refused", rows[0]["detail"])

    def test_an_idle_socket_is_reconnected_rather_than_reported_dead(self):
        """Gateways drop idle TCP without a FIN we see. One retry, then report."""
        self.meter.set("kw", 160.0)
        self.poller.cycle()
        for client in self.poller._modbus.values():
            client._sock.close()              # simulate the silent drop
        rows = {r["point"]: r for r in self.poller.cycle()}
        self.assertTrue(rows["kw"]["ok"], "should have reconnected and retried")


class ConfigLoading(unittest.TestCase):
    def test_a_point_and_rule_are_built_from_config(self):
        entry = {
            "name": "meter.kw", "driver": "modbus", "documented": 40005,
            "type": "f32", "word_order": "low_first",
            "rule": {"trip": 180, "clear": 170, "strikes": 3, "max_age": 60},
        }
        p = point_from_config(entry)
        self.assertEqual(p.name, "meter.kw")
        self.assertEqual(p.driver, "modbus")
        r = rule_from_config(entry)
        self.assertEqual(r.strikes, 3)
        self.assertAlmostEqual(r.deadband, 10.0)

    def test_a_point_without_a_rule_is_allowed(self):
        self.assertIsNone(rule_from_config(
            {"name": "x", "driver": "modbus", "address": 0}))

    def test_an_unknown_driver_is_refused(self):
        with self.assertRaises(ValueError):
            point_from_config({"name": "x", "driver": "carrier-pigeon"})

    def test_the_shipped_example_config_parses(self):
        import json
        with open("config.example.json") as fh:
            cfg = json.load(fh)
        points = [point_from_config(e) for e in cfg["points"]]
        rules = [r for r in (rule_from_config(e) for e in cfg["points"]) if r]
        self.assertEqual(len(points), 5)
        self.assertEqual(len(rules), 4)


if __name__ == "__main__":
    unittest.main()
