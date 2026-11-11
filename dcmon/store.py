"""SQLite time series for readings and alarm transitions.

Two tables, deliberately separate. `readings` is what the plant did;
`events` is what the monitor concluded about it. Keeping the conclusion out of
the measurement table means a rule change never rewrites history, and a bad
threshold can be re-evaluated against the real data afterwards.
"""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id       INTEGER PRIMARY KEY,
    point    TEXT    NOT NULL,
    ts       REAL    NOT NULL,
    value    REAL,
    ok       INTEGER NOT NULL DEFAULT 1,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS readings_point_ts ON readings(point, ts);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    point    TEXT    NOT NULL,
    ts       REAL    NOT NULL,
    state    TEXT    NOT NULL,
    previous TEXT,
    value    REAL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS events_point_ts ON events(point, ts);
"""


class Store:
    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # A monitor that loses its last few readings to an unflushed page cache
        # on a power event is a monitor that cannot explain the power event.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # -- writes -----------------------------------------------------------
    def record(self, point, value, ts=None, ok=True, detail=None):
        ts = time.time() if ts is None else ts
        self.db.execute(
            "INSERT INTO readings(point, ts, value, ok, detail) VALUES (?,?,?,?,?)",
            (point, ts, value, 1 if ok else 0, detail))
        self.db.commit()

    def record_event(self, point, state, previous=None, value=None,
                     ts=None, detail=None):
        ts = time.time() if ts is None else ts
        self.db.execute(
            "INSERT INTO events(point, ts, state, previous, value, detail)"
            " VALUES (?,?,?,?,?,?)",
            (point, ts, state, previous, value, detail))
        self.db.commit()

    # -- reads ------------------------------------------------------------
    def latest(self, point):
        row = self.db.execute(
            "SELECT * FROM readings WHERE point=? ORDER BY ts DESC LIMIT 1",
            (point,)).fetchone()
        return dict(row) if row else None

    def latest_ok(self, point):
        """The newest reading that actually succeeded.

        Staleness has to be measured from this, not from `latest`. A point
        that fails every poll still gets a fresh row written each cycle with
        ok=0, so measuring age from the newest row of any kind would report a
        permanently dead device as up to date — which is precisely the
        blindness the staleness rule exists to catch.
        """
        row = self.db.execute(
            "SELECT * FROM readings WHERE point=? AND ok=1"
            " ORDER BY ts DESC LIMIT 1", (point,)).fetchone()
        return dict(row) if row else None

    def series(self, point, since=None, limit=1000):
        if since is None:
            rows = self.db.execute(
                "SELECT ts, value, ok FROM readings WHERE point=?"
                " ORDER BY ts DESC LIMIT ?", (point, limit)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT ts, value, ok FROM readings WHERE point=? AND ts>=?"
                " ORDER BY ts DESC LIMIT ?", (point, since, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def points(self):
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT point FROM readings ORDER BY point")]

    def recent_events(self, limit=50):
        rows = self.db.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def summary(self, point, since):
        """min / max / mean / count over a window, computed in SQL.

        Failed polls are excluded from the statistics but counted, because a
        mean that silently averages over the readings that did arrive hides
        exactly the interval worth looking at.
        """
        row = self.db.execute(
            "SELECT COUNT(*) AS n,"
            "       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed,"
            "       MIN(CASE WHEN ok=1 THEN value END) AS lo,"
            "       MAX(CASE WHEN ok=1 THEN value END) AS hi,"
            "       AVG(CASE WHEN ok=1 THEN value END) AS mean"
            " FROM readings WHERE point=? AND ts>=?", (point, since)).fetchone()
        return dict(row)

    def prune(self, before):
        cur = self.db.execute("DELETE FROM readings WHERE ts < ?", (before,))
        self.db.commit()
        return cur.rowcount
