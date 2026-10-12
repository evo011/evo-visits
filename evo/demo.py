"""Synthetische Demo-Daten, nur damit man das Dashboard vor der ersten echten Woche sehen kann.

Landen in einer eigenen Datenbank (data/demo.db) und tragen erkennbar falsche Club-Namen.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime

from .forecast import get_tz
from .store import Reading, Store

DEMO_GYMS = {"Demo Nord": 1.0, "Demo Mitte": 1.35, "Demo Süd": 0.8, "Demo West": 1.1}


def typical(dt: datetime, scale: float) -> float:
    h = dt.hour + dt.minute / 60
    if dt.weekday() < 5:
        base = 8 + 18 * math.exp(-((h - 7.5) / 1.2) ** 2) + 14 * math.exp(-((h - 12.5) / 1.3) ** 2) \
            + 55 * math.exp(-((h - 18.5) / 1.6) ** 2)
        if dt.weekday() == 4:
            base *= 0.8
    else:
        base = 6 + 40 * math.exp(-((h - 12.0) / 2.6) ** 2)
    if h < 6 or h > 23:
        base = 0
    return base * scale


def generate(store: Store, tz_name: str, weeks: int = 6, interval_min: int = 10, seed: int = 7) -> int:
    rnd = random.Random(seed)
    tz = get_tz(tz_name)
    now = int(time.time())
    start = now - weeks * 7 * 86400
    start -= start % (interval_min * 60)
    batch: list[tuple[int, Reading]] = []
    level = {g: 1.0 for g in DEMO_GYMS}
    for ts in range(start, now, interval_min * 60):
        dt = datetime.fromtimestamp(ts, tz)
        if not (6 <= dt.hour < 23):
            continue
        for gym, scale in DEMO_GYMS.items():
            # langsam wandernder Tageslevel, damit die Prognose etwas zu tun hat
            level[gym] = 0.98 * level[gym] + 0.02 * (1 + rnd.gauss(0, 0.25))
            v = typical(dt, scale) * level[gym] + rnd.gauss(0, 3)
            batch.append((ts, Reading(gym=gym, count=float(max(0, round(v))), capacity=round(110 * scale))))
    return store.add_batch(batch)
