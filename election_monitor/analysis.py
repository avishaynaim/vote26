"""Analysis of a (possibly partial) result file against a baseline election.

Key ideas
---------
* **Locality matching** is done on the locality code (סמל ישוב), which is stable
  between elections. The double-envelopes row (code 9999) is matched too.
* **Families** make lists comparable across elections (e.g. 2022 Labor + Meretz
  vs. 2026 The Democrats). **Blocs** are the coarse political camps.
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
        he = self.names.get(letter) or letter
        p = self.cfg.get("parties", {}).get(letter)
        if p:
            out = {"letter": letter, "he": he, "name": p.get("short") or he, "bloc": p["bloc"], "family": p["family"]}
        elif (self.inherit is not None and self.cfg.get("inherit_by_letter")
              and letter in self.inherit.cfg.get("parties", {})):
            q = self.inherit.cfg["parties"][letter]
            out = {"letter": letter, "he": he, "name": he if he != letter else f"List {letter}",
                   "bloc": q["bloc"], "family": q["family"], "assumed": True}
        else:
            out = {"letter": letter, "he": he, "name": he if he != letter else f"List {letter}",
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
        if baseline_ballots is not None:
            for r in baseline_ballots.rows:
                self.base_ballots[r.code] = self.base_ballots.get(r.code, 0) + 1
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
        official = {l["letter"]: l["seats"] for l in (baseline_national or {}).get("lists", []) if l.get("seats") is not None}
        if official and sum(official.values()) == self.seats:
            self.base_seats, self.base_seats_source = official, "official (results site)"
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
                "letter": l, "name": p["name"], "he": p.get("he", l), "leader": p.get("leader", ""),
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

        # "other" family is not comparable letter by letter -> spread by current split as well
        def base_est(b: CityAgg) -> dict:
            return {l: b.share(b.fam, self.cur.family(l)) * split(l) for l in cur_letters}

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
            "now": c and {"eligible": c.eligible, "voters": c.voters, "invalid": c.invalid, "valid": c.valid,
                          "letters": [{**self.cur.party(l), "votes": v, "pct": _r(c.share(c.letters, l))}
                                      for l, v in sorted(c.letters.items(), key=lambda kv: -kv[1])]},
            "base": b and {"eligible": b.eligible, "voters": b.voters, "invalid": b.invalid, "valid": b.valid,
                           "turnout": _r(_pct(b.voters, b.eligible)),
                           "letters": [{**self.base.party(l), "votes": v, "pct": _r(b.share(b.letters, l))}
                                       for l, v in sorted(b.letters.items(), key=lambda kv: -kv[1])[:20]]},
            "families": fams,
        }

    # ------------------------------------------------------------------ insights
    def _insights(self, st: dict) -> list:
        ins = []
        pg, nat = st["progress"], st["national"]
        fam_name = {k: v["name"] for k, v in self.cfg["families"].items()}

        def iso(t):  # isolate Hebrew names so they don't scramble English sentences
            return f"\u2068{t}\u2069"

        def add(kind, level, text):
            ins.append({"kind": kind, "level": level, "text": text})

        if not nat["valid"]:
            add("status", "info", "No results published yet. The monitor keeps polling the configured sources.")
            return ins
        frac = pg.get("fraction") or 0
        add("status", "info",
            f"Counting progress ≈ {frac*100:.1f}% ({pg['method']}); {pg['localities_reporting']} of "
            f"{pg['localities_total']} localities reporting; double envelopes "
            f"{'counted' if pg['envelopes_counted'] else 'not yet counted'}.")
        if nat.get("turnout_lfl_delta") is not None:
            d = nat["turnout_lfl_delta"] * 100
            add("turnout", "up" if d >= 0.1 else "down" if d <= -0.1 else "info",
                f"Turnout in reporting localities is {nat['turnout_lfl']*100:.1f}% vs {nat['turnout_lfl_base']*100:.1f}% "
                f"in the same localities last time ({d:+.1f} pp).")
        for s in st["sectors"]:
            if s.get("turnout_delta") is not None and s["now_valid"] > 2000:
                d = s["turnout_delta"] * 100
                if abs(d) >= 2:
                    add("sector", "up" if d > 0 else "down",
                        f"{s['name']}: turnout {s['turnout']*100:.1f}% vs {s['turnout_base']*100:.1f}% ({d:+.1f} pp), "
                        f"{s['reporting']}/{s['localities']} localities in.")
        # blocs
        bl = {b["key"]: b for b in st["blocs"]}
        n = bl.get("N")
        if n:
            ps = n.get("proj_seats")
            add("seats", "key",
                f"{n['name']}: {n['seats']} seats on counted votes"
                + (f", {ps} projected" if ps is not None else "")
                + f" (baseline {n['base_seats']}; majority = {self.majority}).")
        for b in st["blocs"]:
            if b.get("swing") is not None and abs(b["swing"]) >= 0.01:
                add("swing", "up" if b["swing"] > 0 else "down",
                    f"{b['name']} {b['swing']*100:+.1f} pp vs the same localities in the baseline "
                    f"({b['pct']*100:.1f}% now).")
        fams = [f for f in st["families"] if f.get("swing") is not None]
        if fams and max(abs(f["swing"]) for f in fams) >= 0.001:
            g = max(fams, key=lambda f: f["swing"])
            l = min(fams, key=lambda f: f["swing"])
            add("swing", "up", f"Biggest gainer: {g['name']} {g['swing']*100:+.1f} pp like-for-like.")
            add("swing", "down", f"Biggest loser: {l['name']} {l['swing']*100:+.1f} pp like-for-like.")
        # threshold
        for p in st["parties"]:
            if p["pct"] is None:
                continue
            if 0.025 <= p["pct"] <= 0.04:
                if p["passes"]:
                    add("threshold", "warn", f"{p['name']} ({iso(p['letter'])}) is just above the threshold: {p['pct']*100:.2f}%.")
                else:
                    add("threshold", "warn", f"{p['name']} ({iso(p['letter'])}) is below the threshold at {p['pct']*100:.2f}%, "
                                             f"{p['to_threshold']:,} votes short.")
        if st["agreements"]["void"]:
            add("seats", "info", "Surplus agreements void (a partner below threshold): "
                + ", ".join(iso("–".join(p)) for p in st["agreements"]["void"]))
        # cities
        solid = [c for c in st["cities"] if c.get("valid") and (c.get("base_eligible") or 0) >= 5000
                 and c.get("reporting", 0) >= 0.8 and c["sector"] != "envelopes"]
        tc = [c for c in solid if c.get("turnout_delta") is not None]
        up = sorted((c for c in tc if c["turnout_delta"] >= 0.005), key=lambda c: -c["turnout_delta"])[:5]
        dn = sorted((c for c in tc if c["turnout_delta"] <= -0.005), key=lambda c: c["turnout_delta"])[:5]
        if up:
            add("cities", "up", "Largest turnout rises: " + ", ".join(f"{iso(c['name'])} {c['turnout_delta']*100:+.1f}pp" for c in up))
        if dn:
            add("cities", "down", "Largest turnout drops: " + ", ".join(f"{iso(c['name'])} {c['turnout_delta']*100:+.1f}pp" for c in dn))
        flips = [c for c in solid if c.get("flipped")]
        if flips:
            add("cities", "key", f"{len(flips)} larger localities changed their leading camp, e.g. " + ", ".join(
                f"{iso(c['name'])} ({fam_name.get(c['base_winner_family'], c['base_winner_family'])} → "
                f"{fam_name.get(c['winner_family'], c['winner_family'])})" for c in flips[:6]))
        sw = [c for c in solid if c.get("bloc_swing") and max(abs(v or 0) for v in c["bloc_swing"].values()) >= 0.01]
        if sw:
            big = sorted(sw, key=lambda c: -max(abs(v or 0) for v in c["bloc_swing"].values()))[:5]
            add("cities", "info", "Biggest bloc swings: " + ", ".join(
                f"{iso(c['name'])} ({max(c['bloc_swing'].items(), key=lambda kv: abs(kv[1] or 0))[0]} "
                f"{max(c['bloc_swing'].values(), key=lambda v: abs(v or 0))*100:+.1f}pp)" for c in big))
        if nat.get("invalid_pct") is not None:
            bi = st["baseline_national"]["invalid_pct"]
            add("turnout", "info", f"Invalid ballots: {nat['invalid_pct']*100:.2f}% of voters (baseline {bi*100:.2f}%).")
        return ins

    def _changes(self, st: dict, prev: Optional[dict]) -> dict:
        if not prev:
            return {"first": True}
        pc = {c["code"]: c for c in prev.get("cities", []) if c.get("valid")}
        new = [c for c in st["cities"] if c.get("valid") and c["code"] not in pc]
        grew = [c for c in st["cities"] if c.get("valid") and c["code"] in pc and c["valid"] > pc[c["code"]]["valid"]]
        pp = {p["letter"]: p for p in prev.get("parties", [])}
        moves = []
        for p in st["parties"]:
            o = pp.get(p["letter"])
            if o and p["pct"] is not None and o["pct"] is not None:
                moves.append({"letter": p["letter"], "name": p["name"], "delta": _r(p["pct"] - o["pct"], 5),
                              "seats_delta": p["seats"] - o["seats"]})
        moves.sort(key=lambda m: -abs(m["delta"]))
        return {
            "first": False,
            "since": prev.get("meta", {}).get("analyzed_at"),
            "votes_added": st["national"]["valid"] - prev.get("national", {}).get("valid", 0),
            "new_localities": [c["name"] for c in sorted(new, key=lambda c: -(c.get("valid") or 0))[:15]],
            "new_localities_count": len(new),
            "updated_localities_count": len(grew),
            "party_moves": moves[:8],
        }
