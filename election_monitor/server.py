"""Polling loop + HTTP server for the live dashboard (standard library only)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .analysis import Analyzer
from .feed import ResultFile, Source, decode as decode_bytes, load_file, parse
from .site import fetch_html, find_csv_links, parse_results_page

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Cap on history + snapshots so a long sim run doesn't grow unbounded.
HISTORY_CAP = 2000
SNAPSHOT_CAP = 200
# Token for write endpoints: either an env-var override or a per-process random hex.
# Clients on the same loopback bypass; everything else must present X-Admin-Token.
_ADMIN_TOKEN = os.environ.get("EM_ADMIN_TOKEN") or hashlib.sha256(os.urandom(16)).hexdigest()[:16]


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
        self._sim_thread: Optional[threading.Thread] = None
        self.mode = "live"  # one of: "live" (real site), "simulate", "baseline" (2022 final as current)
        self._sim_defaults = {"days": 5.0, "speed": 1000}
        # Baseline cadences — preserve so we can restore them when switching away from sim/baseline.
        self._base_cadences = {k: cfg.get(k) for k in ("poll_seconds", "expb_every", "site_every")}
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
        # Keep only the most recent SNAPSHOT_CAP files so a long sim run doesn't fill the disk.
        try:
            snaps = sorted(x for x in os.listdir(self.snap_dir) if x.endswith("_expc.csv"))
            for old in snaps[:-SNAPSHOT_CAP]:
                try:
                    os.remove(os.path.join(self.snap_dir, old))
                except OSError:
                    pass
        except OSError:
            pass

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
            src._etag.clear()
            src._lastmod.clear()

    def set_mode(self, mode: str, days: Optional[float] = None, speed: Optional[int] = None) -> dict:
        """Switch between 'live' (real site), 'simulate' (mock), and 'baseline' (2022 final).

        Reconfigures the pollable sources, stops/starts the simulation thread, and
        forgets the current state so the next poll re-ingests under the new mode.
        """
        if mode not in ("live", "simulate", "baseline"):
            raise ValueError(f"unknown mode: {mode}")
        # Stop any running sim OUTSIDE the lock so we can wait for the writer thread to
        # actually exit — otherwise a late _write() can race with the new mode's reader.
        old_sim, old_th = self.sim, self._sim_thread
        if old_sim is not None:
            old_sim.stop()
        if old_th is not None and old_th.is_alive():
            old_th.join(timeout=2.0)
        with self.lock:
            if self.sim is old_sim:
                self.sim = None
                self._sim_thread = None
            self.mode = mode
            self._discovered = False
            self.status["expc_url"] = self.status["expb_url"] = None
            self.status["last_error"] = self.status["site_error"] = None
            self.status["site_status"] = self.status["site_totals"] = None
            # Reset ALL cadences to baseline each mode-switch so values can't leak across modes.
            for k, v in self._base_cadences.items():
                if v is not None:
                    self.cfg[k] = v
                else:
                    self.cfg.pop(k, None)
            run_discover = False
            if mode == "live":
                ecfg = self.cfg["elections"][self.current]
                src = ecfg.get("sources", {}) or {}
                self.expc = Source("expc", [_abs(u) for u in src.get("expc", [])])
                self.expb = Source("expb", [_abs(u) for u in src.get("expb", [])])
                self.site_url = ecfg.get("site")
                self.discover_pages = [self.site_url, self.site_url.rstrip("/") + "/nationalresults"] if self.site_url else []
                self.cfg["poll_seconds"] = self.cfg.get("live_poll_seconds", 30)
                run_discover = True
            elif mode == "baseline":
                bdir = _abs(self.cfg["elections"][self.baseline]["baseline_dir"])
                self.expc = Source("expc", [os.path.join(bdir, "expc.csv")])
                self.expb = Source("expb", [os.path.join(bdir, "expb.csv")])
                self.site_url = None
                self.discover_pages = []
                self.cfg["poll_seconds"] = 5
            else:  # simulate
                from .simulate import LiveSim
                # Respect --data-dir instead of hardcoding ROOT/data/sim/feed.
                out = os.path.join(self.data_dir, "sim", "feed")
                os.makedirs(out, exist_ok=True)
                self.expc = Source("expc", [os.path.join(out, "expc.csv")])
                self.expb = Source("expb", [os.path.join(out, "expb.csv")])
                self.site_url = None
                self.discover_pages = []
                self.cfg["poll_seconds"] = 1
                self.cfg["expb_every"] = 1  # sim writes expb every tick, so poll it every tick
                bdir = _abs(self.cfg["elections"][self.baseline]["baseline_dir"])
                self.sim = LiveSim(os.path.join(bdir, "expb.csv"), out,
                                   self.analyzer.base.family, self.analyzer._sector_of,
                                   days=days or self._sim_defaults["days"],
                                   speed=speed or self._sim_defaults["speed"])
                self._sim_thread = self.sim.start()
            self.status["site"] = self.site_url
            self.status["poll_seconds"] = self.cfg["poll_seconds"]
        self.reset_state()
        if run_discover:
            try:
                self.discover()
                self._discovered = True
            except Exception as e:
                self.log(f"discover on mode switch failed: {e}")
        self.poke()
        self.log(f"mode: {mode}")
        return {"mode": self.mode, "sim": self.sim.status() if self.sim else None,
                "poll_seconds": self.cfg["poll_seconds"]}

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
        # Build the history slice + gzipped payload OUTSIDE the lock so HTTP handlers
        # reading /api/state and /api/status don't block for the serialize+disk cost.
        with self.lock:
            # Cap history in memory to avoid unbounded growth on long sim runs.
            if record:
                self.history.append(entry)
                if len(self.history) > HISTORY_CAP:
                    del self.history[: len(self.history) - HISTORY_CAP]
            hist_slice = list(self.history[-500:])
        state["history"] = hist_slice
        payload = gzip.compress(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())
        with self.lock:
            self.state = state
            self.state_gz = payload
            self.current_file = rf
        if record:
            # Disk write happens outside the lock. On abrupt crash a trailing partial line
            # is possible — _restore() already catches ValueError and skips it.
            try:
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as e:
                self.log(f"history append failed: {e}")

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
    log_requests = os.environ.get("EM_LOG_REQUESTS", "").lower() in ("1", "true", "yes")

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Access logs off by default (noisy at 1 Hz poll); set EM_LOG_REQUESTS=1 to enable.
            if log_requests:
                mon.log(f"{self.address_string()} - {fmt % args}")

        def _write_authorized(self) -> bool:
            # Loopback is always trusted. Tailscale CGNAT (100.64.0.0/10) is trusted by default
            # because reaching the port already requires being on the user's tailnet; the firewall
            # rule we added for /api is scoped to that same range. Set EM_STRICT_AUTH=1 to require
            # the X-Admin-Token header even from the tailnet.
            ip = (self.client_address[0] if self.client_address else "") or ""
            if ip in ("127.0.0.1", "::1", "localhost"):
                return True
            strict = os.environ.get("EM_STRICT_AUTH", "").lower() in ("1", "true", "yes")
            if not strict and ip.startswith("100.") and 64 <= int(ip.split(".", 2)[1] or 0) <= 127:
                return True
            got = self.headers.get("X-Admin-Token") or urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("token", [""])[0]
            return bool(got) and hmac.compare_digest(got, _ADMIN_TOKEN)

        def _send(self, code: int, body: bytes, ctype: str, gz: bool = False):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                # Client disconnected mid-response (phone left Tailscale range, etc). Don't spam.
                pass

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
                return self._json({**mon.status, "mode": mon.mode,
                                   "sim": mon.sim.status() if mon.sim else None})
            if path == "/api/mode":
                return self._json({"mode": mon.mode})
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
                code = int(m.group(1))
                if code not in mon.analyzer.base_cities and (
                        mon.current_file is None or code not in mon.current_file.by_city()):
                    return self._json({"error": "unknown city"}, 404)
                return self._json(mon.analyzer.city_ballots(mon.ballots, code))
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
            # Write endpoints are loopback-trusted; remote callers (Tailscale, LAN) must
            # supply X-Admin-Token or ?token= matching EM_ADMIN_TOKEN. See serve() log line.
            if u.path in ("/api/mode", "/api/sim", "/api/upload", "/api/refresh") and not self._write_authorized():
                return self._json({"error": "forbidden"}, 403)
            if u.path == "/api/mode":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n < 10000 else {}
                except ValueError:
                    body = {}
                mode = (body.get("mode") or "").strip()
                if mode not in ("live", "simulate", "baseline"):
                    return self._json({"error": "mode must be 'live', 'simulate', or 'baseline'"}, 400)
                try:
                    out = mon.set_mode(mode, days=body.get("days"), speed=body.get("speed"))
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return self._json(out)
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
                # Validate the upload is a parseable results CSV before swapping it in,
                # so a malformed/malicious body can't poison the active data source.
                try:
                    text, _ = decode_bytes(body)
                    parse(text)
                except Exception as e:
                    return self._json({"error": f"not a valid results CSV: {e}"}, 400)
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


class _CappedThreadingHTTPServer(ThreadingHTTPServer):
    """Cap concurrent client threads so a flood can't exhaust the process."""
    daemon_threads = True
    # Py 3.7+ exposes ThreadingMixIn.block_on_close; defaults false → clean fast shutdown.
    def __init__(self, *a, max_threads: int = 64, **kw):
        super().__init__(*a, **kw)
        self._sem = threading.BoundedSemaphore(max_threads)

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            try:
                request.close()
            finally:
                pass
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            try: self._sem.release()
            except ValueError: pass


def serve(mon: Monitor, host: str, port: int) -> None:
    th = threading.Thread(target=mon.run, name="poller", daemon=True)
    th.start()
    httpd = _CappedThreadingHTTPServer((host, port), make_handler(mon))
    public = host != "127.0.0.1" and host != "localhost"
    mon.log(f"dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}/  "
            f"(monitoring {mon.site_url or mon.expc.candidates[0]}, compared with "
            f"{mon.cfg['elections'][mon.baseline].get('name', mon.baseline)})")
    if public:
        mon.log(f"server is reachable from the network. "
                f"Write endpoints (/api/mode, /api/sim, /api/upload) require X-Admin-Token: {_ADMIN_TOKEN}")
    # Graceful shutdown on SIGTERM so systemd stop / docker stop doesn't kill mid-write.
    def _stop(_sig, _frame):
        mon.log("shutting down (signal received)")
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, AttributeError):
        pass  # signal only works on main thread; non-fatal if someone wraps us
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
        httpd.server_close()
