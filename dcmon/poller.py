"""One poll cycle: read every point, store it, decide, report transitions.

The loop is deliberately boring. The interesting decisions are in rules.py
and the protocol modules; this file's only job is to make sure that a device
that fails does not take the cycle down with it, and that a failure is
recorded as a failure rather than dropped.
"""
import time

from . import decode
from .bacnet import BACnetError, BACnetIP, BACnetTransport
from .modbus import READ_HOLDING, READ_INPUT, ModbusError, ModbusTCP, ModbusTransport
from .rules import ALARM, OK, STALE, UNKNOWN, Rule, RuleSet, render

FUNCTIONS = {"holding": READ_HOLDING, "input": READ_INPUT}


class Point:
    """A named measurement and how to obtain it."""

    def __init__(self, name, driver, **spec):
        self.name = name
        self.driver = driver
        self.spec = spec
        self.units = spec.get("units", "")
        self.description = spec.get("description", "")


def point_from_config(entry):
    entry = dict(entry)
    name = entry.pop("name")
    driver = entry.pop("driver")
    if driver not in ("modbus", "bacnet"):
        raise ValueError("%s: unknown driver %r" % (name, driver))
    return Point(name, driver, **entry)


def rule_from_config(entry):
    r = entry.get("rule")
    if not r:
        return None
    return Rule(point=entry["name"],
                trip=r["trip"], clear=r["clear"],
                direction=r.get("direction", "above"),
                strikes=r.get("strikes", 2),
                max_age=r.get("max_age"),
                description=r.get("description", entry.get("description", "")))


class Poller:
    def __init__(self, points, store, ruleset=None, notify=None):
        self.points = list(points)
        self.store = store
        self.ruleset = ruleset or RuleSet()
        self.notify = notify or (lambda **kw: None)
        self._modbus = {}
        self._bacnet = {}

    # -- connections, reused across cycles --------------------------------
    def _modbus_client(self, host, port, unit, timeout):
        key = (host, port, unit)
        client = self._modbus.get(key)
        if client is None:
            client = ModbusTCP(host, port, unit=unit, timeout=timeout).connect()
            self._modbus[key] = client
        return client

    def _drop_modbus(self, host, port, unit):
        client = self._modbus.pop((host, port, unit), None)
        if client:
            client.close()

    def _bacnet_client(self, host, port, timeout):
        key = (host, port)
        client = self._bacnet.get(key)
        if client is None:
            client = BACnetIP(host, port, timeout=timeout).connect()
            self._bacnet[key] = client
        return client

    def close(self):
        for c in list(self._modbus.values()):
            c.close()
        for c in list(self._bacnet.values()):
            c.close()
        self._modbus.clear()
        self._bacnet.clear()

    # -- reading ----------------------------------------------------------
    def read_point(self, point):
        """Returns (value, ok, detail). Never raises for a device problem."""
        s = point.spec
        try:
            if point.driver == "modbus":
                host = s.get("host", "127.0.0.1")
                port = int(s.get("port", 502))
                unit = int(s.get("unit", 1))
                kind = s.get("type", "f32")
                table = s.get("table", "holding")
                base = 30001 if table == "input" else 40001
                address = (s["address"] if "address" in s
                           else decode.data_address(s["documented"], base))
                count = decode.WIDTH[kind]
                client = self._modbus_client(host, port, unit,
                                             float(s.get("timeout", 3.0)))
                try:
                    regs = client.read(address, count, FUNCTIONS[table])
                except ModbusTransport:
                    # Gateways and PLCs drop idle TCP sockets without a FIN we
                    # ever see, so the first read after a quiet period fails on
                    # a connection that looks open. Reconnect and try once
                    # more; a device that is genuinely gone fails twice and is
                    # reported. Retrying forever would hide a real outage.
                    self._drop_modbus(host, port, unit)
                    client = self._modbus_client(
                        host, port, unit, float(s.get("timeout", 3.0)))
                    regs = client.read(address, count, FUNCTIONS[table])
                value = decode.decode(kind, regs,
                                      word_order=s.get("word_order", "high_first"),
                                      scale=float(s.get("scale", 1.0)))
                return value, True, None

            host = s.get("host", "127.0.0.1")
            port = int(s.get("port", 47808))
            client = self._bacnet_client(host, port, float(s.get("timeout", 3.0)))
            if s.get("check_flags", True):
                r = client.trustworthy(s["object_type"], int(s["instance"]))
                if not r["trusted"]:
                    # A number the device itself disclaims is not a reading.
                    return None, False, (
                        "device disclaims the value (reliability=%s, "
                        "out_of_service=%s)" % (r["reliability"],
                                                r["out_of_service"]))
                value = r["value"]
            else:
                value = client.read_property(s["object_type"], int(s["instance"]))
            if isinstance(value, bool):
                value = 1.0 if value else 0.0
            return float(value), True, None

        except (ModbusError, BACnetError) as e:
            return None, False, "device refused: %s" % e
        except (ModbusTransport, BACnetTransport) as e:
            return None, False, "no answer: %s" % e
        except (KeyError, ValueError) as e:
            return None, False, "configuration: %s" % e
        except OSError as e:
            return None, False, "socket: %s" % e

    # -- one cycle --------------------------------------------------------
    def cycle(self, now=None):
        now = time.time() if now is None else now
        results = []
        for point in self.points:
            value, ok, detail = self.read_point(point)
            self.store.record(point.name, value, ts=now, ok=ok, detail=detail)

            rule = self.ruleset.rules.get(point.name)
            if rule is None:
                results.append({"point": point.name, "value": value, "ok": ok,
                                "state": None, "detail": detail})
                continue

            # Staleness is judged against the newest *successful* reading, not
            # against the clock of this cycle — otherwise a point that fails
            # every poll still looks fresh because we keep asking.
            last_good = self.store.latest_ok(point.name)
            ts = last_good["ts"] if last_good else None
            state, transitioned, rdetail = rule.evaluate(
                value if ok else None, ts=ts, now=now, ok=ok, detail=detail)

            if transitioned:
                self.store.record_event(point.name, state,
                                        previous=None, value=value, ts=now,
                                        detail=rdetail)
                self.notify(point=point.name, state=state, value=value,
                            detail=rdetail, description=rule.description)
            results.append({"point": point.name, "value": value, "ok": ok,
                            "state": state, "transitioned": transitioned,
                            "detail": rdetail or detail})
        return results
