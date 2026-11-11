"""Deciding whether a number is a problem.

Three rules, each written for a specific way naive monitoring fails in a
critical facility.

**Hysteresis.** A single threshold flaps. Alarm above 80 and a value
oscillating 79.9 / 80.1 generates alarms until someone mutes the point, and a
muted point is an unmonitored point. So a rule has a trip level and a separate
clear level, and the gap between them is the deadband.

**Consecutive strikes.** One bad poll on an OT network is a dropped frame, a
busy gateway or a controller mid-reboot. Requiring N consecutive confirmations
turns noise into signal without slowing down a real excursion much.

**Staleness is not health — this is the one that matters.** When a device stops
answering, the last good value stays in the cache looking perfectly normal. A
dashboard that shows it without an age will report 68 degrees all night while
the sensor has been offline since dinner. Here a reading that is older than
`max_age` becomes STALE, which is its own alarm state and is never OK. The
monitor's own blindness has to be louder than the thing it was watching.
"""
import time

OK = "OK"
ALARM = "ALARM"
STALE = "STALE"
UNKNOWN = "UNKNOWN"


class Rule:
    """A threshold with hysteresis, confirmation and a staleness horizon.

    `direction` is "above" for high-limit points (temperature, load) or
    "below" for low-limit points (suction pressure, battery volts).
    """

    def __init__(self, point, trip, clear, direction="above", strikes=2,
                 max_age=None, description=""):
        if direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")
        if direction == "above" and clear > trip:
            raise ValueError(
                "for an 'above' rule the clear level must be <= the trip level;"
                " %r clears at %r but trips at %r" % (point, clear, trip))
        if direction == "below" and clear < trip:
            raise ValueError(
                "for a 'below' rule the clear level must be >= the trip level;"
                " %r clears at %r but trips at %r" % (point, clear, trip))
        self.point = point
        self.trip = trip
        self.clear = clear
        self.direction = direction
        self.strikes = max(1, int(strikes))
        self.max_age = max_age
        self.description = description

        self.state = UNKNOWN
        self._bad = 0
        self._good = 0

    @property
    def deadband(self):
        return abs(self.trip - self.clear)

    def _breached(self, value):
        return value > self.trip if self.direction == "above" else value < self.trip

    def _cleared(self, value):
        return value <= self.clear if self.direction == "above" else value >= self.clear

    def evaluate(self, value, ts=None, now=None, ok=True, detail=None):
        """Feed one reading. Returns (state, transitioned, detail).

        `ok=False` means the poll itself failed. A failed poll is not a value of
        zero — it is an absence, and it counts toward staleness rather than
        toward the threshold.
        """
        now = time.time() if now is None else now
        previous = self.state

        if self.max_age is not None and ts is not None:
            age = now - ts
            if age > self.max_age:
                self.state = STALE
                self._bad = self._good = 0
                return self.state, self.state != previous, (
                    "last reading is %.0fs old, limit %.0fs" % (age, self.max_age))

        if not ok:
            # No usable number. Hold the previous state unless we have no
            # staleness horizon at all, in which case we cannot claim health.
            if self.max_age is None:
                self.state = UNKNOWN
                return self.state, self.state != previous, detail or "poll failed"
            return self.state, False, detail or "poll failed"

        if value is None:
            self.state = UNKNOWN
            return self.state, self.state != previous, "no value"

        if self._breached(value):
            self._good = 0
            if self.state == ALARM:
                # Already alarming. Counting further breaches adds nothing and
                # produces messages like "7 of 2", which reads as a bug.
                return self.state, False, "%s still %s %s" % (
                    _fmt(value), self.direction, _fmt(self.trip))
            self._bad = min(self._bad + 1, self.strikes)
            if self._bad >= self.strikes:
                self.state = ALARM
                return self.state, True, "%s %s %s for %d consecutive polls" % (
                    _fmt(value), self.direction, _fmt(self.trip), self.strikes)
            return self.state, False, "%s breached, %d of %d" % (
                _fmt(value), self._bad, self.strikes)

        if self._cleared(value):
            self._bad = 0
            if self.state == OK:
                self._good = self.strikes
                return self.state, False, _fmt(value)
            self._good = min(self._good + 1, self.strikes)
            if self._good >= self.strikes:
                was = self.state
                self.state = OK
                if was == UNKNOWN:
                    return self.state, True, _fmt(value)
                return self.state, True, "%s back within %s" % (
                    _fmt(value), _fmt(self.clear))
            return self.state, False, "%s, %d of %d to clear" % (
                _fmt(value), self._good, self.strikes)

        # Inside the deadband: not breaching, not yet cleared. Hold.
        self._bad = 0
        return self.state, False, "%s inside deadband %s-%s" % (
            _fmt(value), _fmt(min(self.trip, self.clear)),
            _fmt(max(self.trip, self.clear)))


def render(state, ok=True):
    """How a state is shown to a human.

    A cycle whose read failed must never print as a bare OK. The state machine
    is right to hold its previous state — one dropped frame is not an event —
    but the display has to admit that this cycle produced no measurement.
    Printing "OK" next to "no answer" is the same lie as a dashboard showing
    last night's temperature as though it were current.
    """
    if state is None:
        return "-" if ok else "no-read"
    return state if ok else "%s/no-read" % state


def _fmt(v):
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


class RuleSet:
    def __init__(self, rules=()):
        self.rules = {r.point: r for r in rules}

    def add(self, rule):
        self.rules[rule.point] = rule
        return rule

    def evaluate(self, point, value, **kw):
        rule = self.rules.get(point)
        if rule is None:
            return None
        return rule.evaluate(value, **kw)

    def states(self):
        return {p: r.state for p, r in self.rules.items()}

    def in_alarm(self):
        return sorted(p for p, r in self.rules.items()
                      if r.state in (ALARM, STALE))
