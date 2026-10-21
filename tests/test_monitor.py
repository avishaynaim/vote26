"""Tests run only on data downloaded from https://votes25.bechirot.gov.il/ (data/baseline/k25, tests/fixtures)."""

import json
import os
import shutil
import tempfile
import unittest

from election_monitor.analysis import Analyzer
from election_monitor.feed import ENVELOPES_CODE, load_file
from election_monitor.seats import bader_ofer
from election_monitor.server import load_analyzer
from election_monitor.simulate import Replay
from election_monitor.site import find_csv_links, parse_results_page

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _f:
    CFG = json.load(_f)
K25 = os.path.join(ROOT, "data/baseline/k25")
FIX = os.path.join(ROOT, "tests/fixtures")
EXPC = load_file(os.path.join(K25, "expc.csv"))
EXPB = load_file(os.path.join(K25, "expb.csv"))
with open(os.path.join(K25, "national.json"), encoding="utf-8") as _f:
    NATIONAL = json.load(_f)


def page(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class TestSitePages(unittest.TestCase):
    def test_national_page(self):
        p = parse_results_page(page("votes25_nationalresults.html"))
        self.assertEqual(p["status"], "תוצאות סופיות רשמיות")
        self.assertEqual(p["totals"]["eligible"], 6788804)
        self.assertEqual(len(p["lists"]), 40)
        self.assertEqual(sum(l["seats"] for l in p["lists"]), 120)
        self.assertEqual(p["lists"][0]["letter"], "מחל")

    def test_csv_links_on_page(self):
        links = find_csv_links(page("votes25_nationalresults.html"), "https://votes25.bechirot.gov.il/")
        self.assertEqual(links["expc"], ["https://media25.bechirot.gov.il/files/expc.csv"])
        self.assertEqual(links["expb"], ["https://media25.bechirot.gov.il/files/expb.csv"])

    def test_city_page_matches_csv(self):
        p = parse_results_page(page("votes25_cityresults_5000.html"))
        row = EXPC.by_city()[5000]
        self.assertEqual(p["totals"]["eligible"], row.eligible)
        self.assertEqual(p["totals"]["voters"], row.voters)
        for l in p["lists"]:
            self.assertEqual(l["votes"], row.votes.get(l["letter"], 0))

    def test_ballot_page_matches_csv(self):
        p = parse_results_page(page("votes25_ballotresults_5000_1.html"))
        row = next(r for r in EXPB.rows if r.code == 5000 and r.ballot in ("1", "1.0"))
        self.assertEqual(p["totals"]["voters"], row.voters)


class TestFiles(unittest.TestCase):
    def test_expc_totals_match_national_page(self):
        self.assertEqual(EXPC.encoding, "utf-8-bom")
        self.assertEqual(sum(r.eligible for r in EXPC.rows), NATIONAL["totals"]["eligible"])
        self.assertEqual(sum(r.voters for r in EXPC.rows), NATIONAL["totals"]["voters"])
        self.assertEqual(sum(r.valid for r in EXPC.rows), NATIONAL["totals"]["valid"])
        self.assertTrue(any(r.code == ENVELOPES_CODE for r in EXPC.rows))
        votes = {}
        for r in EXPC.rows:
            for k, v in r.votes.items():
                votes[k] = votes.get(k, 0) + v
        for l in NATIONAL["lists"]:
            self.assertEqual(votes[l["letter"]], l["votes"], l["letter"])

    def test_expb_aggregates_to_expc(self):
        self.assertEqual(EXPB.kind, "ballot")
        bc, cc = EXPB.by_city(), EXPC.by_city()
        self.assertEqual(set(bc), set(cc))
        for code, c in cc.items():
            self.assertEqual(bc[code].votes, c.votes)


class TestSeats(unittest.TestCase):
    def test_allocates_120_and_respects_threshold(self):
        votes = {l["letter"]: l["votes"] for l in NATIONAL["lists"]}
        s = bader_ofer(votes, [])["seats"]
        self.assertEqual(sum(s.values()), 120)
        official = {l["letter"]: l["seats"] for l in NATIONAL["lists"]}
        self.assertEqual({k for k, v in s.items() if v}, {k for k, v in official.items() if v})


class TestAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.an = load_analyzer(CFG, "25", "25")

    def test_baseline_uses_official_seats(self):
        st = self.an.analyze(EXPC)
        self.assertEqual(st["baseline_national"]["seats_source"], "official (results site)")
        n = {b["key"]: b for b in st["blocs"]}
        self.assertEqual(n["N"]["base_seats"], 64)

    def test_same_election_has_zero_swing(self):
        st = self.an.analyze(EXPC)
        for b in st["blocs"]:
            self.assertAlmostEqual(b["swing"], 0, places=4)
        self.assertAlmostEqual(st["national"]["turnout_lfl_delta"], 0, places=4)

    def test_partial_count_with_ballots(self):
        tmp = tempfile.mkdtemp()
        try:
            rp = Replay(os.path.join(K25, "expb.csv"), tmp)
            n = rp.write(0.3)
            st = self.an.analyze(load_file(os.path.join(tmp, "expc.csv")), load_file(os.path.join(tmp, "expb.csv")))
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(st["progress"]["ballots_counted"], n)
        self.assertAlmostEqual(st["progress"]["fraction"], n / len(EXPB.rows), places=3)
        self.assertLess(st["progress"]["fraction"], 0.8)
        blocs = {b["key"]: b for b in st["blocs"]}
        self.assertLessEqual(abs(blocs["N"]["proj_seats"] - 64), 2)

    def test_new_election_inherits_by_letter(self):
        an = load_analyzer(CFG, "26", "25")
        an.cur.set_names({"מחל": "הליכוד", "זזז": "רשימה חדשה"})
        self.assertEqual(an.cur.party("מחל")["bloc"], "N")
        self.assertTrue(an.cur.party("מחל")["assumed"])
        self.assertTrue(an.cur.party("זזז")["unclassified"])
        self.assertEqual(an.cur.party("זזז")["name"], "רשימה חדשה")


if __name__ == "__main__":
    unittest.main()
