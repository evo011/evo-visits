"""SQLite-Ablage fuer Messwerte und Abruf-Laeufe."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts        INTEGER NOT NULL,      -- Unix-Sekunden (UTC)
    gym       TEXT    NOT NULL,
    count     REAL,                  -- Personen im Club
    capacity  REAL,                  -- Maximalbelegung, falls bekannt
    pct       REAL,                  -- Auslastung in Prozent, falls nur die geliefert wird
    PRIMARY KEY (gym, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS runs (
    ts      INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    n       INTEGER NOT NULL,
    source  TEXT,
    error   TEXT
);
"""


@dataclass
class Reading:
    gym: str
    count: Optional[float] = None
    capacity: Optional[float] = None
    pct: Optional[float] = None

    @property
    def value(self) -> Optional[float]:
        return self.count if self.count is not None else self.pct


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                yield conn
                conn.commit()
            finally:
                conn.close()

    def conn(self):
        """Oeffentlicher Zugriff auf dieselbe Verbindung wie _conn() (Context-Manager)."""
        return self._conn()

    def add_readings(self, ts: int, readings: list[Reading]) -> int:
        return self.add_batch([(ts, r) for r in readings])

    def add_batch(self, items: list[tuple[int, Reading]]) -> int:
        rows = [(ts, r.gym, r.count, r.capacity, r.pct) for ts, r in items if r.value is not None]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO samples (ts, gym, count, capacity, pct) VALUES (?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def log_run(self, ok: bool, n: int, source: str = "", error: str = "", ts: Optional[int] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (ts, ok, n, source, error) VALUES (?,?,?,?,?)",
                (ts or int(time.time()), int(ok), n, source, error[:2000]),
            )

    def last_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, ok, n, source, error FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(zip(("ts", "ok", "n", "source", "error"), r)) for r in rows]

    def gyms(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT s.gym, s.ts, s.count, s.capacity, s.pct, agg.n, agg.first_ts
                FROM samples s
                JOIN (SELECT gym, MAX(ts) AS max_ts, COUNT(*) AS n, MIN(ts) AS first_ts
                      FROM samples GROUP BY gym) agg
                  ON agg.gym = s.gym AND agg.max_ts = s.ts
                ORDER BY s.gym
                """
            ).fetchall()
        keys = ("gym", "last_ts", "count", "capacity", "pct", "n_samples", "first_ts")
        return [dict(zip(keys, r)) for r in rows]

    def series(self, gym: str, since_ts: int, until_ts: Optional[int] = None) -> list[tuple[int, float]]:
        """(ts, wert) fuer einen Club; wert = Personen, sonst Prozent."""
        until_ts = until_ts or 2**62
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, COALESCE(count, pct) FROM samples WHERE gym=? AND ts>=? AND ts<=? ORDER BY ts",
                (gym, since_ts, until_ts),
            ).fetchall()
        return [(int(ts), float(v)) for ts, v in rows if v is not None]

    def unit(self, gym: str) -> str:
        """'people' wenn Personenzahlen vorliegen, sonst 'percent'."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(count), COUNT(pct) FROM samples WHERE gym=? AND ts > ?",
                (gym, int(time.time()) - 30 * 86400),
            ).fetchone()
        return "people" if (row and row[0] >= row[1]) else "percent"

    def export_rows(self) -> Iterator[tuple]:
        with self._conn() as c:
            yield from c.execute("SELECT ts, gym, count, capacity, pct FROM samples ORDER BY ts, gym").fetchall()
