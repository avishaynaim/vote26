"""Replay a final per-ballot file (expb.csv) as if it were being counted live.

Writes growing ``expc.csv`` and ``expb.csv`` files at a fixed pace so the whole
pipeline (polling -> parsing -> analysis -> dashboard) can be rehearsed before
election night. Ballot boxes are counted whole, one at a time: small localities
tend to finish first, big cities report gradually, double envelopes come last.
"""

from __future__ import annotations

import csv
import io
import os
import random
import threading
import time

from .feed import ENVELOPES_CODE, load_file


class Replay:
    def __init__(self, ballots_path: str, out_dir: str, steps: int = 40, step_seconds: float = 5.0, seed: int = 7):
        self.final = load_file(ballots_path)
        if self.final.kind != "ballot":
            raise ValueError("replay needs a per-ballot file (expb.csv)")
        self.out_dir = out_dir
        self.steps = steps
        self.step_seconds = step_seconds
        rnd = random.Random(seed)
        size: dict[int, int] = {}
        for r in self.final.rows:
            size[r.code] = size.get(r.code, 0) + r.eligible
        ranked = sorted(size, key=size.get)
        rank = {code: i / max(1, len(ranked) - 1) for i, code in enumerate(ranked)}
        self.when = []  # time in [0, 1] at which each ballot box is reported
        for r in self.final.rows:
            if r.code == ENVELOPES_CODE:
                t = rnd.uniform(0.75, 1.0)
            else:
                start = 0.5 * rank[r.code] * rnd.uniform(0.3, 1.0)
                t = start + rnd.uniform(0, 0.25 + 0.45 * rank[r.code])
            self.when.append(min(1.0, t))
        self._stop = threading.Event()

    def write(self, t: float) -> int:
        counted = [r for r, w in zip(self.final.rows, self.when) if w <= t]
        parties = self.final.parties
        b = io.StringIO()
        wb = csv.writer(b)
        wb.writerow(["סמל ועדה", "שם ישוב", "סמל ישוב", "קלפי", "בזב", "מצביעים", "פסולים", "כשרים"] + parties)
        agg: dict[int, list] = {}
        for r in counted:
            wb.writerow([r.committee, r.name, r.code, r.ballot, r.eligible, r.voters, r.invalid, r.valid]
                        + [r.votes.get(p, 0) for p in parties])
            a = agg.setdefault(r.code, [r.committee, r.name, r.code, 0, 0, 0, 0] + [0] * len(parties))
            a[3] += r.eligible
            a[4] += r.voters
            a[5] += r.invalid
            a[6] += r.valid
            for i, p in enumerate(parties):
                a[7 + i] += r.votes.get(p, 0)
        c = io.StringIO()
        wc = csv.writer(c)
        wc.writerow(["סמל ועדה", "שם ישוב", "סמל ישוב", "בזב", "מצביעים", "פסולים", "כשרים"] + parties)
        for a in sorted(agg.values(), key=lambda a: a[1]):
            wc.writerow(a)
        os.makedirs(self.out_dir, exist_ok=True)
        for name, buf in (("expb.csv", b), ("expc.csv", c)):
            path = os.path.join(self.out_dir, name)
            with open(path + ".tmp", "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(buf.getvalue())
            os.replace(path + ".tmp", path)
        return len(counted)

    def run(self) -> None:
        step = 0
        while not self._stop.is_set() and step <= self.steps:
            self.write(step / self.steps)
            step += 1
            self._stop.wait(self.step_seconds)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="replay", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------------------------
# Mock live election: the 2022 ballots (from the results site) with random changes, reported
# ballot by ballot over several simulated days, at a speed that can be changed while running.
# ---------------------------------------------------------------------------------------------
SPEEDS = (1, 2, 5, 10, 30, 50, 100, 1000)

# Mock shifts per family (multiplier on votes) and turnout change per sector (relative). Random
# noise per locality and per ballot is added on top, so every run of the same seed is identical.
MOCK_FAMILY_FACTORS = {"likud": 0.88, "lapid": 1.04, "far_right": 1.12, "gantz": 1.18, "left": 1.25,
                       "shas": 0.97, "utj": 1.03, "yb": 1.22, "raam": 1.08, "joint": 0.95, "right_alt": 0.6}
MOCK_TURNOUT = {"arab": 1.07, "haredi": 1.02, "large": 0.98, "jewish": 1.0, "envelopes": 1.05}


class LiveSim:
    def __init__(self, ballots_path: str, out_dir: str, profile_family, sector_of, days: float = 5.0,
                 speed: int = 100, seed: int = 26):
        self.source = load_file(ballots_path)
        self.out_dir = out_dir
        self.duration = days * 86400.0
        self.speed = speed
        self.seed = seed
        self.family = profile_family
        self.sector_of = sector_of
        self.lock = threading.Lock()
        self._build()
        self.reset()
        self._stop = threading.Event()

    # mock final results + schedule
    def _build(self) -> None:
        rnd = random.Random(self.seed)
        parties = self.source.parties
        city_noise: dict[int, dict] = {}
        size: dict[int, int] = {}
        for r in self.source.rows:
            size[r.code] = size.get(r.code, 0) + r.eligible
        self.rows = []
        for r in self.source.rows:
            cn = city_noise.setdefault(r.code, {"t": rnd.gauss(0, 0.04),
                                                 "f": {p: rnd.lognormvariate(0, 0.12) for p in parties}})
            sector = self.sector_of(r.code)
            eligible = round(r.eligible * 1.06)
            turnout = (r.voters / r.eligible if r.eligible else 0) * MOCK_TURNOUT.get(sector, 1.0) * (1 + cn["t"])
            voters = r.voters if r.code == 9999 else min(eligible, round(eligible * turnout))
            if r.code == 9999:
                voters = round(r.voters * MOCK_TURNOUT["envelopes"])
            invalid = round(voters * (r.invalid / r.voters if r.voters else 0.006))
            valid = max(0, voters - invalid)
            w = {p: r.votes.get(p, 0) * MOCK_FAMILY_FACTORS.get(self.family(p), 1.0) * cn["f"][p]
                 * rnd.lognormvariate(0, 0.05) for p in parties}
            tw = sum(w.values())
            votes = {p: int(valid * w[p] / tw) for p in parties} if tw else {p: 0 for p in parties}
            if tw:  # largest remainders so the list votes add up to the valid votes
                rest = valid - sum(votes.values())
                for p in sorted(parties, key=lambda p: -(valid * w[p] / tw - votes[p]))[:rest]:
                    votes[p] += 1
            self.rows.append((r, eligible, voters, invalid, valid, votes))
        # schedule: each locality gets a start time and a duration; small ones finish early, big
        # cities report over a longer period, double envelopes arrive last (days 3-5)
        ranked = sorted(size, key=size.get)
        rank = {c: i / max(1, len(ranked) - 1) for i, c in enumerate(ranked)}
        start = {c: rnd.uniform(0.0, 0.35) * (0.3 + rank[c]) for c in size}
        span = {c: rnd.uniform(0.02, 0.15) + 0.35 * rank[c] for c in size}
        self.when = []
        for r, *_ in self.rows:
            if r.code == 9999:
                t = rnd.uniform(0.55, 1.0)
            else:
                # squared: most ballot boxes arrive in the first night, the rest trickle in over the days
                t = min(0.98, start[r.code] + rnd.random() * span[r.code]) ** 2
            self.when.append(t * self.duration)
        self.order = sorted(range(len(self.rows)), key=lambda i: self.when[i])

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.sim_seconds = 0.0
            self.reported = 0
            self.running = True
            self._last_real = time.time()
            self._written = -1
            for fn in ("expc.csv", "expb.csv"):
                p = os.path.join(self.out_dir, fn)
                if os.path.exists(p):
                    os.remove(p)

    def control(self, speed=None, action=None) -> dict:
        with self.lock:
            self._advance()
            if speed is not None and int(speed) in SPEEDS:
                self.speed = int(speed)
            if action == "pause":
                self.running = False
            elif action == "resume":
                self.running = True
        if action == "reset":
            self.reset()
        return self.status()

    def status(self) -> dict:
        return {"sim_seconds": int(self.sim_seconds), "duration": int(self.duration), "speed": self.speed,
                "running": self.running, "finished": self.reported >= len(self.rows),
                "ballots_reported": self.reported, "ballots_total": len(self.rows), "speeds": list(SPEEDS)}

    def clock_label(self) -> str:
        s = int(self.sim_seconds) + 22 * 3600  # polls close at 22:00 on day 1
        return f"יום {s // 86400 + 1} · {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"

    def _advance(self) -> None:
        now = time.time()
        if self.running:
            self.sim_seconds = min(self.duration, self.sim_seconds + (now - self._last_real) * self.speed)
        self._last_real = now
        n = self.reported
        while n < len(self.order) and self.when[self.order[n]] <= self.sim_seconds:
            n += 1
        self.reported = n

    def _write(self) -> None:
        counted = [self.rows[i] for i in self.order[:self.reported]]
        parties = self.source.parties
        b, c = io.StringIO(), io.StringIO()
        wb, wc = csv.writer(b), csv.writer(c)
        wb.writerow(["סמל ועדה", "שם ישוב", "סמל ישוב", "קלפי", "בזב", "מצביעים", "פסולים", "כשרים"] + parties)
        agg: dict[int, list] = {}
        for r, el, vo, inv, val, votes in counted:
            wb.writerow([r.committee, r.name, r.code, r.ballot, el, vo, inv, val] + [votes[p] for p in parties])
            a = agg.setdefault(r.code, [r.committee, r.name, r.code, 0, 0, 0, 0] + [0] * len(parties))
            a[3] += el
            a[4] += vo
            a[5] += inv
            a[6] += val
            for i, p in enumerate(parties):
                a[7 + i] += votes[p]
        wc.writerow(["סמל ועדה", "שם ישוב", "סמל ישוב", "בזב", "מצביעים", "פסולים", "כשרים"] + parties)
        for a in sorted(agg.values(), key=lambda a: a[1]):
            wc.writerow(a)
        os.makedirs(self.out_dir, exist_ok=True)
        for name, buf in (("expb.csv", b), ("expc.csv", c)):
            path = os.path.join(self.out_dir, name)
            with open(path + ".tmp", "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(buf.getvalue())
            os.replace(path + ".tmp", path)
        self._written = self.reported

    def run(self, min_write_interval: float = 1.0) -> None:
        last = 0.0
        while not self._stop.is_set():
            with self.lock:
                self._advance()
                due = self.reported != self._written and time.time() - last >= min_write_interval
                if due:
                    self._write()
                    last = time.time()
            self._stop.wait(0.25)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="livesim", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
