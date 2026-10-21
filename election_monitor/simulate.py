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
