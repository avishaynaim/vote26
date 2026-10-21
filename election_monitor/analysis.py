"""Analysis of a (possibly partial) result file against a baseline election.

Key ideas
---------
* **Locality matching** is done on the locality code (סמל ישוב), which is stable
  between elections. The double-envelopes row (code 9999) is matched too.
* **Families** make lists comparable across elections (e.g. 2022 Labor + Meretz
  vs. a merged list in a later election). **Blocs** are the coarse political camps.
* **Like-for-like (LFL)**: while counting is partial, comparing today's national
  share with the full baseline is misleading (early localities are not
  representative). Instead each reporting locality's *baseline* shares are
  weighted by today's counted votes in that locality. ``share_now - share_lfl``
  is the real swing among the votes counted so far.
* **Projection**: the votes still expected in each locality (baseline volume x
  register growth x sector turnout change, minus what is already counted) are
  distributed with the locality's baseline shares plus its sector's swing.
* **Sectors** (Arab / Haredi / large Jewish cities / other Jewish localities /
  double envelopes) are derived from the baseline result.
"""

from __future__ import annotations

import itertools
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .feed import ENVELOPES_CODE, ResultFile, Row
from .seats import bader_ofer

SECTOR_NAMES = {
    "arab": "Arab & Bedouin localities",
    "haredi": "Haredi localities",
    "large": "Large Jewish / mixed cities",
    "jewish": "Other Jewish localities",
    "envelopes": "Double envelopes (soldiers, diplomats...)",
    "new": "New localities (no baseline)",
}
SHRINK_VOTES = 5000.0  # sector swing is blended with national swing until a sector has this many votes


SECTOR_HE = {
    "arab": "יישובים ערביים ובדואיים", "haredi": "יישובים חרדיים", "large": "ערים גדולות",
    "jewish": "יישובים יהודיים אחרים", "envelopes": "מעטפות חיצוניות", "new": "יישובים חדשים",
}


def short_he(name: str) -> str:
    """Short Hebrew label from the official list name (text before 'בראשות' / 'בהנהגת' ...)."""
    s = re.split(r"\s+(?:בראשות|בהנהגת|בהובלת|תנועתו של|-|–)\s+", name, maxsplit=1)[0].strip()
    return s if len(s) <= 26 else s[:24].rstrip() + "…"


def _pct(a: float, b: float) -> Optional[float]:
    return (a / b) if b else None


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(x, n)


@dataclass
class Profile:
    """Party metadata of one election: official names from the results site,
    bloc/family classification from config (or inherited from the same letter
    in the baseline election)."""

    key: str
    cfg: dict
    names: dict = field(default_factory=dict)  # letter -> official list name (site national page)
    inherit: Optional["Profile"] = None
    _cache: dict = field(default_factory=dict)

    def set_names(self, names: dict) -> None:
        self.names = dict(names)
        self._cache.clear()

    def party(self, letter: str) -> dict:
        if letter in self._cache:
            return self._cache[letter]
        own = self.names.get(letter)
        base_he = self.inherit.names.get(letter) if self.inherit is not None else None
        he = own or base_he or letter
        p = self.cfg.get("parties", {}).get(letter)
        if p:
            out = {"letter": letter, "he": he, "name": p.get("short") or he, "short_he": p.get("short_he") or short_he(he),
                   "bloc": p["bloc"], "family": p["family"]}
        elif (self.inherit is not None and self.cfg.get("inherit_by_letter")
              and letter in self.inherit.cfg.get("parties", {})):
            q = self.inherit.cfg["parties"][letter]
            same_list = not own or own == base_he
            out = {"letter": letter, "he": he,
                   "name": (q.get("short") if same_list else None) or (he if he != letter else f"List {letter}"),
                   "short_he": (q.get("short_he") if same_list else None) or (short_he(he) if he != letter else f"רשימה {letter}"),
                   "bloc": q["bloc"], "family": q["family"], "assumed": True}
        else:
            out = {"letter": letter, "he": he, "name": he if he != letter else f"List {letter}",
                   "short_he": short_he(he) if he != letter else f"רשימה {letter}",
                   "bloc": "X", "family": "other", "unclassified": True}
        self._cache[letter] = out
        return out

    def family(self, letter: str) -> str:
        return self.party(letter)["family"]

    def bloc(self, letter: str) -> str:
        return self.party(letter)["bloc"]


class CityAgg:
    __slots__ = ("code", "name", "committee", "eligible", "voters", "invalid", "valid", "letters", "fam", "bloc")

    def __init__(self, row: Row, prof: Profile):
        self.code, self.name, self.committee = row.code, row.name, row.committee
        self.eligible, self.voters, self.invalid, self.valid = row.eligible, row.voters, row.invalid, row.valid
        self.letters = {k: v for k, v in row.votes.items() if v}
        self.fam: dict[str, int] = {}
        self.bloc: dict[str, int] = {}
        for l, v in self.letters.items():
            f, b = prof.family(l), prof.bloc(l)
            self.fam[f] = self.fam.get(f, 0) + v
            self.bloc[b] = self.bloc.get(b, 0) + v
        if not self.valid:
            self.valid = sum(self.letters.values())

    def share(self, d: dict, k: str) -> float:
        return d.get(k, 0) / self.valid if self.valid else 0.0


def classify_sector(c: CityAgg, cfg: dict) -> str:
    sc = cfg.get("sectors", {})
    ov = sc.get("overrides", {})
    if str(c.code) in ov:
        return ov[str(c.code)]
    if c.code == ENVELOPES_CODE:
        return "envelopes"
    if c.share(c.bloc, "A") >= sc.get("arab_min_share", 0.5):
        return "arab"
    if (c.share(c.fam, "utj") + c.share(c.fam, "shas") >= sc.get("haredi_min_share", 0.5)
            and c.share(c.fam, "utj") >= sc.get("haredi_min_utj", 0.2)):
        return "haredi"
    if c.eligible >= sc.get("large_city_min_eligible", 100000):
        return "large"
    return "jewish"


class Analyzer:
    def __init__(self, cfg: dict, baseline: ResultFile, current_key: str, baseline_key: str,
                 baseline_national: Optional[dict] = None, baseline_ballots: Optional[ResultFile] = None):
        self.cfg = cfg
        self.base = Profile(baseline_key, cfg["elections"][baseline_key])
        self.cur = Profile(current_key, cfg["elections"][current_key], inherit=self.base)
        if baseline_national:
            self.base.set_names({l["letter"]: l["name"] for l in baseline_national.get("lists", [])})
            if current_key == baseline_key:
                self.cur.set_names(self.base.names)
        # ballot boxes per locality in the baseline (from expb.csv) -> per-locality counting progress
        self.base_ballots: dict[int, int] = {}
        self.base_ballot_rows: dict[tuple, Row] = {}
        if baseline_ballots is not None:
            for r in baseline_ballots.rows:
                self.base_ballots[r.code] = self.base_ballots.get(r.code, 0) + 1
                self.base_ballot_rows[(r.code, r.ballot)] = r
        self.growth = float(cfg.get("eligible_growth", 1.0))
        self.threshold = float(cfg.get("threshold", 0.0325))
        self.seats = int(cfg.get("seats", 120))
        self.majority = int(cfg.get("majority", 61))
        self.base_cities = {c.code: c for c in (CityAgg(r, self.base) for r in baseline.by_city().values())}
        self.sector = {code: classify_sector(c, cfg) for code, c in self.base_cities.items()}
        self.base_tot = self._totals(self.base_cities.values())
        self.base_letters = self._sum_dicts(c.letters for c in self.base_cities.values())
        self.base_fam = self._sum_dicts(c.fam for c in self.base_cities.values())
        self.base_bloc = self._sum_dicts(c.bloc for c in self.base_cities.values())
        # Use official seats from the results site when they sum to exactly `self.seats`
        # (so we match the Knesset's own numbers, incl. surplus agreements we don't know).
        # Any missing/non-int seat count disqualifies the official set.
        official = [l for l in (baseline_national or {}).get("lists", []) if isinstance(l.get("seats"), int)]
        official_sum = sum(l["seats"] for l in official) if official else 0
        if official and official_sum == self.seats:
            self.base_seats = {l["letter"]: l["seats"] for l in official}
            self.base_seats_source = "official (results site)"
        else:
            self.base_seats = bader_ofer(self.base_letters, self.base.cfg.get("agreements"), self.seats, self.threshold)["seats"]
            self.base_seats_source = "computed"
        self.base_bloc_seats: dict[str, int] = {}
        self.base_fam_seats: dict[str, int] = {}
        for l, s in self.base_seats.items():
            self.base_bloc_seats[self.base.bloc(l)] = self.base_bloc_seats.get(self.base.bloc(l), 0) + s
            self.base_fam_seats[self.base.family(l)] = self.base_fam_seats.get(self.base.family(l), 0) + s

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _sum_dicts(ds) -> dict:
        out: dict = {}
        for d in ds:
            for k, v in d.items():
                out[k] = out.get(k, 0) + v
        return out

    @staticmethod
    def _totals(cities) -> dict:
        t = {"eligible": 0, "voters": 0, "invalid": 0, "valid": 0, "env_voters": 0, "env_valid": 0}
        for c in cities:
            t["eligible"] += c.eligible
            t["voters"] += c.voters
            t["invalid"] += c.invalid
            t["valid"] += c.valid
            if c.code == ENVELOPES_CODE:
                t["env_voters"] += c.voters
                t["env_valid"] += c.valid
        return t

    # ------------------------------------------------------------------ main
    def analyze(self, current: ResultFile, ballots: Optional[ResultFile] = None,
                meta: Optional[dict] = None, previous: Optional[dict] = None) -> dict:
        cur_cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                      if c.voters or c.valid}
        reporting_mode = self._reporting_mode(cur_cities)
        tot = self._totals(cur_cities.values())
        letters = self._sum_dicts(c.letters for c in cur_cities.values())
        for p in self.cur.cfg.get("parties", {}):
            letters.setdefault(p, 0)
        fam_now = self._sum_dicts(c.fam for c in cur_cities.values())
        bloc_now = self._sum_dicts(c.bloc for c in cur_cities.values())
        valid = tot["valid"]

        # ---- per-locality reporting fraction
        counted_ballots: dict[int, int] = {}
        if ballots is not None:
            for r in ballots.rows:
                if r.voters or r.valid:
                    counted_ballots[r.code] = counted_ballots.get(r.code, 0) + 1
        rep: dict[int, float] = {}
        for code, c in cur_cities.items():
            b = self.base_cities.get(code)
            if not b:
                rep[code] = 1.0
            elif counted_ballots and self.base_ballots.get(code):
                rep[code] = min(1.0, counted_ballots.get(code, 0) / self.base_ballots[code])
            elif code == ENVELOPES_CODE or reporting_mode == "volume" or not c.eligible:
                rep[code] = min(1.0, c.voters / (b.voters * self.growth)) if b.voters else 1.0
            else:
                rep[code] = min(1.0, c.eligible / (b.eligible * self.growth)) if b.eligible else 1.0

        # ---- like-for-like baselines (weights = votes counted today in each locality)
        matched = [c for c in cur_cities.values() if c.code in self.base_cities and c.valid]
        w_tot = sum(c.valid for c in matched)
        lfl_fam = {f: 0.0 for f in self.cfg["families"]}
        lfl_bloc = {b: 0.0 for b in self.cfg["blocs"]}
        for c in matched:
            b = self.base_cities[c.code]
            for f, v in b.fam.items():
                lfl_fam[f] = lfl_fam.get(f, 0) + c.valid * v / b.valid
            for k, v in b.bloc.items():
                lfl_bloc[k] = lfl_bloc.get(k, 0) + c.valid * v / b.valid
        lfl_fam = {k: (v / w_tot if w_tot else None) for k, v in lfl_fam.items()}
        lfl_bloc = {k: (v / w_tot if w_tot else None) for k, v in lfl_bloc.items()}
        m_fam_now = self._sum_dicts(c.fam for c in matched)
        m_bloc_now = self._sum_dicts(c.bloc for c in matched)

        # LFL turnout: today's turnout vs. the same localities' baseline turnout,
        # weighted by eligible voters counted today (ordinary ballots only)
        t_num = t_den = t_base = 0.0
        for c in matched:
            b = self.base_cities[c.code]
            if c.code == ENVELOPES_CODE or not c.eligible or not b.eligible:
                continue
            t_num += c.voters
            t_den += c.eligible
            t_base += c.eligible * b.voters / b.eligible
        turnout_lfl_now = _pct(t_num, t_den)
        turnout_lfl_base = _pct(t_base, t_den)

        # ---- sectors
        sectors = self._sectors(cur_cities, rep, reporting_mode)

        # ---- projection
        projection = self._project(cur_cities, rep, letters, sectors)

        # ---- seats on counted votes
        agreements = self.cur.cfg.get("agreements", [])
        alloc = bader_ofer(letters, agreements, self.seats, self.threshold)
        seats_now = alloc["seats"]

        # ---- progress
        progress = self._progress(cur_cities, ballots, reporting_mode, tot)

        # ---- parties table
        parties = []
        for l, v in sorted(letters.items(), key=lambda kv: -kv[1]):
            p = self.cur.party(l)
            if v == 0 and l not in self.cur.cfg.get("parties", {}):
                continue
            fam = p["family"]
            single_fam = [x for x in letters if letters[x] and self.cur.family(x) == fam]
            pct = _pct(v, valid)
            proj = projection["letters"].get(l) if projection else None
            parties.append({
                "letter": l, "name": p["name"], "he": p.get("he", l), "short_he": p.get("short_he", l),
                "bloc": p["bloc"], "family": fam, "minor": not pct or pct < 0.01,
                "assumed": bool(p.get("assumed")), "unclassified": bool(p.get("unclassified")),
                "votes": v, "pct": _r(pct, 5),
                "passes": bool(valid and v >= valid * self.threshold),
                "to_threshold": max(0, int(valid * self.threshold - v) + 1) if valid else None,
                "seats": seats_now.get(l, 0),
                "proj_pct": _r(proj["pct"], 5) if proj else None,
                "proj_seats": proj["seats"] if proj else None,
                # comparison to baseline is only exact when the family has a single list today
                "base_family_pct": _r(_pct(self.base_fam.get(fam, 0), self.base_tot["valid"]), 5)
                if fam != "other" and len(single_fam) <= 1 else None,
                "lfl_base_pct": _r(lfl_fam.get(fam), 5) if fam != "other" and len(single_fam) <= 1 else None,
            })

        families = []
        for f, fc in self.cfg["families"].items():
            now = _pct(fam_now.get(f, 0), valid)
            base = _pct(self.base_fam.get(f, 0), self.base_tot["valid"])
            mnow = _pct(m_fam_now.get(f, 0), w_tot)
            lfl = lfl_fam.get(f)
            if not now and not base:
                continue
            fseats = sum(s for l, s in seats_now.items() if self.cur.family(l) == f)
            pf = projection["families"].get(f) if projection else None
            families.append({
                "key": f, "name": fc["name"], "color": fc.get("color"),
                "letters_now": [l for l in letters if self.cur.family(l) == f and letters[l]],
                "letters_base": [l for l in self.base_letters if self.base.family(l) == f and self.base_letters[l]],
                "votes": fam_now.get(f, 0), "pct": _r(now, 5), "base_pct": _r(base, 5),
                "lfl_base_pct": _r(lfl, 5), "swing": _r(mnow - lfl, 5) if (mnow is not None and lfl is not None) else None,
                "seats": fseats, "base_seats": self.base_fam_seats.get(f, 0),
                "proj_pct": _r(pf["pct"], 5) if pf else None, "proj_seats": pf["seats"] if pf else None,
            })
        families.sort(key=lambda x: -(x["pct"] or x["base_pct"] or 0))

        blocs = []
        for k, bc in self.cfg["blocs"].items():
            now = _pct(bloc_now.get(k, 0), valid)
            mnow = _pct(m_bloc_now.get(k, 0), w_tot)
            lfl = lfl_bloc.get(k)
            pb = projection["blocs"].get(k) if projection else None
            blocs.append({
                "key": k, "name": bc["name"], "color": bc.get("color"),
                "votes": bloc_now.get(k, 0), "pct": _r(now, 5),
                "base_pct": _r(_pct(self.base_bloc.get(k, 0), self.base_tot["valid"]), 5),
                "lfl_base_pct": _r(lfl, 5), "swing": _r(mnow - lfl, 5) if (mnow is not None and lfl is not None) else None,
                "seats": sum(s for l, s in seats_now.items() if self.cur.bloc(l) == k),
                "base_seats": self.base_bloc_seats.get(k, 0),
                "proj_pct": _r(pb["pct"], 5) if pb else None, "proj_seats": pb["seats"] if pb else None,
            })

        cities = self._cities(cur_cities, rep, reporting_mode)
        national = {
            "eligible_counted": tot["eligible"], "voters": tot["voters"], "invalid": tot["invalid"], "valid": valid,
            "envelopes_voters": tot["env_voters"], "envelopes_counted": tot["env_voters"] > 0,
            "turnout_raw": _r(_pct(tot["voters"], tot["eligible"])),
            "turnout_ordinary": _r(_pct(tot["voters"] - tot["env_voters"], tot["eligible"])),
            "turnout_lfl": _r(turnout_lfl_now), "turnout_lfl_base": _r(turnout_lfl_base),
            "turnout_lfl_delta": _r(turnout_lfl_now - turnout_lfl_base) if turnout_lfl_now is not None and turnout_lfl_base is not None else None,
            "invalid_pct": _r(_pct(tot["invalid"], tot["voters"])),
            "threshold_votes": int(alloc["threshold_votes"]),
            "lfl_coverage": _r(_pct(w_tot, valid)),
            "reporting_mode": reporting_mode,
        }
        base_national = {
            "eligible": self.base_tot["eligible"], "voters": self.base_tot["voters"], "valid": self.base_tot["valid"],
            "invalid": self.base_tot["invalid"],
            "turnout": _r(_pct(self.base_tot["voters"], self.base_tot["eligible"])),
            "turnout_ordinary": _r(_pct(self.base_tot["voters"] - self.base_tot["env_voters"], self.base_tot["eligible"])),
            "invalid_pct": _r(_pct(self.base_tot["invalid"], self.base_tot["voters"])),
            "seats": {l: s for l, s in self.base_seats.items() if s},
            "seats_source": self.base_seats_source,
        }
        state = {
            "meta": {**(meta or {}), "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "current": {"key": self.cur.key, "name": self.cur.cfg.get("name"), "date": self.cur.cfg.get("date")},
                     "baseline": {"key": self.base.key, "name": self.base.cfg.get("name"), "date": self.base.cfg.get("date")},
                     "threshold": self.threshold, "seats": self.seats, "majority": self.majority},
            "progress": progress, "national": national, "baseline_national": base_national,
            "parties": parties, "families": families, "blocs": blocs,
            "agreements": {"active": alloc["pairs"], "void": alloc["void_pairs"]},
            "sectors": sectors, "projection": projection and {
                "confidence": projection["confidence"], "expected_valid": projection["expected_valid"]},
            "cities": cities,
        }
        state["insights"] = self._insights(state)
        state["changes"] = self._changes(state, previous)
        return state

    # ------------------------------------------------------------------ pieces
    def _reporting_mode(self, cur_cities: dict) -> str:
        """Does the בזב column hold only the eligible voters of *counted* ballots
        (partial mode, turnout meaningful) or the full register of each locality?"""
        el = sum(c.eligible for c in cur_cities.values())
        vo = sum(c.voters for c in cur_cities.values() if c.code != ENVELOPES_CODE)
        exp_el = self.base_tot["eligible"] * self.growth
        exp_vo = (self.base_tot["voters"] - self.base_tot["env_voters"]) * self.growth
        if el > 0.95 * exp_el and vo < 0.6 * exp_vo:
            return "volume"
        return "eligible"

    def _progress(self, cur_cities, ballots, mode, tot) -> dict:
        out = {"localities_reporting": sum(1 for c in cur_cities.values() if c.code != ENVELOPES_CODE),
               "localities_total": len((set(self.base_cities) | set(cur_cities)) - {ENVELOPES_CODE}),
               "envelopes_counted": any(c.code == ENVELOPES_CODE for c in cur_cities.values())}
        exp_el = self.base_tot["eligible"] * self.growth
        exp_vo_ord = (self.base_tot["voters"] - self.base_tot["env_voters"]) * self.growth
        ord_voters = tot["voters"] - tot["env_voters"]
        out["by_eligible"] = _r(min(1.0, tot["eligible"] / exp_el)) if exp_el else None
        out["by_volume"] = _r(min(1.0, ord_voters / exp_vo_ord)) if exp_vo_ord else None
        if ballots is not None:
            counted = sum(1 for r in ballots.rows if r.voters or r.valid)
            out["ballots_counted"] = counted
            out["ballots_listed"] = len(ballots.rows)
            tb = self.cfg.get("total_ballots")
            if tb:
                out["ballots_total"], out["ballots_total_source"] = tb, "config"
            elif len(ballots.rows) > counted:
                out["ballots_total"], out["ballots_total_source"] = len(ballots.rows), "feed"
            elif self.base_ballots:
                out["ballots_total"] = sum(self.base_ballots.values())
                out["ballots_total_source"] = "baseline ballot count (estimate)"
        if out.get("ballots_total"):
            out["fraction"] = _r(min(1.0, out["ballots_counted"] / out["ballots_total"]))
            out["method"] = f"ballot boxes counted ({out['ballots_total_source']})"
        elif mode == "eligible":
            out["fraction"], out["method"] = out["by_eligible"], "eligible voters of counted ballots vs. register estimate"
        else:
            out["fraction"], out["method"] = out["by_volume"], "voters counted vs. baseline volume"
        return out

    def _sector_of(self, code: int) -> str:
        return self.sector.get(code, "new")

    def _sectors(self, cur_cities, rep, mode) -> list:
        agg: dict[str, dict] = {}
        for code, b in self.base_cities.items():
            s = self._sector_of(code)
            a = agg.setdefault(s, {"key": s, "name": SECTOR_NAMES[s], "localities": 0, "reporting": 0,
                                   "base_eligible": 0, "base_valid": 0, "exp": 0.0,
                                   "now_eligible": 0, "now_voters": 0, "now_valid": 0,
                                   "lfl_voters_base": 0.0, "bloc_now": {}, "bloc_lfl": {}, "fam_now": {}, "fam_lfl": {}})
            a["localities"] += 1
            a["base_eligible"] += b.eligible
            a["base_valid"] += b.valid
        for code, c in cur_cities.items():
            s = self._sector_of(code)
            a = agg.setdefault(s, {"key": s, "name": SECTOR_NAMES[s], "localities": 0, "reporting": 0,
                                   "base_eligible": 0, "base_valid": 0, "exp": 0.0,
                                   "now_eligible": 0, "now_voters": 0, "now_valid": 0,
                                   "lfl_voters_base": 0.0, "bloc_now": {}, "bloc_lfl": {}, "fam_now": {}, "fam_lfl": {}})
            a["reporting"] += 1
            b = self.base_cities.get(code)
            if not b or not c.valid:
                continue
            a["now_eligible"] += c.eligible
            a["now_voters"] += c.voters
            a["now_valid"] += c.valid
            if b.eligible:
                a["lfl_voters_base"] += c.eligible * b.voters / b.eligible
            for k, v in c.bloc.items():
                a["bloc_now"][k] = a["bloc_now"].get(k, 0) + v
            for k, v in b.bloc.items():
                a["bloc_lfl"][k] = a["bloc_lfl"].get(k, 0) + c.valid * v / b.valid
            for k, v in c.fam.items():
                a["fam_now"][k] = a["fam_now"].get(k, 0) + v
            for k, v in b.fam.items():
                a["fam_lfl"][k] = a["fam_lfl"].get(k, 0) + c.valid * v / b.valid
        out = []
        order = ["arab", "haredi", "large", "jewish", "envelopes", "new"]
        # Preserve custom override sectors (from config.sectors.overrides) by appending any
        # unknown keys at the end — otherwise aggregation includes them but output drops them.
        for s in list(agg.keys()):
            if s not in order:
                order.append(s)
        for s in order:
            if s not in agg:
                continue
            a = agg[s]
            nv = a["now_valid"]
            row = {
                "key": s, "name": a["name"], "localities": a["localities"], "reporting": a["reporting"],
                "base_valid": a["base_valid"], "now_valid": nv,
                "progress": _r(min(1.0, nv / (a["base_valid"] * self.growth))) if a["base_valid"] else None,
                "turnout": _r(_pct(a["now_voters"], a["now_eligible"])) if s != "envelopes" else None,
                "turnout_base": _r(_pct(a["lfl_voters_base"], a["now_eligible"])) if s != "envelopes" else None,
                "blocs": {k: {"pct": _r(_pct(a["bloc_now"].get(k, 0), nv)), "lfl": _r(_pct(a["bloc_lfl"].get(k, 0), nv))}
                          for k in self.cfg["blocs"]} if nv else {},
                "families": {k: {"pct": _r(_pct(a["fam_now"].get(k, 0), nv)), "lfl": _r(_pct(a["fam_lfl"].get(k, 0), nv))}
                             for k in self.cfg["families"] if a["fam_now"].get(k) or a["fam_lfl"].get(k)} if nv else {},
            }
            if row["turnout"] is not None and row["turnout_base"] is not None:
                row["turnout_delta"] = _r(row["turnout"] - row["turnout_base"])
            out.append(row)
        return out

    def _project(self, cur_cities, rep, letters, sectors) -> Optional[dict]:
        valid_now = sum(c.valid for c in cur_cities.values())
        if not valid_now:
            return None
        cur_letters = [l for l in letters]
        fam_letters: dict[str, list[str]] = {}
        for l in cur_letters:
            fam_letters.setdefault(self.cur.family(l), []).append(l)
        fam_now_tot = {f: sum(letters[l] for l in ls) for f, ls in fam_letters.items()}

        def split(l):  # share of letter l within its family, nationally so far
            f = self.cur.family(l)
            ft = fam_now_tot.get(f, 0)
            return letters[l] / ft if ft else 1.0 / len(fam_letters[f])

        # "other" family is not comparable letter by letter -> spread by current split as well.
        # Memoize per baseline city — the result only depends on `b` and the current letter set,
        # and this is called twice per locality inside the main projection loop.
        _base_est_cache: dict = {}

        def base_est(b: CityAgg) -> dict:
            cached = _base_est_cache.get(b.code)
            if cached is not None:
                return cached
            out = {l: b.share(b.fam, self.cur.family(l)) * split(l) for l in cur_letters}
            _base_est_cache[b.code] = out
            return out

        # sector-level swing per letter and turnout factor (shrunk toward national)
        nat_now: dict = {}
        nat_lfl: dict = {}
        nat_w = 0.0
        sec_now: dict = {}
        sec_lfl: dict = {}
        sec_w: dict = {}
        tf_num: dict = {}
        tf_den: dict = {}
        for code, c in cur_cities.items():
            b = self.base_cities.get(code)
            if not b or not c.valid or not b.valid:
                continue
            s = self._sector_of(code)
            est = base_est(b)
            sec_w[s] = sec_w.get(s, 0) + c.valid
            nat_w += c.valid
            for l in cur_letters:
                v = c.letters.get(l, 0)
                e = c.valid * est[l]
                sec_now.setdefault(s, {})[l] = sec_now.get(s, {}).get(l, 0) + v
                sec_lfl.setdefault(s, {})[l] = sec_lfl.get(s, {}).get(l, 0) + e
                nat_now[l] = nat_now.get(l, 0) + v
                nat_lfl[l] = nat_lfl.get(l, 0) + e
            if code != ENVELOPES_CODE and b.voters:
                tf_num[s] = tf_num.get(s, 0) + c.voters
                tf_den[s] = tf_den.get(s, 0) + b.voters * rep.get(code, 1.0) * self.growth
        if not nat_w:
            return None
        nat_swing = {l: (nat_now.get(l, 0) - nat_lfl.get(l, 0)) / nat_w for l in cur_letters}
        nat_tf = (sum(tf_num.values()) / sum(tf_den.values())) if sum(tf_den.values()) else 1.0

        def swing(s):
            w = sec_w.get(s, 0)
            k = w / (w + SHRINK_VOTES)
            return {l: k * ((sec_now.get(s, {}).get(l, 0) - sec_lfl.get(s, {}).get(l, 0)) / w if w else 0)
                    + (1 - k) * nat_swing[l] for l in cur_letters}

        def tfactor(s):
            d = tf_den.get(s, 0)
            k = d / (d + SHRINK_VOTES)
            return k * (tf_num.get(s, 0) / d if d else 1.0) + (1 - k) * nat_tf

        swings = {s: swing(s) for s in set(self.sector.values())}
        tfs = {s: tfactor(s) for s in set(self.sector.values())}
        proj_votes = {l: float(letters[l]) for l in cur_letters}
        expected_valid = float(valid_now)
        for code, b in self.base_cities.items():
            c = cur_cities.get(code)
            r = rep.get(code, 0.0) if c else 0.0
            if r >= 0.99 or not b.valid:
                continue
            s = self._sector_of(code)
            tf = nat_tf if code == ENVELOPES_CODE else tfs[s]
            remaining = max(0.0, b.valid * self.growth * tf - (c.valid if c else 0))
            if not remaining:
                continue
            est = base_est(b)
            sw = nat_swing if code == ENVELOPES_CODE else swings[s]
            shares = {l: max(0.0, est[l] + sw[l]) for l in cur_letters}
            tot = sum(shares.values())
            if not tot:
                continue
            for l in cur_letters:
                proj_votes[l] += remaining * shares[l] / tot
            expected_valid += remaining
        total = sum(proj_votes.values())
        alloc = bader_ofer(proj_votes, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        seats = alloc["seats"]
        fams: dict = {}
        blocs: dict = {}
        for l, v in proj_votes.items():
            f, bl = self.cur.family(l), self.cur.bloc(l)
            fams.setdefault(f, {"votes": 0.0, "seats": 0})
            blocs.setdefault(bl, {"votes": 0.0, "seats": 0})
            fams[f]["votes"] += v
            fams[f]["seats"] += seats.get(l, 0)
            blocs[bl]["votes"] += v
            blocs[bl]["seats"] += seats.get(l, 0)
        for d in (fams, blocs):
            for x in d.values():
                x["pct"] = x["votes"] / total if total else 0
        share_counted = valid_now / expected_valid if expected_valid else 0
        confidence = "high" if share_counted >= 0.6 else "medium" if share_counted >= 0.25 else "low"
        return {"letters": {l: {"votes": v, "pct": v / total if total else 0, "seats": seats.get(l, 0)}
                            for l, v in proj_votes.items()},
                "families": fams, "blocs": blocs, "confidence": confidence,
                "expected_valid": int(expected_valid)}

    def _cities(self, cur_cities, rep, mode) -> list:
        out = []
        codes = set(self.base_cities) | set(cur_cities)
        for code in codes:
            c, b = cur_cities.get(code), self.base_cities.get(code)
            ref = c or b
            row = {"code": code, "name": ref.name, "committee": ref.committee, "sector": self._sector_of(code),
                   "reporting": _r(rep.get(code, 0.0), 3) if c else 0.0}
            if b:
                bw = max(b.letters, key=b.letters.get) if b.letters else None
                row.update({
                    "base_eligible": b.eligible, "base_voters": b.voters, "base_valid": b.valid,
                    "base_turnout": _r(_pct(b.voters, b.eligible)),
                    "base_winner": bw, "base_winner_family": self.base.family(bw) if bw else None,
                    "base_winner_pct": _r(b.share(b.letters, bw)) if bw else None,
                    "base_blocs": {k: _r(b.share(b.bloc, k)) for k in self.cfg["blocs"]},
                })
            if c and c.valid:
                w = max(c.letters, key=c.letters.get) if c.letters else None
                row.update({
                    "eligible": c.eligible, "voters": c.voters, "valid": c.valid, "invalid": c.invalid,
                    "turnout": _r(_pct(c.voters, c.eligible)) if mode == "eligible" else None,
                    "winner": w, "winner_family": self.cur.family(w) if w else None,
                    "winner_pct": _r(c.share(c.letters, w)) if w else None,
                    "blocs": {k: _r(c.share(c.bloc, k)) for k in self.cfg["blocs"]},
                    "top": [[l, _r(c.share(c.letters, l))] for l in sorted(c.letters, key=c.letters.get, reverse=True)[:4]],
                })
                if b:
                    if row.get("turnout") is not None and row.get("base_turnout") is not None:
                        row["turnout_delta"] = _r(row["turnout"] - row["base_turnout"])
                    row["bloc_swing"] = {k: _r(c.share(c.bloc, k) - b.share(b.bloc, k)) for k in self.cfg["blocs"]}
                    fam_keys = set(c.fam) | set(b.fam)
                    deltas = {f: c.share(c.fam, f) - b.share(b.fam, f) for f in fam_keys}
                    row["family_swing"] = {f: _r(v) for f, v in sorted(deltas.items(), key=lambda kv: -abs(kv[1]))[:5]}
                    row["flipped"] = bool(w and row.get("base_winner_family") and self.cur.family(w) != row["base_winner_family"])
            out.append(row)
        out.sort(key=lambda r: -(r.get("base_eligible") or r.get("eligible") or 0))
        return out

    def city_detail(self, current: Optional[ResultFile], code: int) -> Optional[dict]:
        b = self.base_cities.get(code)
        c = None
        if current is not None:
            row = current.by_city().get(code)
            if row:
                c = CityAgg(row, self.cur)
        if not b and not c:
            return None
        fams = []
        for f, fc in self.cfg["families"].items():
            now = c.share(c.fam, f) if c and c.valid else None
            base = b.share(b.fam, f) if b else None
            if not now and not base:
                continue
            fams.append({"key": f, "name": fc["name"], "color": fc.get("color"), "pct": _r(now), "base_pct": _r(base),
                         "delta": _r(now - base) if now is not None and base is not None else None})
        fams.sort(key=lambda x: -(x["pct"] or x["base_pct"] or 0))
        return {
            "code": code, "name": (c or b).name, "sector": SECTOR_NAMES[self._sector_of(code)],
            "sector_key": self._sector_of(code), "base_ballots": self.base_ballots.get(code),
            "now": c and {"eligible": c.eligible, "voters": c.voters, "invalid": c.invalid, "valid": c.valid,
                          "letters": [{**self.cur.party(l), "votes": v, "pct": _r(c.share(c.letters, l))}
                                      for l, v in sorted(c.letters.items(), key=lambda kv: -kv[1])]},
            "base": b and {"eligible": b.eligible, "voters": b.voters, "invalid": b.invalid, "valid": b.valid,
                           "turnout": _r(_pct(b.voters, b.eligible)),
                           "letters": [{**self.base.party(l), "votes": v, "pct": _r(b.share(b.letters, l))}
                                       for l, v in sorted(b.letters.items(), key=lambda kv: -kv[1])[:20]]},
            "families": fams,
        }

    # ------------------------------------------------------------------ drill-downs
    def _ballot_summary(self, r: Row, prof: Profile) -> dict:
        letters = {k: v for k, v in r.votes.items() if v}
        top = sorted(letters, key=letters.get, reverse=True)
        blocs: dict = {}
        for l, v in letters.items():
            blocs[prof.bloc(l)] = blocs.get(prof.bloc(l), 0) + v
        return {"ballot": r.ballot, "eligible": r.eligible, "voters": r.voters, "invalid": r.invalid, "valid": r.valid,
                "turnout": _r(_pct(r.voters, r.eligible)),
                "winner": top[0] if top else None, "winner_pct": _r(_pct(letters[top[0]], r.valid)) if top else None,
                "top": [[l, _r(_pct(letters[l], r.valid))] for l in top[:3]],
                "blocs": {k: _r(_pct(v, r.valid)) for k, v in blocs.items()}}

    def city_ballots(self, ballots: Optional[ResultFile], code: int) -> dict:
        """Every ballot box of a locality, now and (same ballot number) in the baseline."""
        now = {r.ballot: r for r in (ballots.rows if ballots else []) if r.code == code}
        base = {k[1]: r for k, r in self.base_ballot_rows.items() if k[0] == code}

        def key(b):
            try:
                return (0, float(b))
            except (TypeError, ValueError):
                return (1, str(b))

        out = []
        for b in sorted(set(now) | set(base), key=key):
            row = {"ballot": b}
            if b in now and (now[b].voters or now[b].valid):
                row["now"] = self._ballot_summary(now[b], self.cur)
            if b in base:
                row["base"] = self._ballot_summary(base[b], self.base)
            out.append(row)
        return {"code": code, "ballots": out, "counted": sum(1 for r in out if "now" in r),
                "base_total": len(base), "has_feed": ballots is not None}

    def ballot_detail(self, ballots: Optional[ResultFile], code: int, ballot: str) -> Optional[dict]:
        r = next((r for r in (ballots.rows if ballots else []) if r.code == code and r.ballot == ballot), None)
        b = self.base_ballot_rows.get((code, ballot))
        if not r and not b:
            return None

        def lst(row, prof):
            return [{**prof.party(l), "votes": v, "pct": _r(_pct(v, row.valid))}
                    for l, v in sorted(row.votes.items(), key=lambda kv: -kv[1]) if v]
        return {"code": code, "ballot": ballot, "name": (r or b).name,
                "now": r and {**self._ballot_summary(r, self.cur), "letters": lst(r, self.cur)},
                "base": b and {**self._ballot_summary(b, self.base), "letters": lst(b, self.base)}}

    def party_detail(self, current: Optional[ResultFile], letter: str) -> Optional[dict]:
        if current is None:
            return None
        p = self.cur.party(letter)
        fam = p["family"]
        cities = []
        tot_v = tot_valid = 0
        by_sector: dict = {}
        for code, row in current.by_city().items():
            if not row.valid:
                continue
            v = row.votes.get(letter, 0)
            tot_v += v
            tot_valid += row.valid
            b = self.base_cities.get(code)
            base_fam = b.share(b.fam, fam) if (b and fam != "other") else None
            now_fam = sum(x for l, x in row.votes.items() if self.cur.family(l) == fam) / row.valid
            sec = self._sector_of(code)
            a = by_sector.setdefault(sec, {"key": sec, "votes": 0, "valid": 0, "lfl": 0.0})
            a["votes"] += v
            a["valid"] += row.valid
            if base_fam is not None:
                a["lfl"] += row.valid * base_fam
            cities.append({"code": code, "name": row.name, "sector": sec, "valid": row.valid, "votes": v,
                           "pct": _r(v / row.valid), "family_pct": _r(now_fam),
                           "base_family_pct": _r(base_fam),
                           "delta": _r(now_fam - base_fam) if base_fam is not None else None})
        hist = [0] * 20
        for c in cities:
            if c["code"] != ENVELOPES_CODE and c["valid"] >= 200:
                hist[min(19, int((c["pct"] or 0) * 20))] += 1
        big = [c for c in cities if c["valid"] >= 2000]
        return {
            **p, "votes": tot_v, "pct": _r(_pct(tot_v, tot_valid), 5),
            "family_name": self.cfg["families"].get(fam, {}).get("he") or self.cfg["families"].get(fam, {}).get("name"),
            "sectors": [{"key": k, "name": SECTOR_HE.get(k, k), "votes": a["votes"], "pct": _r(_pct(a["votes"], a["valid"])),
                         "share_of_party": _r(_pct(a["votes"], tot_v)),
                         "lfl_family_pct": _r(a["lfl"] / a["valid"]) if a["valid"] and fam != "other" else None}
                        for k, a in sorted(by_sector.items(), key=lambda kv: -kv[1]["votes"])],
            "strongest": sorted(big, key=lambda c: -(c["pct"] or 0))[:25],
            "most_votes": sorted(cities, key=lambda c: -c["votes"])[:25],
            "gains": sorted([c for c in big if c["delta"] is not None], key=lambda c: -c["delta"])[:15],
            "losses": sorted([c for c in big if c["delta"] is not None], key=lambda c: c["delta"])[:15],
            "histogram": hist,
            "cities": len(cities),
        }

    def party_matrix(self, current: Optional[ResultFile]) -> dict:
        """Per-party per-sector vote share — the matrix behind the analytics heatmap.
        Returns {sectors: [key,...], parties: [letter,...], share: {letter: {sector: pct}}, votes: {letter: int}}."""
        if current is None:
            return {"sectors": [], "parties": [], "share": {}, "votes": {}}
        sec_totals: dict[str, int] = {}
        sec_letters: dict[str, dict[str, int]] = {}
        letter_tot: dict[str, int] = {}
        for code, row in current.by_city().items():
            if not row.valid:
                continue
            s = self._sector_of(code)
            sec_totals[s] = sec_totals.get(s, 0) + row.valid
            d = sec_letters.setdefault(s, {})
            for l, v in row.votes.items():
                if not v:
                    continue
                d[l] = d.get(l, 0) + v
                letter_tot[l] = letter_tot.get(l, 0) + v
        sectors = [s for s in ("arab", "haredi", "large", "jewish", "envelopes", "new") if s in sec_totals]
        letters = [l for l, _ in sorted(letter_tot.items(), key=lambda kv: -kv[1]) if letter_tot[l] > 0]
        share = {}
        for l in letters:
            share[l] = {}
            for s in sectors:
                tot = sec_totals.get(s, 0)
                v = sec_letters.get(s, {}).get(l, 0)
                share[l][s] = _r(v / tot, 5) if tot else None
        return {"sectors": [{"key": s, "name_he": SECTOR_HE.get(s, s), "valid": sec_totals[s]} for s in sectors],
                "parties": [{"letter": l, **self.cur.party(l), "votes": letter_tot[l]} for l in letters],
                "share": share}

    def whatif(self, current: Optional[ResultFile], ballots: Optional[ResultFile],
               turnout_delta: Optional[dict] = None, bloc_swing: Optional[dict] = None,
               family_swing: Optional[dict] = None) -> Optional[dict]:
        """Recompute the projection with user-supplied adjustments applied to each sector.

        turnout_delta: {sector_key: additional_turnout_fraction}  (added to current sector turnout)
        bloc_swing:   {bloc_key:   extra_share}                   (added to bloc share in each sector)
        family_swing: {family_key: extra_share}                   (added to family share in each sector)
        Returns a projection-shaped dict so the UI can render it next to the real one."""
        if current is None:
            return None
        tdelta = turnout_delta or {}
        bswing = bloc_swing or {}
        fswing = family_swing or {}
        cur_cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())}
        letters = self._sum_dicts(c.letters for c in cur_cities.values())
        for p in self.cur.cfg.get("parties", {}):
            letters.setdefault(p, 0)
        reporting_mode = self._reporting_mode(cur_cities)
        counted_ballots: dict[int, int] = {}
        if ballots is not None:
            for r in ballots.rows:
                if r.voters or r.valid:
                    counted_ballots[r.code] = counted_ballots.get(r.code, 0) + 1
        rep: dict[int, float] = {}
        for code, c in cur_cities.items():
            b = self.base_cities.get(code)
            if not b:
                rep[code] = 1.0
            elif counted_ballots and self.base_ballots.get(code):
                rep[code] = min(1.0, counted_ballots.get(code, 0) / self.base_ballots[code])
            elif code == ENVELOPES_CODE or reporting_mode == "volume" or not c.eligible:
                rep[code] = min(1.0, c.voters / (b.voters * self.growth)) if b.voters else 1.0
            else:
                rep[code] = min(1.0, c.eligible / (b.eligible * self.growth)) if b.eligible else 1.0
        sectors = self._sectors(cur_cities, rep, reporting_mode)
        proj = self._project(cur_cities, rep, letters, sectors)
        if not proj:
            return None
        # Apply user adjustments by re-weighting the projected vote per party, per sector influence.
        # Simple model: scale each party's projected votes by (1 + family_swing * sector_weight +
        # bloc_swing * sector_weight), and multiply the expected-valid by the turnout delta's sector weight.
        if not (tdelta or bswing or fswing):
            seats = {l: proj["letters"][l]["seats"] for l in proj["letters"]}
            return {"letters": {l: {**d, "seats": seats[l]} for l, d in proj["letters"].items()},
                    "families": proj["families"], "blocs": proj["blocs"], "confidence": proj["confidence"],
                    "expected_valid": proj["expected_valid"], "adjusted": False}
        # per-sector weight: fraction of each party's remaining votes expected from each sector.
        sector_weight: dict[str, dict[str, float]] = {l: {} for l in proj["letters"]}
        for code, b in self.base_cities.items():
            c = cur_cities.get(code)
            r = rep.get(code, 0.0) if c else 0.0
            if r >= 0.99 or not b.valid:
                continue
            s = self._sector_of(code)
            remaining = max(0.0, b.valid * self.growth - (c.valid if c else 0))
            if not remaining:
                continue
            for l, v in b.letters.items():
                if l in sector_weight:
                    sector_weight[l][s] = sector_weight[l].get(s, 0.0) + remaining * v / b.valid
        votes = {l: proj["letters"][l]["votes"] for l in proj["letters"]}
        expected_valid = float(proj["expected_valid"])
        for l in votes:
            p = self.cur.party(l)
            scale = 1.0
            for s, w in sector_weight[l].items():
                if not w:
                    continue
                boost = tdelta.get(s, 0.0) + bswing.get(p.get("bloc"), 0.0) + fswing.get(p.get("family"), 0.0)
                scale += boost * (w / sum(sector_weight[l].values()) if sum(sector_weight[l].values()) else 0)
            votes[l] *= max(0.0, scale)
        tot = sum(votes.values())
        alloc = bader_ofer(votes, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        seats = alloc["seats"]
        fams: dict = {}
        blocs: dict = {}
        for l, v in votes.items():
            p = self.cur.party(l)
            f, bl = p.get("family", "other"), p.get("bloc", "X")
            fams.setdefault(f, {"votes": 0.0, "seats": 0})
            blocs.setdefault(bl, {"votes": 0.0, "seats": 0})
            fams[f]["votes"] += v
            fams[f]["seats"] += seats.get(l, 0)
            blocs[bl]["votes"] += v
            blocs[bl]["seats"] += seats.get(l, 0)
        for d in (fams, blocs):
            for x in d.values():
                x["pct"] = x["votes"] / tot if tot else 0
        return {"letters": {l: {"votes": v, "pct": v / tot if tot else 0, "seats": seats.get(l, 0)}
                            for l, v in votes.items()},
                "families": fams, "blocs": blocs, "confidence": proj["confidence"],
                "expected_valid": int(expected_valid), "adjusted": True,
                "deltas": {"turnout": tdelta, "bloc": bswing, "family": fswing}}

    def search(self, current: Optional[ResultFile], q: str) -> list:
        q = q.strip()
        if not q:
            return []
        out = []
        for code, c in self.base_cities.items():
            if q in c.name or q == str(code):
                out.append({"type": "city", "code": code, "name": c.name, "eligible": c.eligible})
        out.sort(key=lambda x: (not x["name"].startswith(q), -x["eligible"]))
        letters = set(self.cur.cfg.get("parties", {})) | set(self.cur.names) | set(current.parties if current else [])
        parties = [{"type": "party", "letter": l, "name": self.cur.party(l)["short_he"], "he": self.cur.party(l)["he"]}
                   for l in letters if q in l or q in self.cur.party(l)["he"] or q.lower() in self.cur.party(l)["name"].lower()]
        return parties[:8] + out[:20]

    # ------------------------------------------------------------------ insights
    def _insights(self, st: dict) -> list:
        ins = []
        pg, nat = st["progress"], st["national"]
        fam_he = {k: v.get("he") or v["name"] for k, v in self.cfg["families"].items()}
        bloc_he = {k: v.get("he") or v["name"] for k, v in self.cfg["blocs"].items()}

        def add(kind, level, text):
            ins.append({"kind": kind, "level": level, "text": text})

        def pp(x):  # the LRM keeps the sign in front of the number inside Hebrew text
            return f"\u200e{x*100:+.1f}"

        if not nat["valid"]:
            add("status", "info", "טרם פורסמו תוצאות. המערכת ממשיכה לבדוק את האתר.")
            return ins
        frac = pg.get("fraction") or 0
        add("status", "info",
            f"נספרו כ-{frac*100:.1f}% מהקולות; {pg['localities_reporting']:,} מתוך {pg['localities_total']:,} יישובים דיווחו; "
            f"מעטפות חיצוניות {'נספרו' if pg['envelopes_counted'] else 'טרם נספרו'}.")
        if nat.get("turnout_lfl_delta") is not None:
            d = nat["turnout_lfl_delta"]
            add("turnout", "up" if d >= 0.001 else "down" if d <= -0.001 else "info",
                f"שיעור ההצבעה ביישובים שדיווחו: {nat['turnout_lfl']*100:.1f}% לעומת {nat['turnout_lfl_base']*100:.1f}% "
                f"באותם יישובים בבחירות הקודמות ({pp(d)} נק׳ אחוז).")
        for s in st["sectors"]:
            if s.get("turnout_delta") is not None and s["now_valid"] > 2000 and abs(s["turnout_delta"]) >= 0.02:
                d = s["turnout_delta"]
                add("sector", "up" if d > 0 else "down",
                    f"{SECTOR_HE.get(s['key'], s['name'])}: הצבעה {s['turnout']*100:.1f}% לעומת {s['turnout_base']*100:.1f}% "
                    f"({pp(d)} נק׳), {s['reporting']}/{s['localities']} יישובים.")
        bl = {b["key"]: b for b in st["blocs"]}
        n = bl.get("N")
        if n:
            ps = n.get("proj_seats")
            add("seats", "key", f"{bloc_he['N']}: {n['seats']} מנדטים לפי הקולות שנספרו"
                + (f", {ps} בתחזית" if ps is not None else "")
                + f" (בבחירות הקודמות {n['base_seats']}; רוב = {self.majority}).")
        for b in st["blocs"]:
            if b.get("swing") is not None and abs(b["swing"]) >= 0.01:
                add("swing", "up" if b["swing"] > 0 else "down",
                    f"{bloc_he[b['key']]} {pp(b['swing'])} נק׳ לעומת אותם יישובים בבחירות הקודמות ({b['pct']*100:.1f}% כעת).")
        fams = [f for f in st["families"] if f.get("swing") is not None]
        if fams and max(abs(f["swing"]) for f in fams) >= 0.001:
            g = max(fams, key=lambda f: f["swing"])
            l = min(fams, key=lambda f: f["swing"])
            add("swing", "up", f"המתחזקת ביותר: {fam_he.get(g['key'])} {pp(g['swing'])} נק׳ בהשוואה זהה.")
            add("swing", "down", f"הנחלשת ביותר: {fam_he.get(l['key'])} {pp(l['swing'])} נק׳ בהשוואה זהה.")
        for p in st["parties"]:
            if p["pct"] is None or not 0.025 <= p["pct"] <= 0.04:
                continue
            if p["passes"]:
                add("threshold", "warn", f"{p['short_he']} ({p['letter']}) מעל אחוז החסימה בפער קטן: {p['pct']*100:.2f}%.")
            else:
                add("threshold", "warn", f"{p['short_he']} ({p['letter']}) מתחת לאחוז החסימה: {p['pct']*100:.2f}%, "
                                         f"חסרים {p['to_threshold']:,} קולות.")
        if st["agreements"]["void"]:
            add("seats", "info", "הסכמי עודפים שבוטלו (שותפה מתחת לחסימה): "
                + ", ".join("–".join(p) for p in st["agreements"]["void"]))
        solid = [c for c in st["cities"] if c.get("valid") and (c.get("base_eligible") or 0) >= 5000
                 and c.get("reporting", 0) >= 0.8 and c["sector"] != "envelopes"]
        tc = [c for c in solid if c.get("turnout_delta") is not None]
        up = sorted((c for c in tc if c["turnout_delta"] >= 0.005), key=lambda c: -c["turnout_delta"])[:5]
        dn = sorted((c for c in tc if c["turnout_delta"] <= -0.005), key=lambda c: c["turnout_delta"])[:5]
        if up:
            add("cities", "up", "העלייה הגדולה בהצבעה: " + ", ".join(f"{c['name']} {pp(c['turnout_delta'])}" for c in up))
        if dn:
            add("cities", "down", "הירידה הגדולה בהצבעה: " + ", ".join(f"{c['name']} {pp(c['turnout_delta'])}" for c in dn))
        flips = [c for c in solid if c.get("flipped")]
        if flips:
            add("cities", "key", f"{len(flips)} יישובים גדולים החליפו את המפלגה המובילה, למשל: " + ", ".join(
                f"{c['name']} ({fam_he.get(c['base_winner_family'])} ← {fam_he.get(c['winner_family'])})" for c in flips[:6]))
        sw = [c for c in solid if c.get("bloc_swing") and max(abs(v or 0) for v in c["bloc_swing"].values()) >= 0.01]
        if sw:
            big = sorted(sw, key=lambda c: -max(abs(v or 0) for v in c["bloc_swing"].values()))[:5]
            add("cities", "info", "התזוזות הגדולות בין גושים: " + ", ".join(
                f"{c['name']} ({bloc_he[max(c['bloc_swing'].items(), key=lambda kv: abs(kv[1] or 0))[0]]} "
                f"{pp(max(c['bloc_swing'].values(), key=lambda v: abs(v or 0)))})" for c in big))
        if nat.get("invalid_pct") is not None:
            bi = st["baseline_national"]["invalid_pct"]
            add("turnout", "info", f"קולות פסולים: {nat['invalid_pct']*100:.2f}% מהמצביעים (בבחירות הקודמות {bi*100:.2f}%).")
        return ins

    def _changes(self, st: dict, prev: Optional[dict]) -> dict:
        if not prev:
            return {"first": True}
        pc = {c["code"]: c for c in prev.get("cities", []) if c.get("valid")}
        new = [c for c in st["cities"] if c.get("valid") and c["code"] not in pc]
        grew = [c for c in st["cities"] if c.get("valid") and c["code"] in pc and c["valid"] > pc[c["code"]]["valid"]]
        pp = {p["letter"]: p for p in prev.get("parties", [])}
        moves = []
        crossings = []  # letters crossing the threshold up or down in this update
        thr = self.threshold
        for p in st["parties"]:
            o = pp.get(p["letter"])
            if o and p["pct"] is not None and o["pct"] is not None:
                moves.append({"letter": p["letter"], "name": p["name"], "delta": _r(p["pct"] - o["pct"], 5),
                              "seats_delta": p["seats"] - o["seats"]})
                # Threshold crossing detection: both pcts known, one above, one below.
                was_above = o["pct"] >= thr
                is_above = p["pct"] >= thr
                if was_above != is_above:
                    crossings.append({"letter": p["letter"], "name": p["name"], "short_he": p.get("short_he", p["letter"]),
                                      "direction": "up" if is_above else "down",
                                      "from_pct": _r(o["pct"], 5), "to_pct": _r(p["pct"], 5),
                                      "seats_delta": p["seats"] - o["seats"]})
        moves.sort(key=lambda m: -abs(m["delta"]))
        return {
            "first": False,
            "since": prev.get("meta", {}).get("analyzed_at"),
            "votes_added": st["national"]["valid"] - prev.get("national", {}).get("valid", 0),
            "new_localities": [c["name"] for c in sorted(new, key=lambda c: -(c.get("valid") or 0))[:15]],
            "new_localities_count": len(new),
            "new_codes": [c["code"] for c in new][:300],
            "updated_localities_count": len(grew),
            "party_moves": moves[:8],
            "threshold_crossings": crossings,
        }

    # ------------------------------------------------------------------ advanced analytics

    def wasted_votes(self, current: Optional[ResultFile]) -> dict:
        """Votes that went to parties below the threshold — 'wasted' on representation."""
        if not current:
            return {"wasted": 0, "wasted_pct": 0, "parties": [], "total_valid": 0}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"wasted": 0, "wasted_pct": 0, "parties": [], "total_valid": 0}
        wasted_parties = []
        total_wasted = 0
        for l, v in sorted(letters.items(), key=lambda kv: -kv[1]):
            pct = v / total_valid
            if pct < self.threshold:
                p = self.cur.party(l)
                total_wasted += v
                wasted_parties.append({
                    "letter": l, "short_he": p["short_he"], "votes": v,
                    "pct": _r(pct, 5), "bloc": p["bloc"], "family": p["family"],
                    "votes_to_threshold": max(0, int(self.threshold * total_valid) - v + 1),
                })
        # Also compute wasted by bloc
        bloc_wasted: dict[str, int] = {}
        for p in wasted_parties:
            bloc_wasted[p["bloc"]] = bloc_wasted.get(p["bloc"], 0) + p["votes"]
        return {
            "wasted": total_wasted,
            "wasted_pct": _r(total_wasted / total_valid, 5),
            "total_valid": total_valid,
            "parties": wasted_parties,
            "bloc_wasted": {b: {"votes": v, "pct": _r(v / total_valid, 5)} for b, v in bloc_wasted.items()},
        }

    def momentum(self, history: list) -> dict:
        """Rate-of-change (acceleration) of each party's % share across recent snapshots."""
        snapshots = [h for h in history if h and h.get("parties")]
        if len(snapshots) < 3:
            return {"parties": [], "snapshots": len(snapshots), "min_needed": 3}
        # Use last 12 snapshots
        recent = snapshots[-12:]
        # Build per-letter pct time series
        all_letters: set = set()
        for snap in recent:
            for p in snap["parties"]:
                if p.get("pct") is not None:
                    all_letters.add(p["letter"])
        series: dict[str, list] = {l: [] for l in all_letters}
        for snap in recent:
            by_letter = {p["letter"]: p for p in snap["parties"]}
            for l in all_letters:
                pct = by_letter.get(l, {}).get("pct")
                series[l].append(pct)
        results = []
        for l, pts in series.items():
            valid_pts = [(i, v) for i, v in enumerate(pts) if v is not None]
            if len(valid_pts) < 4:
                continue
            # Momentum: slope of last 4 points (linear regression over index)
            n = len(valid_pts)
            xs = [p[0] for p in valid_pts[-6:]]
            ys = [p[1] for p in valid_pts[-6:]]
            xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
            num = sum((xi - xm) * (yi - ym) for xi, yi in zip(xs, ys))
            den = sum((xi - xm) ** 2 for xi in xs)
            slope = num / den if den else 0.0
            # Acceleration: slope of slopes (second derivative) using early vs late half
            mid = len(valid_pts) // 2
            early = valid_pts[:mid]
            late = valid_pts[mid:]
            def _slope(pts2):
                if len(pts2) < 2: return 0.0
                xs2, ys2 = [p[0] for p in pts2], [p[1] for p in pts2]
                xm2, ym2 = sum(xs2) / len(xs2), sum(ys2) / len(ys2)
                n2 = sum((xi - xm2) * (yi - ym2) for xi, yi in zip(xs2, ys2))
                d2 = sum((xi - xm2) ** 2 for xi in xs2)
                return n2 / d2 if d2 else 0.0
            acceleration = _slope(late) - _slope(early)
            p_info = self.cur.party(l)
            latest_pct = next((v for _, v in reversed(valid_pts)), None)
            results.append({
                "letter": l, "short_he": p_info["short_he"], "bloc": p_info["bloc"],
                "family": p_info["family"], "pct": _r(latest_pct, 5),
                "momentum": _r(slope * 100, 4),   # pp per snapshot
                "acceleration": _r(acceleration * 100, 4),
                "series": [_r(v, 5) for v in pts],
                "direction": "up" if slope > 0.0002 else "down" if slope < -0.0002 else "flat",
            })
        results.sort(key=lambda x: -abs(x["momentum"] or 0))
        return {"parties": results[:20], "snapshots": len(recent), "total_snapshots": len(snapshots)}

    def tipping_points(self, current: Optional[ResultFile]) -> dict:
        """Localities whose swing most influences national seat allocation."""
        if not current:
            return {"cities": [], "total_valid": 0}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"cities": [], "total_valid": 0}
        base_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        base_seats = base_alloc["seats"]
        base_n_seats = sum(v for l, v in base_seats.items() if self.cur.party(l)["bloc"] == "N")
        tipping = []
        for code, city in sorted(cities.items(), key=lambda kv: -(kv[1].valid or 0))[:60]:
            if city.valid < 200 or city.code == ENVELOPES_CODE:
                continue
            # Simulate: transfer 5% of this city's votes from largest bloc winner to 2nd bloc
            weight = city.valid / total_valid
            n_share = city.bloc.get("N", 0) / city.valid if city.valid else 0
            o_share = city.bloc.get("O", 0) / city.valid if city.valid else 0
            shift = city.valid * 0.03  # 3% swing within city
            if n_share > o_share:
                # Shift N->O: reduce N letters, increase O letters proportionally
                tweaked = dict(letters)
                for l, v in city.letters.items():
                    p = self.cur.party(l)
                    if p["bloc"] == "N":
                        tweaked[l] = max(0, tweaked.get(l, v) - int(v * 0.03))
                    elif p["bloc"] == "O":
                        tweaked[l] = tweaked.get(l, v) + int(city.letters.get(l, 0) * 0.03 + shift / max(1, sum(1 for ll in city.letters if self.cur.party(ll)["bloc"] == "O")))
            else:
                tweaked = dict(letters)
                for l, v in city.letters.items():
                    p = self.cur.party(l)
                    if p["bloc"] == "O":
                        tweaked[l] = max(0, tweaked.get(l, v) - int(v * 0.03))
                    elif p["bloc"] == "N":
                        tweaked[l] = tweaked.get(l, v) + int(city.letters.get(l, 0) * 0.03 + shift / max(1, sum(1 for ll in city.letters if self.cur.party(ll)["bloc"] == "N")))
            alt_alloc = bader_ofer(tweaked, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
            alt_n_seats = sum(v for l, v in alt_alloc["seats"].items() if self.cur.party(l)["bloc"] == "N")
            seat_swing = alt_n_seats - base_n_seats
            bc = self.base_cities.get(code)
            tipping.append({
                "code": code, "name": city.name,
                "eligible": bc.eligible if bc else city.eligible,
                "valid": city.valid,
                "reporting": _r(city.valid / (bc.eligible * 0.72) if bc and bc.eligible else None, 3),
                "sector": self.sector.get(code, "jewish"),
                "n_share": _r(n_share, 4), "o_share": _r(o_share, 4),
                "weight": _r(weight, 5),
                "seat_swing": seat_swing,
                "influential": abs(seat_swing) >= 1,
            })
        tipping.sort(key=lambda x: (-abs(x["seat_swing"]), -x["valid"]))
        return {"cities": tipping[:30], "total_valid": total_valid, "base_n_seats": base_n_seats}

    def benford(self, current: Optional[ResultFile]) -> dict:
        """Benford's Law first-digit analysis of per-locality vote counts."""
        if not current:
            return {"observed": {}, "expected": {}, "chi2": None, "counts": 0}
        cities = list(current.by_city().values())
        # Collect first digits from per-party-per-city vote totals
        digit_counts = [0] * 10  # index 0 unused
        n_obs = 0
        for row in cities:
            for v in row.votes.values():
                if v and v >= 10:  # ignore single-digit counts (too noisy)
                    d = int(str(v)[0])
                    if 1 <= d <= 9:
                        digit_counts[d] += 1
                        n_obs += 1
        if n_obs < 50:
            return {"observed": {}, "expected": {}, "chi2": None, "counts": n_obs, "sufficient": False}
        expected = {str(d): math.log10(1 + 1 / d) for d in range(1, 10)}
        observed = {str(d): digit_counts[d] / n_obs for d in range(1, 10)}
        # Chi-squared statistic
        chi2 = sum((digit_counts[d] - n_obs * expected[str(d)]) ** 2 / (n_obs * expected[str(d)]) for d in range(1, 10))
        # p-value approximation (8 df) using chi2 CDF
        # Simple categorization: chi2 < 15.5 (p>0.05) -> conforming, 15.5-20 -> borderline, >20 -> non-conforming
        conformance = "conforming" if chi2 < 15.5 else "borderline" if chi2 < 20.0 else "non-conforming"
        return {
            "observed": {k: _r(v, 5) for k, v in observed.items()},
            "expected": {k: _r(v, 5) for k, v in expected.items()},
            "digit_counts": {str(d): digit_counts[d] for d in range(1, 10)},
            "chi2": _r(chi2, 3),
            "counts": n_obs,
            "sufficient": True,
            "conformance": conformance,
        }

    def elasticity(self, current: Optional[ResultFile]) -> dict:
        """For each party: how many seats gained/lost if they received ±1pp more votes nationally."""
        if not current:
            return {"parties": [], "total_valid": 0}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"parties": [], "total_valid": 0}
        base_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        base_seats = base_alloc["seats"]
        shift_votes = int(total_valid * 0.01)  # 1 percentage point
        results = []
        for l, base_v in sorted(letters.items(), key=lambda kv: -kv[1]):
            if base_v < self.threshold * total_valid * 0.5:
                continue  # skip tiny parties far below threshold
            p_info = self.cur.party(l)
            up_letters = dict(letters)
            up_letters[l] = base_v + shift_votes
            up_alloc = bader_ofer(up_letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
            dn_letters = dict(letters)
            dn_letters[l] = max(0, base_v - shift_votes)
            dn_alloc = bader_ofer(dn_letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
            results.append({
                "letter": l,
                "short_he": p_info["short_he"],
                "bloc": p_info["bloc"],
                "family": p_info["family"],
                "seats": base_seats.get(l, 0),
                "pct": _r(base_v / total_valid, 5),
                "gain_1pp": up_alloc["seats"].get(l, 0) - base_seats.get(l, 0),
                "lose_1pp": dn_alloc["seats"].get(l, 0) - base_seats.get(l, 0),
            })
        results.sort(key=lambda x: -(x["seats"] or 0))
        return {"parties": results, "total_valid": total_valid, "shift_votes": shift_votes}

    def swing_gradient(self, current: Optional[ResultFile]) -> dict:
        """Correlation of each party's share vs. city size (eligible voters) — are they urban or rural?"""
        if not current:
            return {"parties": []}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid and c.valid >= 100}
        top_letters = sorted(
            self._sum_dicts(c.letters for c in cities.values()).items(),
            key=lambda kv: -kv[1]
        )[:12]
        results = []
        for l, _ in top_letters:
            p_info = self.cur.party(l)
            pts = []
            for code, city in cities.items():
                bc = self.base_cities.get(code)
                eligible = bc.eligible if bc else city.eligible
                if eligible < 500 or city.code == ENVELOPES_CODE:
                    continue
                share = city.letters.get(l, 0) / city.valid if city.valid else 0
                pts.append((eligible, share))
            if len(pts) < 10:
                continue
            # Pearson correlation between log(eligible) and share
            log_eligs = [math.log(p[0]) for p in pts]
            shares = [p[1] for p in pts]
            n = len(pts)
            xm = sum(log_eligs) / n
            ym = sum(shares) / n
            num = sum((xi - xm) * (yi - ym) for xi, yi in zip(log_eligs, shares))
            den_x = math.sqrt(sum((xi - xm) ** 2 for xi in log_eligs))
            den_y = math.sqrt(sum((yi - ym) ** 2 for yi in shares))
            corr = num / (den_x * den_y) if den_x * den_y else 0.0
            # Decile breakdown: mean share in each size decile
            pts_sorted = sorted(pts, key=lambda p: p[0])
            decile_size = max(1, n // 5)
            quintiles = []
            for qi in range(5):
                chunk = pts_sorted[qi * decile_size:(qi + 1) * decile_size]
                if chunk:
                    quintiles.append({
                        "q": qi + 1,
                        "mean_eligible": int(sum(p[0] for p in chunk) / len(chunk)),
                        "mean_share": _r(sum(p[1] for p in chunk) / len(chunk), 5),
                    })
            results.append({
                "letter": l,
                "short_he": p_info["short_he"],
                "bloc": p_info["bloc"],
                "family": p_info["family"],
                "corr": _r(corr, 4),
                "profile": "urban" if corr > 0.15 else "rural" if corr < -0.15 else "uniform",
                "n_cities": n,
                "quintiles": quintiles,
            })
        results.sort(key=lambda x: x["corr"] or 0, reverse=True)
        return {"parties": results}

    def monte_carlo(self, current: Optional[ResultFile], ballots: Optional[ResultFile],
                    n: int = 400) -> dict:
        """Monte Carlo seat simulation: add counting noise to get seat confidence intervals."""
        if not current:
            return {"parties": [], "iterations": 0}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid or len(letters) < 2:
            return {"parties": [], "iterations": 0}
        # Estimate counting fraction
        tot_eligible = sum((self.base_cities[code].eligible if code in self.base_cities else 0) for code in cities)
        base_tot_eligible = sum(c.eligible for c in self.base_cities.values())
        frac = min(0.99, total_valid / max(1, base_tot_eligible * 0.72 * self.growth))
        # Remaining votes = baseline-derived estimate of uncounted votes
        remaining_letters: dict[str, float] = {}
        for code, bc in self.base_cities.items():
            if code in cities:
                continue  # already counted
            for l, bv in bc.letters.items():
                expected = bv * self.growth * 0.72  # rough expected votes
                remaining_letters[l] = remaining_letters.get(l, 0.0) + expected
        rng = random.Random(42)
        seat_counts: dict[str, list] = {l: [] for l in letters}
        for _ in range(n):
            sim = dict(letters)
            # Add noisy remaining votes
            rem_total = sum(remaining_letters.values())
            if rem_total > 100:
                for l, exp in remaining_letters.items():
                    noise = rng.gauss(0, math.sqrt(exp) * 0.15)
                    sim[l] = sim.get(l, 0) + max(0, exp + noise)
            # Add noise to counted votes (sampling uncertainty)
            for l, v in list(sim.items()):
                if v > 0:
                    noise = rng.gauss(0, math.sqrt(v) * 0.02)
                    sim[l] = max(0, v + noise)
            alloc = bader_ofer(sim, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
            for l in letters:
                seat_counts.setdefault(l, []).append(alloc["seats"].get(l, 0))
        base_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        results = []
        for l, counts in seat_counts.items():
            if not counts:
                continue
            counts_sorted = sorted(counts)
            p_info = self.cur.party(l)
            lo = counts_sorted[int(0.1 * len(counts_sorted))]
            hi = counts_sorted[int(0.9 * len(counts_sorted))]
            mean = sum(counts) / len(counts)
            results.append({
                "letter": l,
                "short_he": p_info["short_he"],
                "bloc": p_info["bloc"],
                "family": p_info["family"],
                "seats_now": base_alloc["seats"].get(l, 0),
                "mean": _r(mean, 2),
                "p10": lo, "p90": hi,
                "range": hi - lo,
                "pct": _r(letters.get(l, 0) / total_valid, 5),
            })
        results.sort(key=lambda x: -(x["seats_now"] or 0))
        return {"parties": results, "iterations": n, "frac_counted": _r(frac, 4)}

    def big_cities_deep(self, current: Optional[ResultFile]) -> dict:
        """Deep per-family breakdown of the 20 largest cities: share, swing, leader vs 2022."""
        if not current:
            return {"cities": []}
        cur_cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        fam_names = {k: v.get("he") or v["name"] for k, v in self.cfg["families"].items()}
        # Pick top-20 by baseline eligibility (real cities, not envelopes)
        big = sorted(
            [(code, bc) for code, bc in self.base_cities.items()
             if bc.eligible >= 40000 and code != ENVELOPES_CODE],
            key=lambda kv: -kv[1].eligible
        )[:20]
        results = []
        for code, bc in big:
            cc = cur_cities.get(code)
            if not cc or not cc.valid:
                results.append({"code": code, "name": bc.name, "eligible": bc.eligible, "reported": False})
                continue
            # Per-family share now vs. baseline
            fam_now = {f: cc.fam.get(f, 0) / cc.valid for f in cc.fam} if cc.valid else {}
            fam_base = {f: bc.fam.get(f, 0) / bc.valid for f in bc.fam} if bc.valid else {}
            all_fams = sorted(set(fam_now) | set(fam_base),
                              key=lambda f: -(fam_now.get(f, 0)))
            fam_breakdown = []
            for f in all_fams[:8]:
                now_v = fam_now.get(f, 0)
                base_v = fam_base.get(f, 0)
                fam_breakdown.append({
                    "family": f,
                    "name_he": fam_names.get(f, f),
                    "now": _r(now_v, 5),
                    "base": _r(base_v, 5),
                    "delta": _r(now_v - base_v, 5),
                })
            # Winner now vs. base
            winner_now = max(cc.letters, key=lambda l: cc.letters[l]) if cc.letters else None
            winner_base = max(bc.letters, key=lambda l: bc.letters[l]) if bc.letters else None
            pn = self.cur.party(winner_now) if winner_now else {}
            pb = self.base.party(winner_base) if winner_base else {}
            turnout_now = cc.voters / cc.eligible if cc.eligible else None
            turnout_base = bc.voters / bc.eligible if bc.eligible else None
            results.append({
                "code": code, "name": bc.name, "eligible": bc.eligible,
                "reported": True, "valid": cc.valid,
                "turnout": _r(turnout_now, 4),
                "turnout_base": _r(turnout_base, 4),
                "turnout_delta": _r((turnout_now - turnout_base) if (turnout_now is not None and turnout_base) else None, 4),
                "sector": self.sector.get(code, "jewish"),
                "winner_letter": winner_now, "winner_he": pn.get("short_he"),
                "winner_pct": _r(cc.letters.get(winner_now, 0) / cc.valid if cc.valid and winner_now else None, 4),
                "base_winner_letter": winner_base, "base_winner_he": pb.get("short_he"),
                "base_winner_pct": _r(bc.letters.get(winner_base, 0) / bc.valid if bc.valid and winner_base else None, 4),
                "flipped": (pn.get("family") != pb.get("family")) if pn and pb else False,
                "families": fam_breakdown,
                "bloc_n_now": _r(cc.bloc.get("N", 0) / cc.valid, 4) if cc.valid else None,
                "bloc_n_base": _r(bc.bloc.get("N", 0) / bc.valid, 4) if bc.valid else None,
                "bloc_n_delta": _r((cc.bloc.get("N", 0) / cc.valid - bc.bloc.get("N", 0) / bc.valid) if cc.valid and bc.valid else None, 4),
            })
        results.sort(key=lambda x: -x.get("eligible", 0))
        return {"cities": results, "fam_names": fam_names}

    # ── 1. coalition_paths ────────────────────────────────────────────────────
    def coalition_paths(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"blocs": [], "party_combos": [], "current_seats": {}, "majority": self.majority}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"blocs": [], "party_combos": [], "current_seats": {}, "majority": self.majority}
        alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        seats = alloc["seats"]
        # Per-bloc seats
        bloc_seats: dict[str, int] = {}
        for l, s in seats.items():
            b = self.cur.party(l)["bloc"]
            bloc_seats[b] = bloc_seats.get(b, 0) + s
        # All bloc subsets
        all_blocs = [b for b in bloc_seats if bloc_seats[b] > 0]
        bloc_combos = []
        for r in range(1, len(all_blocs) + 1):
            for combo in itertools.combinations(all_blocs, r):
                s = sum(bloc_seats.get(b, 0) for b in combo)
                bloc_combos.append({"combo": list(combo), "seats": s, "gap": s - self.majority,
                                    "over_majority": s >= self.majority})
        bloc_combos.sort(key=lambda x: abs(x["gap"]))
        # Party combos: top-N minimal coalitions
        above = [(l, s) for l, s in sorted(seats.items(), key=lambda kv: -kv[1]) if s > 0]
        party_combos = []
        for size in range(1, 6):
            for combo in itertools.combinations(above, size):
                s = sum(x[1] for x in combo)
                if s >= self.majority:
                    letters_c = [x[0] for x in combo]
                    blocs_set = list({self.cur.party(l)["bloc"] for l in letters_c})
                    party_combos.append({"parties": letters_c, "seats": s, "gap": s - self.majority,
                                         "blocs": "+".join(sorted(blocs_set))})
            if len(party_combos) >= 20:
                break
        party_combos.sort(key=lambda x: (len(x["parties"]), -x["seats"]))
        return {"blocs": bloc_combos[:16], "party_combos": party_combos[:12],
                "current_seats": {l: s for l, s in seats.items() if s > 0},
                "bloc_seats": bloc_seats, "majority": self.majority}

    # ── 2. bloc_internal_shift ────────────────────────────────────────────────
    def bloc_internal_shift(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"blocs": {}}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters_now = self._sum_dicts(c.letters for c in cities.values())
        total_now = sum(letters_now.values()) or 1
        total_base = sum(self.base_letters.values()) or 1
        blocs: dict[str, dict] = {}
        for l, v in letters_now.items():
            b = self.cur.party(l)["bloc"]
            blocs.setdefault(b, {"letters": {}})
            blocs[b]["letters"][l] = blocs[b]["letters"].get(l, 0) + v
        result = {}
        for b, bd in blocs.items():
            bloc_total_now = sum(bd["letters"].values()) or 1
            base_total = sum(self.base_letters.get(l, 0) for l in bd["letters"]) or 1
            parties = []
            for l, v in sorted(bd["letters"].items(), key=lambda kv: -kv[1]):
                base_v = self.base_letters.get(l, 0)
                intra_now = v / bloc_total_now
                intra_base = base_v / base_total if base_total else 0
                p = self.cur.party(l)
                parties.append({"letter": l, "short_he": p["short_he"], "family": p["family"],
                                 "intra_now": _r(intra_now, 4), "intra_base": _r(intra_base, 4),
                                 "delta": _r(intra_now - intra_base, 4)})
            result[b] = {"total_now": _r(bloc_total_now / total_now, 4),
                         "total_base": _r(sum(self.base_letters.get(l, 0) for l in bd["letters"]) / total_base, 4),
                         "parties": sorted(parties, key=lambda x: -(x["intra_now"] or 0))}
        return {"blocs": result}

    # ── 3. geographic_concentration ───────────────────────────────────────────
    def geographic_concentration(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"parties": []}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                  if c.valid and c.code != ENVELOPES_CODE}
        letters_total = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters_total.values()) or 1
        results = []
        for l, tot in sorted(letters_total.items(), key=lambda kv: -kv[1]):
            if tot / total_valid < self.threshold * 0.5:
                continue
            p = self.cur.party(l)
            city_shares = [(code, c.letters.get(l, 0) / tot) for code, c in cities.items() if c.valid and tot > 0]
            hhi = sum(s ** 2 for _, s in city_shares)
            city_shares.sort(key=lambda x: -x[1])
            top = [{"name": cities[code].name, "code": code, "share": _r(s, 4)}
                   for code, s in city_shares[:3] if s > 0]
            weak = [{"name": cities[code].name, "code": code, "share": _r(s, 4)}
                    for code, s in city_shares[-3:] if code in cities]
            profile = "concentrated" if hhi > 0.05 else "moderate" if hhi > 0.02 else "spread"
            results.append({"letter": l, "short_he": p["short_he"], "bloc": p["bloc"],
                             "hhi": _r(hhi, 5), "profile": profile,
                             "top_cities": top, "weak_cities": weak})
        return {"parties": results}

    # ── 4. seat_efficiency ────────────────────────────────────────────────────
    def seat_efficiency(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"parties": [], "national_avg": None}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"parties": [], "national_avg": None}
        alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        seats = alloc["seats"]
        national_avg = total_valid / self.seats
        results = []
        for l, s in sorted(seats.items(), key=lambda kv: -kv[1]):
            if s == 0:
                continue
            votes = letters.get(l, 0)
            p = self.cur.party(l)
            results.append({"letter": l, "short_he": p["short_he"], "bloc": p["bloc"], "family": p["family"],
                             "seats": s, "votes": votes, "pct": _r(votes / total_valid, 5),
                             "votes_per_seat": _r(votes / s if s else None, 0)})
        results.sort(key=lambda x: x["votes_per_seat"] or 0)
        for i, r in enumerate(results):
            r["efficiency_rank"] = i + 1
        return {"parties": results, "national_avg": _r(national_avg, 0)}

    # ── 5. cascade_effect ────────────────────────────────────────────────────
    def cascade_effect(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"scenarios": []}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values()) or 1
        base_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        base_seats = base_alloc["seats"]
        base_n = sum(v for l, v in base_seats.items() if self.cur.party(l)["bloc"] == "N")
        scenarios = []
        for l, v in sorted(letters.items(), key=lambda kv: -kv[1]):
            pct = v / total_valid
            if pct < 0.005 or pct > self.threshold + 0.03:
                continue
            direction = "above" if pct >= self.threshold else "below"
            tweaked = {k: vv for k, vv in letters.items() if k != l}
            alt = bader_ofer(tweaked, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
            alt_seats = alt["seats"]
            alt_n = sum(vv for ll, vv in alt_seats.items() if self.cur.party(ll)["bloc"] == "N")
            changes = []
            all_letters = set(list(base_seats.keys()) + list(alt_seats.keys()))
            for ll in all_letters:
                delta = alt_seats.get(ll, 0) - base_seats.get(ll, 0)
                if delta != 0:
                    pp = self.cur.party(ll)
                    changes.append({"letter": ll, "short_he": pp["short_he"], "bloc": pp["bloc"], "delta": delta})
            changes.sort(key=lambda x: -abs(x["delta"]))
            p = self.cur.party(l)
            scenarios.append({"letter": l, "short_he": p["short_he"], "bloc": p["bloc"],
                               "pct": _r(pct, 5), "direction": direction,
                               "seat_changes": changes, "n_seats_without": alt_n, "n_seats_with": base_n})
        return {"scenarios": scenarios}

    # ── 6. turnout_nowcast ────────────────────────────────────────────────────
    def turnout_nowcast(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"reported_turnout": None, "extrapolated_final": None}
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                      if c.valid and c.code != ENVELOPES_CODE}
        rep_elig = sum(c.eligible for c in cities_now.values())
        rep_voters = sum(c.voters for c in cities_now.values())
        rep_turnout = rep_voters / rep_elig if rep_elig else None
        # Unreported localities
        unr_elig = sum(bc.eligible for code, bc in self.base_cities.items()
                       if code not in cities_now and code != ENVELOPES_CODE)
        total_elig = rep_elig + unr_elig
        # Assume unreported will turn out at base_turnout * ratio of current/base for reported
        base_rep_turnout = 0.0
        base_rep_elig = 0
        for code, c in cities_now.items():
            bc = self.base_cities.get(code)
            if bc and bc.eligible and bc.voters:
                base_rep_turnout += (bc.voters / bc.eligible) * bc.eligible
                base_rep_elig += bc.eligible
        avg_base_rep = base_rep_turnout / base_rep_elig if base_rep_elig else 0.72
        ratio = (rep_turnout / avg_base_rep) if avg_base_rep and rep_turnout else 1.0
        # Expected unreported turnout
        unr_expected_voters = sum(
            bc.voters * ratio for code, bc in self.base_cities.items()
            if code not in cities_now and code != ENVELOPES_CODE and bc.voters
        )
        total_expected = rep_voters + unr_expected_voters
        final_pct = total_expected / total_elig if total_elig else None
        return {
            "reported_turnout": _r(rep_turnout, 4),
            "extrapolated_final": _r(final_pct, 4),
            "reported_eligible": rep_elig,
            "total_eligible": total_elig,
            "extrapolated_voters": _r(total_expected, 0),
            "pct_eligible_reported": _r(rep_elig / total_elig if total_elig else None, 4),
            "note": f"based on {len(cities_now)} localities reporting",
        }

    # ── 7. party_comovement ───────────────────────────────────────────────────
    def party_comovement(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"top_allied": [], "top_competing": []}
        cities = [c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                  if c.valid >= 200 and c.code != ENVELOPES_CODE]
        letters_total = self._sum_dicts(c.letters for c in cities)
        total_valid = sum(letters_total.values()) or 1
        top_letters = [l for l, v in sorted(letters_total.items(), key=lambda kv: -kv[1])
                       if v / total_valid >= self.threshold * 0.8][:10]
        # Build per-city share vectors
        vecs: dict[str, list] = {l: [] for l in top_letters}
        for city in cities:
            for l in top_letters:
                vecs[l].append(city.letters.get(l, 0) / city.valid if city.valid else 0)
        def pearson(a: list, b: list) -> float:
            n = len(a)
            if n < 5:
                return 0.0
            am, bm = sum(a) / n, sum(b) / n
            num = sum((ai - am) * (bi - bm) for ai, bi in zip(a, b))
            da = math.sqrt(sum((ai - am) ** 2 for ai in a))
            db = math.sqrt(sum((bi - bm) ** 2 for bi in b))
            return num / (da * db) if da * db else 0.0
        pairs = []
        for i, la in enumerate(top_letters):
            for lb in top_letters[i + 1:]:
                r = pearson(vecs[la], vecs[lb])
                pa, pb = self.cur.party(la), self.cur.party(lb)
                rel = "allied" if r > 0.3 else "competing" if r < -0.3 else "neutral"
                pairs.append({"a": la, "b": lb, "a_he": pa["short_he"], "b_he": pb["short_he"],
                               "r": _r(r, 3), "relationship": rel})
        pairs.sort(key=lambda x: -(x["r"] or 0))
        return {"top_allied": pairs[:5], "top_competing": list(reversed(pairs))[:5], "all_pairs": pairs}

    # ── 8. envelope_analysis ──────────────────────────────────────────────────
    def envelope_analysis(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"available": False}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        env = cities.get(ENVELOPES_CODE)
        if not env or not env.valid:
            return {"available": False, "total_valid": 0}
        letters_national = self._sum_dicts(c.letters for code, c in cities.items() if code != ENVELOPES_CODE)
        total_nat = sum(letters_national.values()) or 1
        total_env = env.valid
        parties = []
        for l, ev in sorted(env.letters.items(), key=lambda kv: -kv[1]):
            env_pct = ev / total_env
            nat_pct = letters_national.get(l, 0) / total_nat
            p = self.cur.party(l)
            parties.append({"letter": l, "short_he": p["short_he"], "bloc": p["bloc"],
                             "envelope_pct": _r(env_pct, 5), "national_pct": _r(nat_pct, 5),
                             "delta": _r(env_pct - nat_pct, 5), "overperforms": env_pct > nat_pct})
        parties.sort(key=lambda x: -(x["delta"] or 0))
        bloc_env: dict[str, float] = {}
        bloc_nat: dict[str, float] = {}
        for l, ev in env.letters.items():
            b = self.cur.party(l)["bloc"]
            bloc_env[b] = bloc_env.get(b, 0) + ev / total_env
        for l, nv in letters_national.items():
            b = self.cur.party(l)["bloc"]
            bloc_nat[b] = bloc_nat.get(b, 0) + nv / total_nat
        return {"available": True, "total_valid": total_env,
                "parties": parties, "bloc_envelope": {b: _r(v, 4) for b, v in bloc_env.items()},
                "bloc_national": {b: _r(v, 4) for b, v in bloc_nat.items()}}

    # ── 9. last_seat_race ─────────────────────────────────────────────────────
    def last_seat_race(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"races": [], "total_valid": 0}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"races": [], "total_valid": 0}
        base_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        base_seats = base_alloc["seats"]
        races = []
        checked = set()
        step = max(1, int(total_valid * 0.0002))
        for l in sorted(base_seats, key=lambda k: -base_seats[k]):
            if l in checked or base_seats[l] == 0:
                continue
            # Find votes needed for this party to gain a seat
            add = step
            while add <= total_valid * 0.02:
                up = dict(letters)
                up[l] = letters.get(l, 0) + add
                up_alloc = bader_ofer(up, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
                if up_alloc["seats"].get(l, 0) > base_seats.get(l, 0):
                    # Find who lost
                    loser = min(
                        (ll for ll in base_seats if up_alloc["seats"].get(ll, 0) < base_seats[ll]),
                        key=lambda ll: base_seats[ll], default=None)
                    if loser:
                        pl = self.cur.party(l)
                        plo = self.cur.party(loser)
                        races.append({
                            "leader_letter": l, "leader_he": pl["short_he"], "leader_seats": base_seats[l],
                            "challenger_letter": loser, "challenger_he": plo["short_he"],
                            "challenger_seats": base_seats[loser],
                            "votes_needed": add, "votes_as_pct": _r(add / total_valid, 5)})
                        checked.add(loser)
                    break
                add += step
            if len(races) >= 6:
                break
        return {"races": races, "total_valid": total_valid}

    # ── 10. swing_decomposition ───────────────────────────────────────────────
    def swing_decomposition(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"total_swing": None, "turnout_effect": None, "preference_effect": None, "sectors": []}
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                      if c.valid and c.code != ENVELOPES_CODE}
        if not cities_now:
            return {"total_swing": None, "turnout_effect": None, "preference_effect": None, "sectors": []}
        # Actual N share now (LFL)
        now_n = sum(c.bloc.get("N", 0) for c in cities_now.values())
        now_total = sum(c.valid for c in cities_now.values()) or 1
        actual_n_share = now_n / now_total
        # Counterfactual A: same vote shares as 2022 (baseline), but current turnout pattern
        cf_a_n = 0.0
        cf_a_total = 0.0
        for code, c in cities_now.items():
            bc = self.base_cities.get(code)
            if not bc or not bc.valid:
                continue
            base_n_share = bc.bloc.get("N", 0) / bc.valid
            cf_a_n += base_n_share * c.valid
            cf_a_total += c.valid
        cf_a_share = cf_a_n / cf_a_total if cf_a_total else actual_n_share
        # Counterfactual B: same turnout as 2022, current vote shares
        cf_b_n = 0.0
        cf_b_total = 0.0
        for code, c in cities_now.items():
            bc = self.base_cities.get(code)
            if not bc or not bc.eligible:
                continue
            base_turnout = bc.voters / bc.eligible if bc.eligible else 0.72
            expected_voters = c.eligible * base_turnout if c.eligible else c.valid
            n_share_now = c.bloc.get("N", 0) / c.valid if c.valid else 0
            cf_b_n += n_share_now * expected_voters
            cf_b_total += expected_voters
        cf_b_share = cf_b_n / cf_b_total if cf_b_total else actual_n_share
        # LFL baseline (what 2022 looks like for reported cities)
        base_n = sum(self.base_cities[code].bloc.get("N", 0) for code in cities_now if code in self.base_cities)
        base_tot = sum(self.base_cities[code].valid for code in cities_now if code in self.base_cities) or 1
        lfl_base = base_n / base_tot
        total_swing = _r(actual_n_share - lfl_base, 4)
        turnout_effect = _r(cf_a_share - lfl_base, 4)
        preference_effect = _r(actual_n_share - cf_a_share, 4)
        # By sector
        sector_map: dict[str, dict] = {}
        for code, c in cities_now.items():
            sec = self.sector.get(code, "jewish")
            bc = self.base_cities.get(code)
            if not bc or not bc.valid:
                continue
            sd = sector_map.setdefault(sec, {"n_now": 0, "total_now": 0, "base_n": 0, "base_total": 0,
                                             "cf_a_n": 0, "cf_a_total": 0})
            sd["n_now"] += c.bloc.get("N", 0)
            sd["total_now"] += c.valid
            sd["base_n"] += bc.bloc.get("N", 0)
            sd["base_total"] += bc.valid
            base_n_share = bc.bloc.get("N", 0) / bc.valid if bc.valid else 0
            sd["cf_a_n"] += base_n_share * c.valid
            sd["cf_a_total"] += c.valid
        sectors = []
        for sec, sd in sector_map.items():
            an = sd["n_now"] / sd["total_now"] if sd["total_now"] else None
            bn = sd["base_n"] / sd["base_total"] if sd["base_total"] else None
            ca = sd["cf_a_n"] / sd["cf_a_total"] if sd["cf_a_total"] else None
            if an is None or bn is None:
                continue
            sectors.append({"sector": sec, "name_he": SECTOR_HE.get(sec, sec),
                             "total": _r(an - bn, 4),
                             "turnout": _r((ca - bn) if ca is not None else None, 4),
                             "preference": _r((an - ca) if ca is not None else None, 4)})
        return {"total_swing": total_swing, "turnout_effect": turnout_effect,
                "preference_effect": preference_effect, "sectors": sectors}

    # ── 11. new_voter_cities ──────────────────────────────────────────────────
    def new_voter_cities(self, current: Optional[ResultFile]) -> dict:
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid} \
            if current else {}
        rows = []
        for code, bc in self.base_cities.items():
            if code == ENVELOPES_CODE:
                continue
            cc = cities_now.get(code)
            # Use current eligibility from the result file if available, else estimate
            elig_now = cc.eligible if cc else int(bc.eligible * self.growth)
            growth = elig_now - bc.eligible
            growth_pct = growth / bc.eligible if bc.eligible else 0
            n_share = _r(cc.bloc.get("N", 0) / cc.valid, 4) if cc and cc.valid else None
            o_share = _r(cc.bloc.get("O", 0) / cc.valid, 4) if cc and cc.valid else None
            rows.append({"code": code, "name": bc.name, "eligible_now": elig_now,
                         "eligible_base": bc.eligible, "growth": growth,
                         "growth_pct": _r(growth_pct, 4), "reported": cc is not None,
                         "n_share": n_share, "o_share": o_share,
                         "sector": self.sector.get(code, "jewish")})
        rows.sort(key=lambda x: -(x["growth"] or 0))
        return {"cities": rows[:25]}

    # ── 12. reporting_pace ────────────────────────────────────────────────────
    def reporting_pace(self, current: Optional[ResultFile]) -> dict:
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid} \
            if current else {}
        sector_stats: dict[str, dict] = {}
        for sec in ["arab", "haredi", "large", "jewish", "envelopes"]:
            sector_stats[sec] = {"rep": 0, "total": 0, "elig_rep": 0, "elig_total": 0, "voters": 0}
        for code, bc in self.base_cities.items():
            sec = self.sector.get(code, "jewish")
            if sec not in sector_stats:
                continue
            sd = sector_stats[sec]
            sd["total"] += 1
            sd["elig_total"] += bc.eligible
            cc = cities_now.get(code)
            if cc:
                sd["rep"] += 1
                sd["elig_rep"] += cc.eligible or bc.eligible
                sd["voters"] += cc.voters
        results = []
        for sec, sd in sector_stats.items():
            results.append({
                "sector": sec, "name_he": SECTOR_HE.get(sec, sec),
                "cities_reported": sd["rep"], "cities_total": sd["total"],
                "eligible_reported": sd["elig_rep"], "eligible_total": sd["elig_total"],
                "pct_cities": _r(sd["rep"] / sd["total"] if sd["total"] else None, 4),
                "pct_eligible": _r(sd["elig_rep"] / sd["elig_total"] if sd["elig_total"] else None, 4),
                "turnout_so_far": _r(sd["voters"] / sd["elig_rep"] if sd["elig_rep"] else None, 4),
            })
        return {"sectors": results}

    # ── 13. threshold_sensitivity ─────────────────────────────────────────────
    def threshold_sensitivity(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"thresholds": []}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"thresholds": []}
        baseline_alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        baseline_seats = baseline_alloc["seats"]
        thresholds_to_test = [(0.02, "2%"), (0.025, "2.5%"), (0.03, "3%"),
                              (0.0325, "3.25% (נוכחי)"), (0.035, "3.5%"), (0.04, "4%")]
        results = []
        for thr, label in thresholds_to_test:
            alloc = bader_ofer(letters, self.cur.cfg.get("agreements", []), self.seats, thr)
            seats = alloc["seats"]
            n_seats = sum(v for l, v in seats.items() if self.cur.party(l)["bloc"] == "N")
            o_seats = sum(v for l, v in seats.items() if self.cur.party(l)["bloc"] == "O")
            qualifying = sum(1 for l, v in letters.items() if v / total_valid >= thr)
            changes = []
            for l in set(list(baseline_seats.keys()) + list(seats.keys())):
                delta = seats.get(l, 0) - baseline_seats.get(l, 0)
                if delta != 0:
                    p = self.cur.party(l)
                    changes.append({"letter": l, "short_he": p["short_he"], "seats": seats.get(l, 0), "delta": delta})
            changes.sort(key=lambda x: -abs(x["delta"]))
            results.append({"pct": thr, "label": label, "parties_qualifying": qualifying,
                             "n_seats": n_seats, "o_seats": o_seats, "seat_changes": changes[:8]})
        return {"thresholds": results}

    # ── 14. family_flow_estimate ──────────────────────────────────────────────
    def family_flow_estimate(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"gains": [], "losses": [], "flows": []}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        fam_now = self._sum_dicts(c.fam for c in cities.values())
        fam_base = dict(self.base_fam)
        # Scale base to same total as now
        total_now = sum(fam_now.values()) or 1
        total_base = sum(fam_base.values()) or 1
        scale = total_now / total_base
        all_fams = set(list(fam_now.keys()) + list(fam_base.keys()))
        net: dict[str, int] = {}
        for f in all_fams:
            scaled_base = int(fam_base.get(f, 0) * scale)
            net[f] = fam_now.get(f, 0) - scaled_base
        gains = sorted([{"family": f, "name_he": self.cfg["families"].get(f, {}).get("he", f),
                         "net": net[f]} for f in net if net[f] > 0], key=lambda x: -x["net"])
        losses = sorted([{"family": f, "name_he": self.cfg["families"].get(f, {}).get("he", f),
                          "net": net[f]} for f in net if net[f] < 0], key=lambda x: x["net"])
        # Estimate flows: each losing family distributes proportionally to gaining families
        total_gain = sum(g["net"] for g in gains) or 1
        flows = []
        for loss in losses[:5]:
            for gain in gains[:5]:
                est = abs(loss["net"]) * gain["net"] / total_gain
                if est > total_now * 0.002:
                    flows.append({"from": loss["family"], "from_he": loss["name_he"],
                                  "to": gain["family"], "to_he": gain["name_he"],
                                  "estimated_votes": _r(est, 0)})
        flows.sort(key=lambda x: -(x["estimated_votes"] or 0))
        return {"gains": gains[:6], "losses": losses[:6], "flows": flows[:8]}

    # ── 15. bellwether_ranking ────────────────────────────────────────────────
    def bellwether_ranking(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"national_n_share": None, "cities": []}
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                      if c.valid and c.code != ENVELOPES_CODE}
        total_n = sum(c.bloc.get("N", 0) for c in cities_now.values())
        total_v = sum(c.valid for c in cities_now.values()) or 1
        nat_n_share = total_n / total_v
        cities_out = []
        for code, c in cities_now.items():
            if c.valid < 500:
                continue
            bc = self.base_cities.get(code)
            local_n = c.bloc.get("N", 0) / c.valid
            dist = abs(local_n - nat_n_share)
            # Historical bellwether: how close was 2022 local N-share to 2022 national N-share
            base_nat_n = sum(self.base_cities[cc].bloc.get("N", 0) for cc in self.base_cities) / \
                         max(1, sum(self.base_cities[cc].valid for cc in self.base_cities))
            hist_dist = abs((bc.bloc.get("N", 0) / bc.valid if bc and bc.valid else 0.5) - base_nat_n) if bc else None
            cities_out.append({"code": code, "name": c.name,
                                "n_share": _r(local_n, 4), "distance": _r(dist, 4),
                                "hist_distance": _r(hist_dist, 4),
                                "eligible": bc.eligible if bc else c.eligible,
                                "sector": self.sector.get(code, "jewish")})
        cities_out.sort(key=lambda x: x["distance"])
        for i, c in enumerate(cities_out):
            c["rank"] = i + 1
        return {"national_n_share": _r(nat_n_share, 4), "cities": cities_out[:30]}

    # ── 16. party_extremes ────────────────────────────────────────────────────
    def party_extremes(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"parties": []}
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values())
                      if c.valid >= 500 and c.code != ENVELOPES_CODE}
        letters_total = self._sum_dicts(c.letters for c in cities_now.values())
        total_valid = sum(letters_total.values()) or 1
        results = []
        for l, tot in sorted(letters_total.items(), key=lambda kv: -kv[1]):
            if tot / total_valid < 0.03:
                continue
            p = self.cur.party(l)
            city_shares = []
            for code, c in cities_now.items():
                if not c.valid:
                    continue
                share = c.letters.get(l, 0) / c.valid
                bc = self.base_cities.get(code)
                base_share = (bc.letters.get(l, 0) / bc.valid if bc and bc.valid else None)
                city_shares.append({"code": code, "name": c.name, "share": share, "base_share": base_share,
                                    "sector": self.sector.get(code, "jewish")})
            city_shares.sort(key=lambda x: -x["share"])
            strongest = [{"code": c["code"], "name": c["name"], "share": _r(c["share"], 4),
                          "base_share": _r(c["base_share"], 4), "sector": c["sector"]}
                         for c in city_shares[:5]]
            weakest = [{"code": c["code"], "name": c["name"], "share": _r(c["share"], 4),
                        "base_share": _r(c["base_share"], 4), "sector": c["sector"]}
                       for c in city_shares[-5:] if c["share"] > 0]
            results.append({"letter": l, "short_he": p["short_he"], "bloc": p["bloc"],
                             "pct": _r(tot / total_valid, 5), "strongest": strongest, "weakest": weakest})
        return {"parties": results}

    # ── 17. sector_deep_compare ───────────────────────────────────────────────
    def sector_deep_compare(self, current: Optional[ResultFile]) -> dict:
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid} \
            if current else {}
        sector_data: dict[str, dict] = {s: {"cities_total": 0, "cities_rep": 0,
                                             "elig": 0, "voters": 0, "valid": 0,
                                             "base_elig": 0, "base_voters": 0, "base_valid": 0,
                                             "letters_now": {}, "letters_base": {}}
                                        for s in ["arab", "haredi", "large", "jewish", "envelopes"]}
        for code, bc in self.base_cities.items():
            sec = self.sector.get(code, "jewish")
            if sec not in sector_data:
                continue
            sd = sector_data[sec]
            sd["cities_total"] += 1
            sd["base_elig"] += bc.eligible
            sd["base_voters"] += bc.voters
            sd["base_valid"] += bc.valid
            for l, v in bc.letters.items():
                sd["letters_base"][l] = sd["letters_base"].get(l, 0) + v
            cc = cities_now.get(code)
            if cc:
                sd["cities_rep"] += 1
                sd["elig"] += cc.eligible or bc.eligible
                sd["voters"] += cc.voters
                sd["valid"] += cc.valid
                for l, v in cc.letters.items():
                    sd["letters_now"][l] = sd["letters_now"].get(l, 0) + v
        results = []
        for sec, sd in sector_data.items():
            tot_now = sd["valid"] or 1
            tot_base = sd["base_valid"] or 1
            blocs: dict[str, dict] = {}
            for l, v in sd["letters_now"].items():
                b = self.cur.party(l)["bloc"]
                blocs.setdefault(b, {"now_v": 0, "base_v": 0})["now_v"] += v
            for l, v in sd["letters_base"].items():
                b = self.cur.party(l)["bloc"]
                blocs.setdefault(b, {"now_v": 0, "base_v": 0})["base_v"] += v
            bloc_out = {b: {"now": _r(bd["now_v"] / tot_now, 4), "base": _r(bd["base_v"] / tot_base, 4)}
                        for b, bd in blocs.items()}
            top_l_now = max(sd["letters_now"], key=lambda l: sd["letters_now"][l]) if sd["letters_now"] else None
            top_l_base = max(sd["letters_base"], key=lambda l: sd["letters_base"][l]) if sd["letters_base"] else None
            results.append({
                "sector": sec, "name_he": SECTOR_HE.get(sec, sec),
                "eligible": sd["elig"] or sd["base_elig"],
                "voters": sd["voters"], "turnout": _r(sd["voters"] / sd["elig"] if sd["elig"] else None, 4),
                "base_turnout": _r(sd["base_voters"] / sd["base_elig"] if sd["base_elig"] else None, 4),
                "blocs": bloc_out,
                "top_party_now": top_l_now,
                "top_party_now_he": self.cur.party(top_l_now)["short_he"] if top_l_now else None,
                "top_party_base": top_l_base,
                "top_party_base_he": self.cur.party(top_l_base)["short_he"] if top_l_base else None,
                "cities_total": sd["cities_total"], "cities_reported": sd["cities_rep"],
            })
        return {"sectors": results}

    # ── 18. projection_accuracy_track ────────────────────────────────────────
    def projection_accuracy_track(self, history: list) -> dict:
        snaps = [h for h in history if h and h.get("parties") and h.get("projection")]
        if len(snaps) < 2:
            return {"datapoints": [], "trend": "stable", "current_proj_n": None, "current_actual_n": None}
        # For each snapshot, get its projection and the actual seats at that time
        datapoints = []
        for h in snaps[-20:]:
            proj_n = sum(p.get("proj_seats", 0) or 0 for p in h["parties"]
                         if h.get("projection") and self.cur.party(p["letter"])["bloc"] == "N")
            actual_n = sum(p.get("seats", 0) or 0 for p in h["parties"]
                           if self.cur.party(p["letter"])["bloc"] == "N")
            datapoints.append({"t": h.get("meta", {}).get("analyzed_at", ""),
                                "projected_n": proj_n, "actual_n_then": actual_n,
                                "error": abs(proj_n - actual_n)})
        # Trend: is the error decreasing?
        errors = [d["error"] for d in datapoints]
        if len(errors) >= 4:
            early = sum(errors[:len(errors) // 2]) / (len(errors) // 2)
            late = sum(errors[len(errors) // 2:]) / (len(errors) - len(errors) // 2)
            trend = "converging" if late < early - 0.5 else "diverging" if late > early + 0.5 else "stable"
        else:
            trend = "stable"
        last = snaps[-1]
        curr_proj = sum(p.get("proj_seats", 0) or 0 for p in last["parties"]
                        if self.cur.party(p["letter"])["bloc"] == "N")
        curr_actual = sum(p.get("seats", 0) or 0 for p in last["parties"]
                          if self.cur.party(p["letter"])["bloc"] == "N")
        return {"datapoints": datapoints, "trend": trend,
                "current_proj_n": curr_proj, "current_actual_n": curr_actual}

    # ── 19. surplus_optimizer ─────────────────────────────────────────────────
    def surplus_optimizer(self, current: Optional[ResultFile]) -> dict:
        if not current:
            return {"n_bloc_best": None, "o_bloc_best": None}
        cities = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters = self._sum_dicts(c.letters for c in cities.values())
        total_valid = sum(letters.values())
        if not total_valid:
            return {"n_bloc_best": None, "o_bloc_best": None}
        base_alloc = bader_ofer(letters, [], self.seats, self.threshold)
        base_seats = base_alloc["seats"]
        def bloc_seats(seats_d: dict, bloc: str) -> int:
            return sum(v for l, v in seats_d.items() if self.cur.party(l)["bloc"] == bloc)
        base_n = bloc_seats(base_seats, "N")
        base_o = bloc_seats(base_seats, "O")
        def try_agreement(party_a: str, party_b: str) -> int:
            alloc = bader_ofer(letters, [[party_a, party_b]], self.seats, self.threshold)
            return alloc["seats"].get(party_a, 0) + alloc["seats"].get(party_b, 0)
        results = {"n_bloc_best": None, "o_bloc_best": None, "all_n_pairs": [], "all_o_pairs": []}
        for bloc_key, bloc_label, base_b in [("N", "n_bloc", base_n), ("O", "o_bloc", base_o)]:
            bloc_letters = sorted([l for l in letters if self.cur.party(l)["bloc"] == bloc_key
                                    and letters[l] / total_valid >= self.threshold * 0.7],
                                   key=lambda l: -letters.get(l, 0))
            best = {"gain": 0, "parties": [], "seats_without": base_b, "seats_with": base_b}
            pairs_out = []
            for pa, pb in itertools.combinations(bloc_letters, 2):
                combined_base = base_seats.get(pa, 0) + base_seats.get(pb, 0)
                combined_with = try_agreement(pa, pb)
                gain = combined_with - combined_base
                # Also recompute bloc total
                alloc2 = bader_ofer(letters, [[pa, pb]], self.seats, self.threshold)
                new_b = bloc_seats(alloc2["seats"], bloc_key)
                ppa, ppb = self.cur.party(pa), self.cur.party(pb)
                pairs_out.append({"parties": [pa, pb], "parties_he": [ppa["short_he"], ppb["short_he"]],
                                   "gain": gain, "seats_with": new_b,
                                   "seats_without": base_b, "bloc_gain": new_b - base_b})
                if new_b - base_b > best["gain"]:
                    best = {"parties": [pa, pb], "parties_he": [ppa["short_he"], ppb["short_he"]],
                            "gain": new_b - base_b, "seats_with": new_b, "seats_without": base_b}
            pairs_out.sort(key=lambda x: -x.get("bloc_gain", 0))
            results[f"{bloc_label}_best"] = best if best["gain"] > 0 else None
            results[f"all_{bloc_label}_pairs"] = pairs_out[:8]
        return results

    # ── 20. live_insights_extended ────────────────────────────────────────────
    def live_insights_extended(self, current: Optional[ResultFile], history: list) -> dict:
        insights = []
        def add(category: str, importance: str, text: str) -> None:
            insights.append({"text": text, "category": category, "importance": importance})
        if not current:
            add("data", "high", "הנתונים עדיין לא זמינים — ממתין לנתוני הבחירות.")
            return {"insights": insights}
        cities_now = {c.code: c for c in (CityAgg(r, self.cur) for r in current.by_city().values()) if c.valid}
        letters_now = self._sum_dicts(c.letters for c in cities_now.values())
        total_now = sum(letters_now.values()) or 1
        # Turnout vs 2022
        base_elig = sum(bc.eligible for bc in self.base_cities.values() if bc.code != ENVELOPES_CODE)
        rep_cities = {c: v for c, v in cities_now.items() if c != ENVELOPES_CODE}
        rep_voters = sum(c.voters for c in rep_cities.values())
        rep_elig = sum(v.eligible or self.base_cities[c].eligible
                       for c, v in rep_cities.items() if c in self.base_cities) or 1
        base_rep_voters = sum(self.base_cities[c].voters for c in rep_cities if c in self.base_cities)
        base_rep_elig = sum(self.base_cities[c].eligible for c in rep_cities if c in self.base_cities) or 1
        curr_to = rep_voters / rep_elig
        base_to = base_rep_voters / base_rep_elig
        delta_to = curr_to - base_to
        if abs(delta_to) > 0.005:
            add("turnout", "high", f"אחוז ההצבעה ביישובים שדיווחו: {curr_to*100:.1f}% "
                f"({'גבוה' if delta_to > 0 else 'נמוך'} ב-{abs(delta_to)*100:.1f} נק׳ מ-2022)")
        # N-bloc status
        n_now = sum(c.bloc.get("N", 0) for c in cities_now.values())
        n_share = n_now / total_now
        base_n = sum(self.base_cities[c].bloc.get("N", 0) for c in cities_now if c in self.base_cities)
        base_tot_rep = sum(self.base_cities[c].valid for c in cities_now if c in self.base_cities) or 1
        base_n_share = base_n / base_tot_rep
        swing = n_share - base_n_share
        alloc = bader_ofer(letters_now, self.cur.cfg.get("agreements", []), self.seats, self.threshold)
        n_seats = sum(v for l, v in alloc["seats"].items() if self.cur.party(l)["bloc"] == "N")
        base_n_seats_full = sum(v for l, v in self.base_letters.items() if self.cur.party(l)["bloc"] == "N")
        if alloc["seats"]:
            add("coalition", "high", f"גוש נתניהו: {n_seats} מנדטים לפי הספירה הנוכחית "
                f"({'מעל' if n_seats >= self.majority else 'מתחת ל'}-{self.majority} הנדרשים)")
        if abs(swing) > 0.005:
            add("swing", "high", f"גוש נתניהו: {'+' if swing > 0 else ''}{swing*100:.1f} נק׳ ביחס ל-2022 "
                f"ביישובים שדיווחו")
        # Largest cities status
        big = sorted([(code, bc) for code, bc in self.base_cities.items()
                      if bc.eligible >= 100000 and code != ENVELOPES_CODE],
                     key=lambda kv: -kv[1].eligible)[:5]
        rep_big = [(code, bc) for code, bc in big if code in cities_now]
        if len(rep_big) < len(big):
            names = ", ".join(bc.name for code, bc in big if code not in cities_now)
            add("cities", "high", f"הערים הגדולות שטרם דיווחו: {names}")
        # Party momentum
        snaps = [h for h in history if h and h.get("parties")]
        if len(snaps) >= 4:
            recent = snaps[-4:]
            for l in sorted(letters_now, key=lambda k: -letters_now.get(k, 0))[:8]:
                pts = [next((p["pct"] for p in h["parties"] if p["letter"] == l), None) for h in recent]
                pts = [v for v in pts if v is not None]
                if len(pts) >= 3:
                    slope = pts[-1] - pts[0]
                    p = self.cur.party(l)
                    if abs(slope) > 0.004:
                        add("momentum", "medium", f"{p['short_he']}: {'עולה' if slope > 0 else 'יורדת'} "
                            f"ב-{abs(slope)*100:.2f} נק׳ ב-4 העדכונים האחרונים")
        # Threshold watch
        near = [(l, v / total_now) for l, v in letters_now.items()
                if abs(v / total_now - self.threshold) < 0.008 and v / total_now > 0.01]
        for l, pct in sorted(near, key=lambda x: abs(x[1] - self.threshold)):
            p = self.cur.party(l)
            above = pct >= self.threshold
            add("threshold", "high" if abs(pct - self.threshold) < 0.004 else "medium",
                f"{p['short_he']} {'מעל' if above else 'מתחת ל'}אחוז החסימה: {pct*100:.2f}% "
                f"({'חסרים' if above else 'עודף'} {abs(pct - self.threshold)*100:.2f} נק׳)")
        # Sector outlier
        for sec in ["arab", "haredi"]:
            sec_cities = [c for code, c in cities_now.items() if self.sector.get(code) == sec]
            if sec_cities:
                sec_n = sum(c.bloc.get("N", 0) for c in sec_cities)
                sec_v = sum(c.valid for c in sec_cities) or 1
                sec_base_n = sum(self.base_cities[code].bloc.get("N", 0)
                                 for code, c in cities_now.items()
                                 if self.sector.get(code) == sec and code in self.base_cities)
                sec_base_v = sum(self.base_cities[code].valid
                                 for code, c in cities_now.items()
                                 if self.sector.get(code) == sec and code in self.base_cities) or 1
                s = sec_n / sec_v - sec_base_n / sec_base_v
                if abs(s) > 0.01:
                    add("sector", "medium", f"מגזר {SECTOR_HE.get(sec, sec)}: גוש נ׳ "
                        f"{'+' if s > 0 else ''}{s*100:.1f} נק׳ ביחס ל-2022")
        # Flipped city
        flipped = [(code, c) for code, c in cities_now.items()
                   if code in self.base_cities and c.valid >= 2000 and code != ENVELOPES_CODE]
        flip_count = 0
        for code, c in flipped:
            bc = self.base_cities[code]
            win_now = max(c.letters, key=lambda l: c.letters[l]) if c.letters else None
            win_base = max(bc.letters, key=lambda l: bc.letters[l]) if bc.letters else None
            if win_now and win_base:
                if self.cur.party(win_now)["family"] != self.cur.party(win_base)["family"]:
                    flip_count += 1
        if flip_count > 0:
            add("cities", "medium", f"{flip_count} יישובים החליפו מפלגה מובילה ביחס ל-2022")
        return {"insights": insights[:15]}
