"""Polling loop + HTTP server for the live dashboard (standard library only)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .analysis import Analyzer
from .feed import ResultFile, Source, load_file, parse
from .site import fetch_html, find_csv_links, parse_results_page

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _abs(p: str) -> str:
    if re.match(r"^https?://", p) or os.path.isabs(p):
        return p
    return os.path.join(ROOT, p)


class Monitor:
    def __init__(self, cfg: dict, current: str, baseline: str, sources: Optional[dict] = None,
                 data_dir: str = "data", log=print):
        self.cfg = cfg
        self.current, self.baseline = current, baseline
        self.log = log
        ecfg = cfg["elections"][current]
        src = sources or ecfg.get("sources", {})
        self.expc = Source("expc", [_abs(u) for u in src.get("expc", [])])
        self.expb = Source("expb", [_abs(u) for u in src.get("expb", [])])
        self.site_url = None if sources else ecfg.get("site")
        # result-file links are read from the site's own pages (home + national results page)
        self.discover_pages = src.get("discover_pages") or (
            [self.site_url, self.site_url.rstrip("/") + "/nationalresults"] if self.site_url else [])
        self.analyzer = load_analyzer(cfg, current, baseline)
        self.data_dir = _abs(data_dir)
        self.snap_dir = os.path.join(self.data_dir, "snapshots", f"k{current}")
        os.makedirs(self.snap_dir, exist_ok=True)
        self.history_path = os.path.join(self.data_dir, f"history_k{current}_vs_k{baseline}.jsonl")
        self.lock = threading.Lock()
        self.state: Optional[dict] = None
        self.state_gz: Optional[bytes] = None
        self.current_file: Optional[ResultFile] = None
        self.ballots: Optional[ResultFile] = None
        self.history: list[dict] = []
        self.last_hash = None
        self.status = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "polls": 0, "changes": 0,
                       "last_poll": None, "last_change": None, "last_error": None, "expc_url": None,
                       "expb_url": None, "poll_seconds": cfg.get("poll_seconds", 30), "candidates": {},
                       "site": self.site_url, "site_status": None, "site_totals": None, "site_error": None}
        self.sim = None  # LiveSim when running a simulation
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._discovered = False
        self._restore()

    # ----------------------------------------------------------------- persistence
    def _restore(self) -> None:
        if os.path.exists(self.history_path):
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        self.history.append(json.loads(line))
                    except ValueError:
                        pass
        snaps = sorted(x for x in os.listdir(self.snap_dir) if x.endswith("_expc.csv"))
        if snaps:
            try:
                self._ingest(load_file(os.path.join(self.snap_dir, snaps[-1])), {"restored_from": snaps[-1]},
                             record=False)
                self.log(f"restored last snapshot {snaps[-1]}")
            except Exception as e:  # corrupted snapshot should not prevent start-up
                self.log(f"could not restore snapshot: {e}")

    def _save_snapshot(self, text: str, h: str) -> None:
        name = time.strftime("%Y%m%d-%H%M%S") + f"_{h[:8]}_expc.csv"
        with open(os.path.join(self.snap_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    # ----------------------------------------------------------------- core
    def reset_state(self) -> None:
        """Forget all results (used when a simulation restarts)."""
        with self.lock:
            self.state = self.state_gz = self.current_file = self.ballots = None
            self.history = []
            self.last_hash = None
            if os.path.exists(self.history_path):
                os.remove(self.history_path)
        for src in (self.expc, self.expb):
            src._mtime.clear()

    def _ingest(self, rf: ResultFile, meta: dict, record: bool = True) -> None:
        prev = self.state
        if self.sim is not None:
            meta = {**meta, "sim_clock": self.sim.clock_label(), "simulation": True}
        state = self.analyzer.analyze(rf, self.ballots, meta=meta, previous=prev)
        entry = {
            "t": state["meta"]["analyzed_at"],
            "vt": state["meta"].get("sim_clock"),
            "progress": state["progress"].get("fraction"),
            "valid": state["national"]["valid"],
            "turnout_lfl": state["national"]["turnout_lfl"],
            "turnout_lfl_base": state["national"]["turnout_lfl_base"],
            "blocs": {b["key"]: [b["pct"], b["seats"], b["proj_seats"]] for b in state["blocs"]},
            "parties": {p["letter"]: [p["pct"], p["seats"], p["proj_seats"]] for p in state["parties"] if (p["pct"] or 0) >= 0.005},
        }
        with self.lock:
            if record:
                self.history.append(entry)
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            state["history"] = self.history[-500:]
            self.state = state
            self.state_gz = gzip.compress(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())
            self.current_file = rf

    def poll_site(self) -> None:
        """Read the results site's national page: official list names and the status headline."""
        try:
            page = parse_results_page(fetch_html(self.site_url))
        except Exception as e:
            self.status["site_error"] = str(getattr(e, "reason", e))
            return
        self.status["site_error"] = None
        self.status["site_status"] = page["status"]
        self.status["site_totals"] = page["totals"]
        names = {l["letter"]: l["name"] for l in page["lists"]}
        if names and names != self.analyzer.cur.names:
            self.analyzer.cur.set_names(names)
            if self.current_file is not None:  # re-label the current state with the official names
                self._ingest(self.current_file, (self.state or {}).get("meta", {}), record=False)

    def discover(self) -> None:
        for page_url in self.discover_pages:
            try:
                found = find_csv_links(fetch_html(page_url), page_url)
            except Exception:
                continue
            self.expc.add_candidates(found["expc"])
            self.expb.add_candidates(found["expb"])
            if found["expc"] or found["expb"]:
                self.log(f"discovered result files on {page_url}: {found}")

    def poll_once(self) -> None:
        self.status["polls"] += 1
        self.status["last_poll"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if self.site_url and (self.status["polls"] - 1) % max(1, int(self.cfg.get("site_every", 4))) == 0:
            self.poll_site()
        if self.expb.candidates and (self.status["polls"] - 1) % max(1, int(self.cfg.get("expb_every", 2))) == 0:
            rb = self.expb.fetch()
            if rb.status == "new":
                try:
                    self.ballots = parse(rb.text, rb.encoding)
                    self.status["expb_url"] = rb.url
                except ValueError as e:
                    self.log(f"expb parse error: {e}")
        r = self.expc.fetch()
        if r.status == "error":
            self.status["last_error"] = r.error
            # the site may not exist yet before election night: rescan it every 10 failed polls
            if self.discover_pages and (not self._discovered or self.status["polls"] % 10 == 0):
                self._discovered = True
                self.discover()
            return
        self.status["expc_url"] = r.url
        self.status["last_error"] = None
        if r.status == "unchanged":
            return
        h = hashlib.sha1(r.text.encode()).hexdigest()
        if h == self.last_hash:
            return
        try:
            rf = parse(r.text, r.encoding)
        except ValueError as e:
            self.status["last_error"] = f"parse error: {e}"
            return
        self.last_hash = h
        self._save_snapshot(r.text, h)
        self._ingest(rf, {"source": r.url, "last_modified": r.last_modified, "encoding": r.encoding,
                          "fetch_ms": r.elapsed_ms, "hash": h[:10]})
        self.status["changes"] += 1
        self.status["last_change"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        st = self.state
        self.log(f"update: progress {(st['progress'].get('fraction') or 0)*100:.1f}%, "
                 f"{st['national']['valid']:,} valid votes, "
                 + ", ".join(f"{b['key']}={b['seats']}" for b in st["blocs"]))

    def run(self) -> None:
        if self.discover_pages:
            self._discovered = True
            self.discover()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # keep polling whatever happens
                self.status["last_error"] = f"{type(e).__name__}: {e}"
                self.log(f"poll error: {e}")
            self.status["candidates"] = {"expc": self.expc.candidates, "expb": self.expb.candidates}
            self._wake.wait(float(self.cfg.get("poll_seconds", 30)))
            self._wake.clear()

    def poke(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ----------------------------------------------------------------- exports
    def cities_csv(self) -> str:
        st = self.state or {"cities": []}
        buf = io.StringIO()
        w = csv.writer(buf)
        blocs = list(self.cfg["blocs"])
        w.writerow(["code", "name", "sector", "reporting", "eligible", "voters", "valid", "turnout", "base_turnout",
                    "turnout_delta_pp", "winner", "base_winner", "flipped"]
                   + [f"{b}_pct" for b in blocs] + [f"{b}_base_pct" for b in blocs])
        for c in st["cities"]:
            def pp(x):
                return "" if x is None else round(x * 100, 2)
            w.writerow([c["code"], c["name"], c["sector"], pp(c.get("reporting")), c.get("eligible", ""),
                        c.get("voters", ""), c.get("valid", ""), pp(c.get("turnout")), pp(c.get("base_turnout")),
                        pp(c.get("turnout_delta")), c.get("winner", ""), c.get("base_winner", ""), c.get("flipped", "")]
                       + [pp((c.get("blocs") or {}).get(b)) for b in blocs]
                       + [pp((c.get("base_blocs") or {}).get(b)) for b in blocs])
        return "﻿" + buf.getvalue()


def load_analyzer(cfg: dict, current: str, baseline: str) -> Analyzer:
    bdir = cfg["elections"][baseline].get("baseline_dir")
    if not bdir or not os.path.exists(os.path.join(_abs(bdir), "expc.csv")):
        raise SystemExit(f"no baseline data for election {baseline}: run "
                         f"`python3 -m election_monitor sync --election {baseline}` first")
    bdir = _abs(bdir)
    national = None
    if os.path.exists(os.path.join(bdir, "national.json")):
        with open(os.path.join(bdir, "national.json"), encoding="utf-8") as f:
            national = json.load(f)
    ballots = load_file(os.path.join(bdir, "expb.csv")) if os.path.exists(os.path.join(bdir, "expb.csv")) else None
    return Analyzer(cfg, load_file(os.path.join(bdir, "expc.csv")), current, baseline, national, ballots)


def make_handler(mon: Monitor):
    static_dir = os.path.join(ROOT, "static")
    inbox = os.path.join(mon.data_dir, "inbox")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, gz: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            path = u.path
            if path in ("/", "/index.html"):
                with open(os.path.join(static_dir, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                with mon.lock:
                    gz = mon.state_gz
                if gz is None:
                    return self._json({"empty": True, "status": mon.status,
                                       "meta": {"current": mon.current, "baseline": mon.baseline}})
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    return self._send(200, gz, "application/json; charset=utf-8", gz=True)
                return self._send(200, gzip.decompress(gz), "application/json; charset=utf-8")
            if path == "/api/status":
                return self._json({**mon.status, "sim": mon.sim.status() if mon.sim else None})
            if path == "/api/sim":
                return self._json(mon.sim.status() if mon.sim else {"error": "not a simulation"}, 200 if mon.sim else 404)
            if path == "/api/config":
                a = mon.analyzer
                letters_now = set(a.cur.cfg.get("parties", {})) | set(a.cur.names)
                if mon.current_file is not None:
                    letters_now |= set(mon.current_file.parties)
                return self._json({"families": mon.cfg["families"], "blocs": mon.cfg["blocs"],
                                   "current": {l: a.cur.party(l) for l in letters_now},
                                   "baseline": {l: a.base.party(l) for l in a.base_letters}})
            m = re.match(r"^/api/city/(\d+)/ballots$", path)
            if m:
                return self._json(mon.analyzer.city_ballots(mon.ballots, int(m.group(1))))
            m = re.match(r"^/api/ballot/(\d+)/([^/]+)$", path)
            if m:
                d = mon.analyzer.ballot_detail(mon.ballots, int(m.group(1)), urllib.parse.unquote(m.group(2)))
                return self._json(d) if d else self._json({"error": "not found"}, 404)
            m = re.match(r"^/api/party/([^/]+)$", path)
            if m:
                d = mon.analyzer.party_detail(mon.current_file, urllib.parse.unquote(m.group(1)))
                return self._json(d) if d else self._json({"error": "no data"}, 404)
            if path == "/api/search":
                q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
                return self._json(mon.analyzer.search(mon.current_file, q))
            m = re.match(r"^/api/city/(\d+)$", path)
            if m:
                d = mon.analyzer.city_detail(mon.current_file, int(m.group(1)))
                return self._json(d) if d else self._json({"error": "not found"}, 404)
            if path == "/api/export/cities.csv":
                return self._send(200, mon.cities_csv().encode("utf-8"), "text/csv; charset=utf-8")
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/sim":
                if not mon.sim:
                    return self._json({"error": "not a simulation"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n < 10000 else {}
                except ValueError:
                    body = {}
                st = mon.sim.control(speed=body.get("speed"), action=body.get("action"))
                if body.get("action") == "reset":
                    mon.reset_state()
                mon.poke()
                return self._json(st)
            if u.path == "/api/refresh":
                mon.poke()
                return self._json({"ok": True})
            if u.path == "/api/upload":
                kind = urllib.parse.parse_qs(u.query).get("kind", ["expc"])[0]
                if kind not in ("expc", "expb"):
                    return self._json({"error": "kind must be expc or expb"}, 400)
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 50_000_000:
                    return self._json({"error": "empty or too large"}, 400)
                body = self.rfile.read(n)
                os.makedirs(inbox, exist_ok=True)
                dest = os.path.join(inbox, f"{kind}.csv")
                with open(dest, "wb") as f:
                    f.write(body)
                (mon.expc if kind == "expc" else mon.expb).add_candidates([dest])
                (mon.expc if kind == "expc" else mon.expb).active = dest
                mon.poke()
                return self._json({"ok": True, "saved": dest})
            return self._json({"error": "not found"}, 404)

    return H


def serve(mon: Monitor, host: str, port: int) -> None:
    th = threading.Thread(target=mon.run, name="poller", daemon=True)
    th.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(mon))
    mon.log(f"dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}/  "
            f"(monitoring {mon.site_url or mon.expc.candidates[0]}, compared with "
            f"{mon.cfg['elections'][mon.baseline].get('name', mon.baseline)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
        httpd.server_close()
