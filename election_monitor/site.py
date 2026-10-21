"""Reading the Central Elections Committee results website (votesNN.bechirot.gov.il).

Structure of the site, as observed on https://votes25.bechirot.gov.il/ :

* ``/`` and ``/nationalresults`` - national totals table (eligible, voters, turnout,
  valid, invalid) and a table of all lists: full name, ballot letters, seats,
  % of valid votes, votes. A heading states the status (e.g. "תוצאות סופיות רשמיות").
* ``/cityresults?cityID=<locality code>`` - the same two tables for one locality.
* ``/ballotresults?cityID=<code>&BallotNumber=<n>`` - the same for one ballot box;
  the heading names the polling place and the ballot's serial ("ברזל") number.
* Download links on every page (opened with ``window.open``):
  ``https://mediaNN.bechirot.gov.il/files/expc.csv`` (per locality) and
  ``https://mediaNN.bechirot.gov.il/files/expb.csv`` (per ballot box).
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Optional

from .feed import USER_AGENT, decode


def fetch_html(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text, _ = decode(resp.read())
    return text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).replace("‏", "").strip()


def _num(s: str) -> Optional[float]:
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_results_page(page: str) -> dict:
    """Parse a national / city / ballot results page into totals + list table."""
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<select.*?</select>", "", page, flags=re.S)
    headings = [_clean(h) for h in re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", body, flags=re.S)]
    tables = []
    for tb in re.findall(r"<table.*?</table>", body, flags=re.S):
        rows = [[_clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S)]
                for tr in re.findall(r"<tr.*?</tr>", tb, flags=re.S)]
        tables.append([r for r in rows if r])
    out: dict = {"headings": headings, "totals": {}, "lists": []}
    for rows in tables:
        if not rows:
            continue
        head = rows[0]
        if any("בעלי זכות בחירה" in h for h in head) and len(rows) > 1:
            for h, v in zip(head, rows[1]):
                key = ("eligible" if "בעלי זכות" in h else "turnout" if "שיעור" in h else
                       "valid" if "כשרים" in h else "invalid" if "פסולים" in h else
                       "voters" if "קולות" in h else h)
                n = _num(v)
                out["totals"][key] = round(n / 100, 6) if key == "turnout" and n is not None else n
        elif any("אותיות" in h for h in head):
            ix = {h: i for i, h in enumerate(head)}

            def col(word):
                return next((i for h, i in ix.items() if word in h), None)

            ci_name, ci_letter, ci_seats = col("שם הרשימה"), col("אותיות"), col("מנדטים")
            ci_pct, ci_votes = col("אחוז"), col("מספר הקולות")
            for r in rows[1:]:
                if ci_letter is None or ci_letter >= len(r) or not r[ci_letter]:
                    continue
                out["lists"].append({
                    "letter": r[ci_letter],
                    "name": r[ci_name] if ci_name is not None else r[ci_letter],
                    "seats": int(_num(r[ci_seats]) or 0) if ci_seats is not None else None,
                    "pct": (_num(r[ci_pct]) or 0) / 100 if ci_pct is not None else None,
                    "votes": int(_num(r[ci_votes]) or 0) if ci_votes is not None else None,
                })
    out["status"] = headings[-1] if headings else ""
    return out


def find_csv_links(page: str, base_url: str) -> dict[str, list[str]]:
    """Links to expc/expb CSV files anywhere in a page (including window.open(...) handlers)."""
    text = html.unescape(page)
    found: dict[str, list[str]] = {"expc": [], "expb": []}
    for m in re.finditer(r"""([^"'()\s,;]*?(exp[bc])[^"'()\s,;]*?\.csv)""", text, re.I):
        url = urllib.parse.urljoin(base_url, m.group(1))
        kind = m.group(2).lower()
        if url not in found[kind]:
            found[kind].append(url)
    return found


def site_links(page: str, base_url: str) -> list[str]:
    """Internal navigation links of the results site (national / city / ballot pages)."""
    links = []
    for h in re.findall(r'href="([^"#][^"]*)"', page):
        h = html.unescape(h)
        if h.startswith("javascript") or "gov.il/he/" in h or re.search(r"\.(css|ico|png|js)(\?|$)", h):
            continue
        u = urllib.parse.urljoin(base_url, h)
        if u not in links:
            links.append(u)
    return links


def sync(base_url: str, dest_dir: str, log=print) -> dict:
    """Download a results site's data: the national page (parsed to JSON) and the CSV files it links to."""
    import json
    import os

    os.makedirs(dest_dir, exist_ok=True)
    base_url = base_url.rstrip("/") + "/"
    home = fetch_html(base_url)
    national = parse_results_page(home)
    national["source"] = base_url
    links = find_csv_links(home, base_url)
    national["files"] = links
    national["pages"] = site_links(home, base_url)
    with open(os.path.join(dest_dir, "national.json"), "w", encoding="utf-8") as f:
        json.dump(national, f, ensure_ascii=False, indent=1)
    log(f"national page: {national['status']} - {len(national['lists'])} lists")
    for kind, urls in links.items():
        for url in urls[:1]:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
            with open(os.path.join(dest_dir, f"{kind}.csv"), "wb") as f:
                f.write(raw)
            log(f"{kind}.csv <- {url} ({len(raw):,} bytes)")
    return national
