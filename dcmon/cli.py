"""Command line: poll once, watch continuously, or print the trend."""
import argparse
import json
import os
import sys
import time

from .poller import Poller, point_from_config, rule_from_config
from .rules import ALARM, OK, STALE, UNKNOWN, RuleSet, render
from .store import Store

STATE_MARK = {OK: "  ok  ", ALARM: " ALARM", STALE: " STALE", UNKNOWN: "  ??  "}


def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    if "points" not in cfg:
        raise ValueError("config has no 'points' list")
    return cfg


def build(cfg, db_path=None):
    store = Store(db_path or cfg.get("database", "dcmon.db"))
    points = [point_from_config(e) for e in cfg["points"]]
    rules = [r for r in (rule_from_config(e) for e in cfg["points"]) if r]
    return store, Poller(points, store, RuleSet(rules), notify=_print_alarm)


def _print_alarm(point, state, value, detail, description=""):
    where = " — %s" % description if description else ""
    shown = "n/a" if value is None else ("%.3f" % value).rstrip("0").rstrip(".")
    print("%s  %-7s %-22s %s%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), state, point, detail or shown, where),
        flush=True)


def cmd_poll(args):
    cfg = load_config(args.config)
    store, poller = build(cfg, args.database)
    try:
        for row in poller.cycle():
            shown = "----" if row["value"] is None else (
                ("%.3f" % row["value"]).rstrip("0").rstrip("."))
            print("%-14s %-22s %10s   %s" % (
                render(row["state"], row["ok"]), row["point"], shown,
                row["detail"] or ""))
    finally:
        poller.close()
        store.close()
    return 0


def cmd_watch(args):
    cfg = load_config(args.config)
    interval = args.interval or cfg.get("interval", 10)
    store, poller = build(cfg, args.database)
    print("polling %d point(s) every %ds — ctrl-c to stop"
          % (len(poller.points), interval), flush=True)
    try:
        while True:
            started = time.time()
            rows = poller.cycle()
            bad = [r for r in rows if r["state"] in (ALARM, STALE)]
            if args.verbose or bad:
                print("%s  %d point(s), %d in alarm" % (
                    time.strftime("%H:%M:%S"), len(rows), len(bad)), flush=True)
            time.sleep(max(0.0, interval - (time.time() - started)))
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        poller.close()
        store.close()
    return 0


def cmd_trend(args):
    store = Store(args.database or load_config(args.config).get(
        "database", "dcmon.db"))
    try:
        since = time.time() - args.minutes * 60
        names = [args.point] if args.point else store.points()
        if not names:
            print("no readings stored yet")
            return 1
        for name in names:
            s = store.summary(name, since)
            if not s["n"]:
                print("%-22s no readings in the last %d min" % (name, args.minutes))
                continue
            failed = s["failed"] or 0
            print("%-22s n=%-5d min=%-9s max=%-9s mean=%-9s failed=%d" % (
                name, s["n"],
                _num(s["lo"]), _num(s["hi"]), _num(s["mean"]), failed))
            if args.spark:
                rows = store.series(name, since=since, limit=200)
                print(" " * 22 + " " + _spark([r["value"] for r in rows
                                               if r["ok"] and r["value"] is not None]))
        events = store.recent_events(limit=args.events)
        if events:
            print("\nrecent transitions")
            for e in events:
                print("  %s  %-7s %-22s %s" % (
                    time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"])),
                    e["state"], e["point"], e["detail"] or ""))
    finally:
        store.close()
    return 0


def _num(v):
    return "----" if v is None else ("%.2f" % v).rstrip("0").rstrip(".")


def _spark(values):
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return blocks[0] * len(values)
    return "".join(
        blocks[min(len(blocks) - 1, int((v - lo) / (hi - lo) * len(blocks)))]
        for v in values)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dcmon",
                                 description="Poll Modbus and BACnet points,"
                                             " store them, alarm on them.")
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("-d", "--database")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("poll", help="one cycle, printed")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("watch", help="poll on an interval until stopped")
    p.add_argument("-i", "--interval", type=int)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("trend", help="summarise what is stored")
    p.add_argument("-p", "--point")
    p.add_argument("-m", "--minutes", type=int, default=60)
    p.add_argument("-e", "--events", type=int, default=10)
    p.add_argument("--spark", action="store_true")
    p.set_defaults(fn=cmd_trend)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
