# modbus-bacnet-monitor

Polls power meters over **Modbus TCP** and air handlers over **BACnet/IP**,
stores the readings, and alarms on them without crying wolf.

Both protocols are encoded and parsed in this repository on stdlib sockets.
There is no `pymodbus`, no `bacpypes`, and nothing to `pip install` — because
an integrator who cannot read an APDU in a packet capture is holding a vendor
tool rather than a skill. ~1,550 lines of Python, 101 tests, no dependencies.

```bash
python3 demo.py                              # starts both simulated devices
python3 -m unittest discover -s tests -t .   # 101 tests, no hardware, no network
```

`demo.py` stands up a Modbus power meter and a BACnet air handler on loopback
and then walks every failure the rules exist for: a threshold breach that needs
confirming, hysteresis holding an alarm inside the deadband, a device that stops
answering, and a point the device itself disclaims.

```
dcmon/modbus.py    Modbus TCP read client                 (130 lines)
dcmon/bacnet.py    BACnet/IP ReadProperty, hand-encoded    (330 lines)
dcmon/decode.py    registers -> engineering units           (95 lines)
dcmon/rules.py     hysteresis, confirmation, staleness     (170 lines)
dcmon/store.py     SQLite readings + events                (130 lines)
dcmon/poller.py    one cycle, fault-isolated               (180 lines)
sim/               a meter and an AHU that speak real bytes
```

---

## The idea the whole thing is built around

**A monitor's own blindness has to be louder than the thing it was watching.**

When a device stops answering, its last good value does not disappear. It sits
in the cache at 55.0 °F, inside every limit, looking exactly like a
measurement. A dashboard that shows it without an age will report that the
supply air is fine all night while the sensor has been offline since dinner.

So in this tool a reading carries a timestamp, and a reading older than its
point's `max_age` becomes **STALE** — which is its own alarm state and is never
OK. Not seeing a problem and there being no problem are opposite facts.

### The subtle half of that

Staleness has to be measured from the last **successful** reading, not the last
row written. A dead device still produces a row every cycle, because the
monitor keeps asking and keeps recording the failure. Age measured from the
newest row of any kind stays near zero forever:

```
naive  (newest row of any kind):   0 seconds — "current"
honest (newest successful read):  59 seconds — stale
```

That is `Store.latest_ok()`, and `test_the_difference_is_the_whole_point`
asserts the gap.

---

## Four bugs the protocols hand you

### 1. Word order turns 480 V into a number that passes a high limit

A 32-bit float spans two Modbus registers. The IEEE bytes inside each register
are big-endian, but **which register holds the high word is a vendor decision**,
and plenty of meters put it second.

Get it wrong and you do not get garbage. You get `2.3e-41` — which is below
every high limit anyone would configure, so the alarm never fires and the point
reads healthy. A wrong answer that looks plausible is worse than an error, and
this is why `word_order` is a required thought rather than a default.

### 2. Register 40001 is address 0

The `4xxxx` and `3xxxx` numbers in vendor manuals are a 1-based Modicon
*reference* convention, not the address that goes in the PDU. Holding register
40001 is address **0** with function code 3.

Using the documented number directly does not error. It silently returns a
different register — which is how voltage ends up reported as current, and
every point is off by one in a way that looks like a wiring problem.

### 3. A BACnet length field of 5 does not mean five bytes

This one was a real bug in this decoder, found by a test. BACnet's
length/value/type field is overloaded: 0–4 is a literal byte count, but **5
means the real length follows** in the next byte (or two, or four).

Every string longer than four characters takes that path. Reading 5 as a
literal length consumes the wrong number of bytes and misaligns every tag after
it, so the failure surfaced as `missing closing tag 3` — pointing at the end of
the buffer rather than at the string that caused it. `_tag_length()` now
handles all three forms and six tests cover them.

### 4. UDP has no ordering, so a late reply looks like a fresh one

BACnet/IP is UDP. A reply to the *previous* poll arriving slightly late is
byte-for-byte indistinguishable from a reply to this one unless you check the
invoke ID — and believing it reports a stale value as current. The client
matches the invoke ID and skips datagrams that do not, which
`test_client_skips_a_late_datagram_and_finds_the_right_one` proves by injecting
one.

Modbus has the same trap with its transaction ID. On a shared serial gateway a
mismatch is *another point's data*, not an error.

---

## Alarming without getting muted

**Hysteresis.** One threshold flaps. Alarm above 80 with a value oscillating
79.9 / 80.1 and you generate alarms until somebody mutes the point — and a
muted point is an unmonitored point with paperwork. So a rule has a trip level
and a separate clear level. Inside the deadband the alarm *holds*. Six
oscillations across the trip point produce zero transitions.

**Confirmation.** One bad poll on an OT network is a dropped frame, a busy
gateway or a controller mid-reboot. N consecutive readings turn noise into
signal. An interrupted run resets, because two breaches either side of a good
reading are not consecutive.

**A failed poll is not a value of zero.** Treating it as 0 would clear a
high-limit alarm, which is the most dangerous possible interpretation of
silence. A failed read contributes to staleness and nothing else.

**A failed poll never prints as a bare `OK`.** The state machine is right to
hold its previous state through one dropped frame, but the display has to admit
the cycle produced no measurement, so it renders `OK/no-read`. Printing `OK`
next to `no answer` is the same lie as showing last night's temperature as
current.

**An inverted deadband is refused at construction.** A rule that clears above
its trip point can never clear, so it fails loudly at startup rather than
quietly never alarming.

---

## Read-only, structurally

There is no write path in either protocol module, and the tests assert it
against the parse tree.

For **Modbus** there is no function code 6 or 16. A monitoring tool that can
write to a power meter or a chiller controller is a tool that can take a room
down, and "it only writes when you tell it to" is not a safety argument anyone
should accept at 3am.

For **BACnet** the reason is sharper: writes go through the **priority array**.
Writing `present-value` at a priority and never releasing it leaves the point
commanded, the BMS can no longer control it, and the override survives the
technician going home. People have frozen coils that way.

The cross-system database is opened read-only for the same reason. Absence of
capability is the only guarantee that survives a tired operator.

---

## A number the device disclaims is not a reading

A BACnet point can report a perfectly plausible value while the device has
already flagged it. `out-of-service` means present-value is whatever a
technician last typed in during commissioning; a non-zero `reliability` means
the device is telling you not to trust its own sensor.

Reading `present-value` alone cannot distinguish either from a live
measurement. `trustworthy()` reads all three and the poller refuses the value
when the device disclaims it — recorded as a failed read, with the reason, not
as a healthy 55 °F.

---

## On the tests

101 tests, no network, no hardware, no mocks of the protocols. The simulators
encode genuine BVLL/NPDU/APDU and MBAP frames, so a mistake in either side's
tag handling fails here instead of on site. Deliberately bad replies —
mismatched transaction IDs, lying byte counts, truncated frames, silence on an
open socket — are hand-built from bytes.

Two of these tests were themselves wrong, which is worth recording:

- A "silence" fixture **closed the socket** instead of staying quiet, so it
  tested a clean EOF rather than a hung controller. Different failure, and the
  one it claimed to cover was the one it did not.
- A guard asserting there is no write path **grepped the source for
  `WriteProperty`** and failed on the docstring explaining why there is no
  `WriteProperty`. It now walks the AST. A guard that cannot tell code from
  prose is a guard that gets deleted the first time it cries wolf.

---

## Configuration

Points are declared in JSON — see `config.example.json`, which is parsed by a
test so it cannot drift:

```json
{
  "name": "meter.kw_total",
  "driver": "modbus",
  "host": "10.20.30.40", "port": 502, "unit": 1,
  "documented": 40005, "type": "f32", "word_order": "low_first",
  "rule": { "direction": "above", "trip": 180.0, "clear": 170.0,
            "strikes": 2, "max_age": 120,
            "description": "UPS-A above 80% of 225 kW module rating" }
}
```

```bash
python3 -m dcmon -c config.json poll            # one cycle
python3 -m dcmon -c config.json watch -i 10     # until stopped
python3 -m dcmon -c config.json trend -m 60 --spark
```

Gateways and PLCs drop idle TCP sockets without a FIN anyone sees, so the first
read after a quiet period fails on a connection that still looks open. The
poller reconnects and retries **once**; a device that is genuinely gone fails
twice and gets reported. Retrying forever would hide a real outage.

MIT licensed. The reasoning is the useful part.
