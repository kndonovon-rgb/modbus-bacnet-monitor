#!/usr/bin/env python3
"""Start both simulators, poll them, and force each alarm condition.

    python3 demo.py

No hardware, no configuration, no dependencies. This is the whole system
exercised end to end in about fifteen seconds, including the three failures
the rules exist for: a threshold breach that needs confirming, a device that
stops answering, and a point the device itself disclaims.
"""
import os
import tempfile
import threading
import time

from dcmon import bacnet
from dcmon.poller import Poller, Point
from dcmon.rules import Rule, RuleSet, render
from dcmon.store import Store
from sim.bacnet_ahu import AHU, AHUServer
from sim.modbus_meter import Meter, MeterServer

BAR = "-" * 74


def banner(text):
    print("\n%s\n%s\n%s" % (BAR, text, BAR))


def main():
    meter = Meter()
    msrv = MeterServer(port=0, meter=meter)
    mport = msrv.server_address[1]
    threading.Thread(target=msrv.serve_forever, daemon=True).start()

    ahu = AHU()
    asrv = AHUServer(port=0, ahu=ahu)
    asrv.serve_in_thread()

    print("power meter  : 127.0.0.1:%d  (floats LOW WORD FIRST)" % mport)
    print("air handler  : 127.0.0.1:%d  (AI1 SA-TEMP, AI2 RA-TEMP)" % asrv.port)

    tmp = tempfile.mkdtemp(prefix="dcmon-demo-")
    store = Store(os.path.join(tmp, "demo.db"))

    points = [
        Point("meter.kw_total", "modbus", host="127.0.0.1", port=mport,
              documented=40005, type="f32", word_order="low_first", units="kW"),
        Point("meter.voltage_an", "modbus", host="127.0.0.1", port=mport,
              documented=40001, type="f32", word_order="low_first", units="V"),
        Point("ahu1.supply_air_temp", "bacnet", host="127.0.0.1",
              port=asrv.port, object_type="analog-input", instance=1,
              units="degF", timeout=1.0),
    ]
    rules = RuleSet([
        Rule("meter.kw_total", trip=180.0, clear=170.0, strikes=2,
             max_age=30, description="UPS-A above 80% of module rating"),
        Rule("meter.voltage_an", trip=456.0, clear=465.0, direction="below",
             strikes=2, max_age=30, description="phase A-N undervoltage"),
        Rule("ahu1.supply_air_temp", trip=60.0, clear=57.0, strikes=2,
             max_age=5, description="discharge air high"),
    ])
    poller = Poller(points, store, rules)

    def cycle(label):
        print("\n%s" % label)
        for r in poller.cycle():
            shown = "----" if r["value"] is None else (
                ("%.2f" % r["value"]).rstrip("0").rstrip("."))
            print("   %-22s %9s  %-13s %s" % (
                r["point"], shown, render(r["state"], r["ok"]),
                r["detail"] or ""))

    try:
        banner("1. Normal operation")
        cycle("first poll — rules start UNKNOWN and need confirmation")
        cycle("second poll — now confirmed healthy")

        banner("2. A threshold breach, and why one poll is not enough")
        meter.set("kw", 192.0)
        cycle("load jumps to 192 kW — one strike, not yet an alarm")
        cycle("still high — second strike, ALARM")

        banner("3. Hysteresis: back under the trip point is not 'clear'")
        meter.set("kw", 176.0)
        cycle("176 kW is below the 180 trip but inside the deadband — held")
        cycle("still inside the deadband — still ALARM, no flapping")
        meter.set("kw", 165.0)
        cycle("165 kW is past the 170 clear level — one")
        cycle("confirmed — OK")

        banner("4. The device stops answering")
        print("   (stopping the air handler; its last value stays plausible)")
        asrv.shutdown()
        time.sleep(0.2)
        cycle("poll fails — the stored value is still 55 degF and still wrong")
        print("   waiting out the 5s staleness horizon ...")
        time.sleep(5.5)
        cycle("STALE — the monitor's own blindness is now the alarm")

        banner("5. A value the device disclaims")
        ahu2 = AHU()
        ahu2.set((bacnet.ANALOG_INPUT, 1), "value", 55.0)
        ahu2.set((bacnet.ANALOG_INPUT, 1), "out_of_service", True)
        srv2 = AHUServer(port=asrv.port, ahu=ahu2)
        srv2.serve_in_thread()
        try:
            cycle("55 degF looks perfect; out-of-service means it is fiction")
        finally:
            srv2.shutdown()

        banner("6. What was stored")
        for name in store.points():
            s = store.summary(name, time.time() - 3600)
            print("   %-22s n=%-4d failed=%-3d min=%s max=%s" % (
                name, s["n"], s["failed"] or 0,
                "----" if s["lo"] is None else "%.2f" % s["lo"],
                "----" if s["hi"] is None else "%.2f" % s["hi"]))
        print("\n   transitions:")
        for e in reversed(store.recent_events(limit=20)):
            print("     %-7s %-22s %s" % (e["state"], e["point"], e["detail"]))
    finally:
        poller.close()
        store.close()
        msrv.shutdown()
        msrv.server_close()
        print("\n(demo database in %s)" % tmp)


if __name__ == "__main__":
    main()
