"""Fetching and parsing the Central Elections Committee result files.

The committee publishes two flat CSV files while counting:

* ``expc.csv`` - one row per locality (city / town / kibbutz / Bedouin tribe ...)
* ``expb.csv`` - one row per ballot box (קלפי)

Both share the same shape: a few metadata columns (committee code, locality name,
locality code, eligible voters, voters, invalid, valid) followed by one column per
ballot letter (אותיות הרשימה). Party columns are read from the header, never
hard coded, so new lists show up automatically.

The votes25 files are UTF-8 with BOM; plain UTF-8 and windows-1255 are handled as fallbacks.
"""

from __future__ import annotations

import csv
import io
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

ENVELOPES_CODE = 9999  # "מעטפות חיצוניות" - double envelopes (soldiers, diplomats, hospitals, prisons)

# Canonical metadata column -> accepted header spellings (after normalisation).
META_ALIASES = {
    "committee": ["סמל ועדה", "ועדה"],
    "name": ["שם ישוב", "שם יישוב", "ישוב", "יישוב"],
    "code": ["סמל ישוב", "סמל יישוב"],
    "ballot": ["קלפי", "מספר קלפי", "סמל קלפי"],
    "eligible": ["בזב", "בז ב", "בעלי זכות בחירה"],
    "voters": ["מצביעים"],
    "invalid": ["פסולים"],
    "valid": ["כשרים"],
}
# Extra metadata columns that appear in expb.csv and must not be treated as parties.
EXTRA_META = {"ריכוז", "שופט", "ברזל", "ת עדכון", "תאריך", "סמל קלפי", "מספר קלפי"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def _norm(h: str) -> str:
    h = h.replace("﻿", "").replace('"', " ").replace("'", " ").replace("״", " ").replace("׳", " ")
    return re.sub(r"\s+", " ", h).strip()


def decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-bom"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1255", errors="replace"), "windows-1255"


def _int(v: str) -> int:
    v = (v or "").strip().replace(",", "")
    if not v:
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


@dataclass
class Row:
    committee: str
    name: str
    code: int
    eligible: int
    voters: int
    invalid: int
    valid: int
    votes: dict[str, int]
    ballot: Optional[str] = None


@dataclass
class ResultFile:
    kind: str  # "city" or "ballot"
    parties: list[str]
    rows: list[Row]
    encoding: str = ""

    def by_city(self) -> dict[int, Row]:
        """Aggregate rows per locality code (identity for expc, sums ballots for expb)."""
        out: dict[int, Row] = {}
        for r in self.rows:
            c = out.get(r.code)
            if c is None:
                out[r.code] = Row(r.committee, r.name, r.code, r.eligible, r.voters,
                                  r.invalid, r.valid, dict(r.votes))
            else:
                c.eligible += r.eligible
                c.voters += r.voters
                c.invalid += r.invalid
                c.valid += r.valid
                for k, v in r.votes.items():
                    c.votes[k] = c.votes.get(k, 0) + v
        return out


def parse(text: str, encoding: str = "") -> ResultFile:
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("empty file")
    header = [_norm(h) for h in rows[0]]
    idx: dict[str, int] = {}
    for key, aliases in META_ALIASES.items():
        for i, h in enumerate(header):
            if h in aliases:
                idx[key] = i
                break
    missing = [k for k in ("name", "code", "voters", "valid") if k not in idx]
    if missing:
        raise ValueError(f"unrecognised header, missing {missing}: {header[:10]}")
    # Party columns are everything after the last metadata column.
    last_meta = max(idx.values())
    party_cols = [(i, h) for i, h in enumerate(header)
                  if i > last_meta and h and h not in EXTRA_META]
    out_rows = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        code = _int(r[idx["code"]])
        if not code and not r[idx["name"]].strip():
            continue
        out_rows.append(Row(
            committee=r[idx["committee"]].strip() if "committee" in idx else "",
            name=re.sub(r"\s+", " ", r[idx["name"]]).strip(),
            code=code,
            eligible=_int(r[idx["eligible"]]) if "eligible" in idx else 0,
            voters=_int(r[idx["voters"]]),
            invalid=_int(r[idx["invalid"]]) if "invalid" in idx else 0,
            valid=_int(r[idx["valid"]]),
            votes={h: _int(r[i]) for i, h in party_cols},
            ballot=r[idx["ballot"]].strip() if "ballot" in idx else None,
        ))
    return ResultFile("ballot" if "ballot" in idx else "city", [h for _, h in party_cols], out_rows, encoding)


def load_file(path: str) -> ResultFile:
    with open(path, "rb") as f:
        text, enc = decode(f.read())
    return parse(text, enc)


@dataclass
class FetchResult:
    status: str  # "new", "unchanged", "error"
    url: str
    text: Optional[str] = None
    encoding: str = ""
    last_modified: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


@dataclass
class Source:
    """One logical file (expc or expb) with a list of candidate locations.

    Candidates can be http(s) URLs or local paths (useful when the site is
    geo-blocked and files are downloaded by hand into ``data/inbox``). The first
    candidate that works is remembered and tried first next time.
    """

    name: str
    candidates: list[str]
    timeout: float = 20.0
    active: Optional[str] = None
    _etag: dict[str, str] = field(default_factory=dict)
    _lastmod: dict[str, str] = field(default_factory=dict)
    _mtime: dict[str, float] = field(default_factory=dict)

    def add_candidates(self, urls: list[str]) -> None:
        for u in urls:
            if u not in self.candidates:
                self.candidates.append(u)

    def fetch(self) -> FetchResult:
        order = ([self.active] if self.active else []) + [c for c in self.candidates if c != self.active]
        errors = []
        for loc in order:
            res = self._fetch_one(loc)
            if res.status != "error":
                self.active = loc
                return res
            errors.append(f"{loc}: {res.error}")
        return FetchResult("error", order[0] if order else "", error=" | ".join(errors) or "no candidates")

    def _fetch_one(self, loc: str) -> FetchResult:
        t0 = time.time()
        if not re.match(r"^https?://", loc):
            path = loc[7:] if loc.startswith("file://") else loc
            if not os.path.exists(path):
                return FetchResult("error", loc, error="file not found")
            mtime = os.path.getmtime(path)
            if self._mtime.get(loc) == mtime:
                return FetchResult("unchanged", loc)
            with open(path, "rb") as f:
                text, enc = decode(f.read())
            self._mtime[loc] = mtime
            return FetchResult("new", loc, text, enc, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime)),
                               elapsed_ms=int((time.time() - t0) * 1000))
        headers = {"User-Agent": USER_AGENT, "Accept": "text/csv,text/plain,*/*",
                   "Accept-Language": "he-IL,he;q=0.9,en;q=0.8", "Cache-Control": "no-cache"}
        if loc in self._etag:
            headers["If-None-Match"] = self._etag[loc]
        if loc in self._lastmod:
            headers["If-Modified-Since"] = self._lastmod[loc]
        req = urllib.request.Request(loc, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                et, lm = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return FetchResult("unchanged", loc, elapsed_ms=int((time.time() - t0) * 1000))
            return FetchResult("error", loc, error=f"HTTP {e.code}")
        except Exception as e:  # network errors, timeouts, DNS, TLS
            return FetchResult("error", loc, error=str(getattr(e, "reason", e)))
        text, enc = decode(raw)
        if "<html" in text[:500].lower():
            return FetchResult("error", loc, error="got HTML instead of CSV")
        if et:
            self._etag[loc] = et
        if lm:
            self._lastmod[loc] = lm
        return FetchResult("new", loc, text, enc, lm, elapsed_ms=int((time.time() - t0) * 1000))
