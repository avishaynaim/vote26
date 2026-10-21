"""Command line entry point.

    python -m election_monitor serve                     # monitor the configured site (votes25 by default)
    python -m election_monitor --site URL serve          # monitor another site (e.g. the new one)
    python -m election_monitor --current 25 serve --simulate   # rehearsal: replay the 2022 count
    python -m election_monitor sync --election 25        # download a results site's files
    python -m election_monitor report --file expc.csv    # one-off text report
    python -m election_monitor probe                     # check which feed URLs answer
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .feed import Source, load_file, parse
from .server import ROOT, Monitor, _abs, load_analyzer, serve
from .site import fetch_html, find_csv_links, parse_results_page, sync


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_serve(a, cfg):
    sources = None
    if a.simulate:
        from .simulate import Replay
        bdir = cfg["elections"][a.current].get("baseline_dir")
        final = a.replay_file or (bdir and os.path.join(_abs(bdir), "expb.csv"))
        if not final or not os.path.exists(final):
            sys.exit("simulation needs a final expb.csv: use --current with an election that has baseline data, "
                     "or --replay-file")
        out = os.path.join(ROOT, "data", "sim", "feed")
        for fn in ("expc.csv", "expb.csv"):
            if os.path.exists(os.path.join(out, fn)):
                os.remove(os.path.join(out, fn))
        Replay(final, out, steps=a.sim_steps, step_seconds=a.sim_step_seconds).start()
        sources = {"expc": [os.path.join(out, "expc.csv")], "expb": [os.path.join(out, "expb.csv")],
                   "discover_pages": []}
        cfg["expb_every"] = 1
        cfg["poll_seconds"] = min(cfg.get("poll_seconds", 30), a.sim_step_seconds)
        a.data_dir = os.path.join("data", "sim")
        # wipe old rehearsal history so each simulation starts clean
        sim_dir = _abs(a.data_dir)
        for root, _, files in os.walk(sim_dir):
            for fn in files:
                if fn.endswith(".jsonl") or (fn.endswith("_expc.csv") and "snapshots" in root):
                    os.remove(os.path.join(root, fn))
    elif a.file:
        sources = {"expc": [os.path.abspath(a.file)], "expb": [], "discover_pages": []}
    if a.poll:
        cfg["poll_seconds"] = a.poll
    mon = Monitor(cfg, a.current, a.baseline, sources=sources, data_dir=a.data_dir)
    serve(mon, a.host, a.port)


def cmd_report(a, cfg):
    an = load_analyzer(cfg, a.current, a.baseline)
    ecfg = cfg["elections"][a.current]
    if a.file:
        rf = load_file(a.file)
    else:
        src = Source("expc", [_abs(u) for u in ecfg["sources"]["expc"]])
        r = src.fetch()
        if r.status != "new":
            sys.exit(f"could not fetch results: {r.error}")
        rf = parse(r.text, r.encoding)
    if ecfg.get("site") and a.current != a.baseline:
        try:
            an.cur.set_names({l["letter"]: l["name"] for l in parse_results_page(fetch_html(ecfg["site"]))["lists"]})
        except Exception:
            pass
    st = an.analyze(rf)
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    print_report(st, cfg)


def _p(x, d=1):
    return "   -  " if x is None else f"{x*100:5.{d}f}%"


def _pp(x):
    return "    -  " if x is None else f"{x*100:+6.1f}"


def print_report(st, cfg):
    m, n, pg = st["meta"], st["national"], st["progress"]
    print(f"== {m['current']['name']} vs {m['baseline']['name']} ==")
    print(f"Progress {(pg.get('fraction') or 0)*100:.1f}% ({pg['method']}), "
          f"{pg['localities_reporting']}/{pg['localities_total']} localities")
    print(f"Votes: voters {n['voters']:,}  valid {n['valid']:,}  invalid {_p(n['invalid_pct'],2)}  "
          f"threshold {n['threshold_votes']:,} votes")
    print(f"Turnout (same localities): {_p(n['turnout_lfl'])} vs {_p(n['turnout_lfl_base'])}  ({_pp(n['turnout_lfl_delta'])} pp)")
    print("\nBLOCS                      now    seats  proj | base   seats | LFL swing")
    for b in st["blocs"]:
        print(f"  {b['name'][:24]:24} {_p(b['pct'])} {b['seats']:5} {b['proj_seats'] if b['proj_seats'] is not None else '-':>5} |"
              f" {_p(b['base_pct'])} {b['base_seats']:5} | {_pp(b['swing'])}")
    print("\nPARTIES                         votes      pct  seats proj | family base")
    for p in st["parties"]:
        if (p["pct"] or 0) < 0.003:
            continue
        print(f"  {p['letter']:4} {p['name'][:24]:24} {p['votes']:>10,} {_p(p['pct'],2)} {p['seats']:4} "
              f"{p['proj_seats'] if p['proj_seats'] is not None else '-':>4} | {_p(p['base_family_pct'])}"
              f"{'' if p['passes'] else '  BELOW THRESHOLD'}")
    print("\nFAMILIES (cross-election)       now   base   LFL-swing  seats base")
    for f in st["families"]:
        print(f"  {f['name'][:30]:30} {_p(f['pct'])} {_p(f['base_pct'])} {_pp(f['swing'])}   {f['seats']:4} {f['base_seats']:4}")
    print("\nSECTORS                         in      turnout  base   Δpp")
    for s in st["sectors"]:
        print(f"  {s['name'][:30]:30} {s['reporting']:4}/{s['localities']:<4} {_p(s['turnout'])} {_p(s['turnout_base'])} "
              f"{_pp(s.get('turnout_delta'))}")
    print("\nINSIGHTS")
    for i in st["insights"]:
        print("  •", i["text"])


def cmd_probe(a, cfg):
    site = cfg["elections"][a.current].get("site")
    if not site:
        sys.exit("no 'site' configured")
    try:
        home = fetch_html(site, timeout=15)
    except Exception as e:
        sys.exit(f"site {site}: {getattr(e, 'reason', e)}")
    p = parse_results_page(home)
    print(f"site {site}: status '{p['status']}', {len(p['lists'])} lists, totals {p['totals']}")
    for kind, urls in find_csv_links(home, site).items():
        for u in urls:
            r = Source(kind, [u], timeout=30).fetch()
            print(f"{kind}: {u} -> {r.status}{' ' + r.error if r.error else ''}")


def cmd_sync(a, cfg):
    ecfg = cfg["elections"][a.election]
    url = a.url or ecfg.get("site")
    dest = _abs(ecfg.get("baseline_dir") or f"data/baseline/k{a.election}")
    sync(url, dest)
    print(f"saved to {dest}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="election_monitor", description="Live Knesset election results monitor")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--current", help="election key to monitor (default from config)")
    ap.add_argument("--baseline", help="election key to compare with (default from config)")
    ap.add_argument("--site", help="results site to monitor (default: config 'site', https://votes25.bechirot.gov.il/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="poll the results feed and serve the dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8025)
    s.add_argument("--poll", type=float, help="seconds between polls")
    s.add_argument("--file", help="watch a local expc.csv instead of the configured URLs")
    s.add_argument("--data-dir", default="data")
    s.add_argument("--simulate", action="store_true", help="replay a final file live (rehearsal)")
    s.add_argument("--replay-file", help="final expb.csv to replay (default: the current election's baseline expb.csv)")
    s.add_argument("--sim-steps", type=int, default=40)
    s.add_argument("--sim-step-seconds", type=float, default=5.0)
    r = sub.add_parser("report", help="print a one-off analysis")
    r.add_argument("--file", help="expc.csv to analyse (default: fetch from the configured URLs)")
    r.add_argument("--json", action="store_true")
    sub.add_parser("probe", help="check which configured feed URLs respond")
    y = sub.add_parser("sync", help="download a results site's national page and CSV files as baseline data")
    y.add_argument("--election", default="25")
    y.add_argument("--url", help="site URL (default: the election's 'site' in config)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if getattr(a, "site", None):
        cfg["elections"][cfg["current"]]["site"] = a.site
    a.current = a.current or cfg["current"]
    a.baseline = a.baseline or cfg["baseline"]
    for k in (a.current, a.baseline):
        if k not in cfg["elections"]:
            sys.exit(f"unknown election '{k}', known: {list(cfg['elections'])}")
    {"serve": cmd_serve, "report": cmd_report, "probe": cmd_probe, "sync": cmd_sync}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
