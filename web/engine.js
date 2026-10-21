/* In-browser engine for the hosted dashboard.

   A JavaScript port of election_monitor (analysis.py, seats.py, simulate.LiveSim). It answers the
   same /api/... requests the Python server answers, so static/index.html runs unchanged inside a
   static page (claude.ai Artifact) with no server: window.fetch is intercepted for "/api/" URLs.

   Data: data/expb.csv (2022 ballot-box results downloaded from votes25.bechirot.gov.il),
   data/national.json (the site's national page: official list names and seats), data/config.json. */
(function () {
  "use strict";
  const ENV = 9999, SHRINK = 5000;
  const SECTOR_HE = { arab: "יישובים ערביים ובדואיים", haredi: "יישובים חרדיים", large: "ערים גדולות", jewish: "יישובים יהודיים אחרים", envelopes: "מעטפות חיצוניות", new: "יישובים חדשים" };
  const SECTOR_EN = { arab: "Arab & Bedouin localities", haredi: "Haredi localities", large: "Large Jewish / mixed cities", jewish: "Other Jewish localities", envelopes: "Double envelopes (soldiers, diplomats...)", new: "New localities (no baseline)" };
  const r = (x, n = 4) => x == null || Number.isNaN(x) ? null : Math.round(x * 10 ** n) / 10 ** n;
  const pct = (a, b) => b ? a / b : null;
  const add = (o, k, v) => { o[k] = (o[k] || 0) + v; };
  const sumObjs = list => { const o = {}; for (const d of list) for (const k in d) add(o, k, d[k]); return o; };
  const nowIso = () => { const d = new Date(), z = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`; };
  const fmt = n => Math.round(n).toLocaleString("en-US");
  const sgn = x => "‎" + (x >= 0 ? "+" : "-") + Math.abs(x * 100).toFixed(1);

  // ------------------------------------------------------------------ CSV
  function parseCSV(text) {
    text = text.replace(/^﻿/, "");
    const rows = []; let row = [], f = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
      else if (c === '"') q = true;
      else if (c === ",") { row.push(f); f = ""; }
      else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(f); f = ""; if (row.some(x => x.trim())) rows.push(row); row = []; }
      else f += c;
    }
    if (f || row.length) { row.push(f); if (row.some(x => x.trim())) rows.push(row); }
    return rows;
  }
  const norm = h => h.replace(/﻿/g, "").replace(/["'״׳]/g, " ").replace(/\s+/g, " ").trim();
  const META = { committee: ["סמל ועדה", "ועדה"], name: ["שם ישוב", "שם יישוב", "ישוב", "יישוב"], code: ["סמל ישוב", "סמל יישוב"],
    ballot: ["קלפי", "מספר קלפי", "סמל קלפי"], eligible: ["בזב", "בז ב", "בעלי זכות בחירה"], voters: ["מצביעים"], invalid: ["פסולים"], valid: ["כשרים"] };
  const EXTRA = new Set(["ריכוז", "שופט", "ברזל", "ת עדכון", "תאריך", "סמל קלפי", "מספר קלפי"]);
  const num = v => { v = String(v || "").trim().replace(/,/g, ""); const n = parseFloat(v); return Number.isFinite(n) ? Math.trunc(n) : 0; };
  function parseResults(text) {
    const rows = parseCSV(text);
    if (!rows.length) throw new Error("empty file");
    const header = rows[0].map(norm), idx = {};
    for (const [k, al] of Object.entries(META)) { const i = header.findIndex(h => al.includes(h)); if (i >= 0) idx[k] = i; }
    for (const k of ["name", "code", "voters", "valid"]) if (!(k in idx)) throw new Error("unrecognised header: " + header.slice(0, 8).join(","));
    const last = Math.max(...Object.values(idx));
    const pcols = header.map((h, i) => [i, h]).filter(([i, h]) => i > last && h && !EXTRA.has(h));
    const out = [];
    for (const raw of rows.slice(1)) {
      const g = i => raw[i] ?? "";
      const code = num(g(idx.code)); if (!code && !g(idx.name).trim()) continue;
      const votes = {}; for (const [i, h] of pcols) votes[h] = num(g(i));
      out.push({ committee: "committee" in idx ? g(idx.committee).trim() : "", name: g(idx.name).replace(/\s+/g, " ").trim(), code,
        ballot: "ballot" in idx ? g(idx.ballot).trim() : null, eligible: "eligible" in idx ? num(g(idx.eligible)) : 0,
        voters: num(g(idx.voters)), invalid: "invalid" in idx ? num(g(idx.invalid)) : 0, valid: num(g(idx.valid)), votes });
    }
    return { kind: "ballot" in idx ? "ballot" : "city", parties: pcols.map(x => x[1]), rows: out };
  }
  function byCity(rows) {
    const m = new Map();
    for (const x of rows) {
      const c = m.get(x.code);
      if (!c) m.set(x.code, { committee: x.committee, name: x.name, code: x.code, eligible: x.eligible, voters: x.voters, invalid: x.invalid, valid: x.valid, votes: { ...x.votes } });
      else { c.eligible += x.eligible; c.voters += x.voters; c.invalid += x.invalid; c.valid += x.valid; for (const k in x.votes) add(c.votes, k, x.votes[k]); }
    }
    return m;
  }

  // ------------------------------------------------------------------ seats (Bader-Ofer)
  function baderOfer(votes, agreements, seats = 120, thr = 0.0325) {
    const valid = Object.values(votes).reduce((a, b) => a + b, 0), res = {};
    for (const l in votes) res[l] = 0;
    if (valid <= 0) return { seats: res, threshold_votes: 0, pairs: [], void_pairs: [] };
    const min = valid * thr, pass = Object.keys(votes).filter(l => votes[l] > 0 && votes[l] >= min);
    const vp = pass.reduce((a, l) => a + votes[l], 0);
    pass.forEach(l => res[l] = Math.floor(votes[l] / vp * seats));
    const used = new Set(), units = [], pairs = [], voids = [];
    for (const [a, b] of agreements || []) {
      if (pass.includes(a) && pass.includes(b) && !used.has(a) && !used.has(b)) { units.push([a, b]); pairs.push([a, b]); used.add(a); used.add(b); }
      else if (a in votes || b in votes) voids.push([a, b]);
    }
    pass.forEach(l => { if (!used.has(l)) units.push([l]); });
    const uv = units.map(u => u.reduce((s, l) => s + votes[l], 0)), us = units.map(u => u.reduce((s, l) => s + res[l], 0));
    let left = seats - us.reduce((a, b) => a + b, 0);
    while (left > 0 && units.length) { let bi = 0, bq = -1; units.forEach((u, i) => { const q = uv[i] / (us[i] + 1); if (q > bq) { bq = q; bi = i; } }); us[bi]++; left--; }
    units.forEach((u, i) => {
      if (u.length === 1) { res[u[0]] = us[i]; return; }
      const [a, b] = u; let k = us[i] - res[a] - res[b];
      while (k-- > 0) { if (votes[a] / (res[a] + 1) >= votes[b] / (res[b] + 1)) res[a]++; else res[b]++; }
    });
    return { seats: res, threshold_votes: min, pairs, void_pairs: voids };
  }

  // ------------------------------------------------------------------ profiles
  function shortHe(name) {
    const s = name.split(/\s+(?:בראשות|בהנהגת|בהובלת|תנועתו של|-|–)\s+/)[0].trim();
    return s.length <= 26 ? s : s.slice(0, 24).trim() + "…";
  }
  class Profile {
    constructor(key, cfg, inherit) { this.key = key; this.cfg = cfg; this.inherit = inherit || null; this.names = {}; this.cache = {}; }
    setNames(n) { this.names = { ...n }; this.cache = {}; }
    party(l) {
      if (this.cache[l]) return this.cache[l];
      const own = this.names[l], baseHe = this.inherit ? this.inherit.names[l] : undefined, he = own || baseHe || l;
      const p = (this.cfg.parties || {})[l]; let out;
      if (p) out = { letter: l, he, name: p.short || he, short_he: p.short_he || shortHe(he), bloc: p.bloc, family: p.family };
      else if (this.inherit && this.cfg.inherit_by_letter && (this.inherit.cfg.parties || {})[l]) {
        const q = this.inherit.cfg.parties[l], same = !own || own === baseHe;
        out = { letter: l, he, name: (same && q.short) || (he !== l ? he : "List " + l), short_he: (same && q.short_he) || (he !== l ? shortHe(he) : "רשימה " + l), bloc: q.bloc, family: q.family, assumed: true };
      } else out = { letter: l, he, name: he !== l ? he : "List " + l, short_he: he !== l ? shortHe(he) : "רשימה " + l, bloc: "X", family: "other", unclassified: true };
      return (this.cache[l] = out);
    }
    family(l) { return this.party(l).family; }
    bloc(l) { return this.party(l).bloc; }
  }
  function cityAgg(row, prof) {
    const c = { code: row.code, name: row.name, committee: row.committee, eligible: row.eligible, voters: row.voters, invalid: row.invalid, valid: row.valid, letters: {}, fam: {}, bloc: {} };
    for (const l in row.votes) { const v = row.votes[l]; if (!v) continue; c.letters[l] = v; add(c.fam, prof.family(l), v); add(c.bloc, prof.bloc(l), v); }
    if (!c.valid) c.valid = Object.values(c.letters).reduce((a, b) => a + b, 0);
    return c;
  }
  const share = (c, d, k) => c.valid ? (d[k] || 0) / c.valid : 0;
  const argmax = o => { let best = null, bv = -Infinity; for (const k in o) if (o[k] > bv) { bv = o[k]; best = k; } return best; };

  // ------------------------------------------------------------------ analyzer
  class Analyzer {
    constructor(cfg, baseBallots, curKey, baseKey, national) {
      this.cfg = cfg;
      this.base = new Profile(baseKey, cfg.elections[baseKey]);
      this.cur = new Profile(curKey, cfg.elections[curKey], this.base);
      if (national) this.base.setNames(Object.fromEntries(national.lists.map(l => [l.letter, l.name])));
      this.baseBallots = {}; this.baseBallotRows = new Map();
      for (const x of baseBallots) { add(this.baseBallots, x.code, 1); this.baseBallotRows.set(x.code + "|" + x.ballot, x); }
      this.growth = +cfg.eligible_growth || 1; this.threshold = +cfg.threshold || 0.0325; this.seats = cfg.seats || 120; this.majority = cfg.majority || 61;
      this.baseCities = new Map([...byCity(baseBallots).values()].map(row => [row.code, cityAgg(row, this.base)]));
      this.sector = new Map([...this.baseCities].map(([code, c]) => [code, this.classify(c)]));
      this.baseTot = this.totals(this.baseCities.values());
      this.baseLetters = sumObjs([...this.baseCities.values()].map(c => c.letters));
      this.baseFam = sumObjs([...this.baseCities.values()].map(c => c.fam));
      this.baseBloc = sumObjs([...this.baseCities.values()].map(c => c.bloc));
      const official = national ? Object.fromEntries(national.lists.filter(l => l.seats != null).map(l => [l.letter, l.seats])) : {};
      if (Object.values(official).reduce((a, b) => a + b, 0) === this.seats) { this.baseSeats = official; this.baseSeatsSource = "official (results site)"; }
      else { this.baseSeats = baderOfer(this.baseLetters, this.base.cfg.agreements, this.seats, this.threshold).seats; this.baseSeatsSource = "computed"; }
      this.baseBlocSeats = {}; this.baseFamSeats = {};
      for (const l in this.baseSeats) { add(this.baseBlocSeats, this.base.bloc(l), this.baseSeats[l]); add(this.baseFamSeats, this.base.family(l), this.baseSeats[l]); }
    }
    classify(c) {
      const sc = this.cfg.sectors || {};
      if ((sc.overrides || {})[c.code]) return sc.overrides[c.code];
      if (c.code === ENV) return "envelopes";
      if (share(c, c.bloc, "A") >= (sc.arab_min_share ?? 0.5)) return "arab";
      if (share(c, c.fam, "utj") + share(c, c.fam, "shas") >= (sc.haredi_min_share ?? 0.5) && share(c, c.fam, "utj") >= (sc.haredi_min_utj ?? 0.2)) return "haredi";
      if (c.eligible >= (sc.large_city_min_eligible ?? 100000)) return "large";
      return "jewish";
    }
    sectorOf(code) { return this.sector.get(code) || "new"; }
    totals(cities) {
      const t = { eligible: 0, voters: 0, invalid: 0, valid: 0, env_voters: 0, env_valid: 0 };
      for (const c of cities) { t.eligible += c.eligible; t.voters += c.voters; t.invalid += c.invalid; t.valid += c.valid; if (c.code === ENV) { t.env_voters += c.voters; t.env_valid += c.valid; } }
      return t;
    }

    analyze(cityRows, ballots, meta, prev) {
      const cur = new Map();
      for (const row of cityRows) { const c = cityAgg(row, this.cur); if (c.voters || c.valid) cur.set(c.code, c); }
      const mode = this.reportingMode(cur), tot = this.totals(cur.values());
      const letters = sumObjs([...cur.values()].map(c => c.letters));
      for (const p in this.cur.cfg.parties || {}) if (!(p in letters)) letters[p] = 0;
      const famNow = sumObjs([...cur.values()].map(c => c.fam)), blocNow = sumObjs([...cur.values()].map(c => c.bloc));
      const valid = tot.valid;
      const counted = {};
      if (ballots) for (const x of ballots) if (x.voters || x.valid) add(counted, x.code, 1);
      const hasCounted = Object.keys(counted).length > 0;
      const rep = new Map();
      for (const [code, c] of cur) {
        const b = this.baseCities.get(code);
        if (!b) rep.set(code, 1);
        else if (hasCounted && this.baseBallots[code]) rep.set(code, Math.min(1, (counted[code] || 0) / this.baseBallots[code]));
        else if (code === ENV || mode === "volume" || !c.eligible) rep.set(code, b.voters ? Math.min(1, c.voters / (b.voters * this.growth)) : 1);
        else rep.set(code, b.eligible ? Math.min(1, c.eligible / (b.eligible * this.growth)) : 1);
      }
      const matched = [...cur.values()].filter(c => this.baseCities.has(c.code) && c.valid);
      const wTot = matched.reduce((a, c) => a + c.valid, 0);
      let lflFam = Object.fromEntries(Object.keys(this.cfg.families).map(f => [f, 0])), lflBloc = Object.fromEntries(Object.keys(this.cfg.blocs).map(k => [k, 0]));
      for (const c of matched) { const b = this.baseCities.get(c.code); for (const f in b.fam) add(lflFam, f, c.valid * b.fam[f] / b.valid); for (const k in b.bloc) add(lflBloc, k, c.valid * b.bloc[k] / b.valid); }
      for (const k in lflFam) lflFam[k] = wTot ? lflFam[k] / wTot : null;
      for (const k in lflBloc) lflBloc[k] = wTot ? lflBloc[k] / wTot : null;
      const mFam = sumObjs(matched.map(c => c.fam)), mBloc = sumObjs(matched.map(c => c.bloc));
      let tn = 0, td = 0, tb = 0;
      for (const c of matched) { const b = this.baseCities.get(c.code); if (c.code === ENV || !c.eligible || !b.eligible) continue; tn += c.voters; td += c.eligible; tb += c.eligible * b.voters / b.eligible; }
      const tNow = pct(tn, td), tBase = pct(tb, td);
      const sectors = this.sectors(cur);
      const projection = this.project(cur, rep, letters);
      const agreements = this.cur.cfg.agreements || [];
      const alloc = baderOfer(letters, agreements, this.seats, this.threshold), seatsNow = alloc.seats;
      const progress = this.progress(cur, ballots, mode, tot);
      const parties = [];
      for (const [l, v] of Object.entries(letters).sort((a, b) => b[1] - a[1])) {
        const p = this.cur.party(l);
        if (v === 0 && !(l in (this.cur.cfg.parties || {}))) continue;
        const fam = p.family, single = Object.keys(letters).filter(x => letters[x] && this.cur.family(x) === fam), pc = pct(v, valid);
        const pr = projection ? projection.letters[l] : null, comparable = fam !== "other" && single.length <= 1;
        parties.push({ letter: l, name: p.name, he: p.he, short_he: p.short_he, bloc: p.bloc, family: fam, minor: !pc || pc < 0.01,
          assumed: !!p.assumed, unclassified: !!p.unclassified, votes: v, pct: r(pc, 5), passes: !!(valid && v >= valid * this.threshold),
          to_threshold: valid ? Math.max(0, Math.trunc(valid * this.threshold - v) + 1) : null, seats: seatsNow[l] || 0,
          proj_pct: pr ? r(pr.pct, 5) : null, proj_seats: pr ? pr.seats : null,
          base_family_pct: comparable ? r(pct(this.baseFam[fam] || 0, this.baseTot.valid), 5) : null,
          lfl_base_pct: comparable ? r(lflFam[fam], 5) : null });
      }
      const families = [];
      for (const [f, fc] of Object.entries(this.cfg.families)) {
        const now = pct(famNow[f] || 0, valid), base = pct(this.baseFam[f] || 0, this.baseTot.valid), mnow = pct(mFam[f] || 0, wTot), lfl = lflFam[f];
        if (!now && !base) continue;
        const pf = projection ? projection.families[f] : null;
        families.push({ key: f, name: fc.name, color: fc.color, letters_now: Object.keys(letters).filter(l => this.cur.family(l) === f && letters[l]),
          letters_base: Object.keys(this.baseLetters).filter(l => this.base.family(l) === f && this.baseLetters[l]),
          votes: famNow[f] || 0, pct: r(now, 5), base_pct: r(base, 5), lfl_base_pct: r(lfl, 5), swing: mnow != null && lfl != null ? r(mnow - lfl, 5) : null,
          seats: Object.entries(seatsNow).filter(([l]) => this.cur.family(l) === f).reduce((a, [, s]) => a + s, 0), base_seats: this.baseFamSeats[f] || 0,
          proj_pct: pf ? r(pf.pct, 5) : null, proj_seats: pf ? pf.seats : null });
      }
      families.sort((a, b) => (b.pct || b.base_pct || 0) - (a.pct || a.base_pct || 0));
      const blocs = Object.entries(this.cfg.blocs).map(([k, bc]) => {
        const now = pct(blocNow[k] || 0, valid), mnow = pct(mBloc[k] || 0, wTot), lfl = lflBloc[k], pb = projection ? projection.blocs[k] : null;
        return { key: k, name: bc.name, color: bc.color, votes: blocNow[k] || 0, pct: r(now, 5), base_pct: r(pct(this.baseBloc[k] || 0, this.baseTot.valid), 5),
          lfl_base_pct: r(lfl, 5), swing: mnow != null && lfl != null ? r(mnow - lfl, 5) : null,
          seats: Object.entries(seatsNow).filter(([l]) => this.cur.bloc(l) === k).reduce((a, [, s]) => a + s, 0), base_seats: this.baseBlocSeats[k] || 0,
          proj_pct: pb ? r(pb.pct, 5) : null, proj_seats: pb ? pb.seats : null };
      });
      const cities = this.cities(cur, rep, mode);
      const national = { eligible_counted: tot.eligible, voters: tot.voters, invalid: tot.invalid, valid, envelopes_voters: tot.env_voters, envelopes_counted: tot.env_voters > 0,
        turnout_raw: r(pct(tot.voters, tot.eligible)), turnout_ordinary: r(pct(tot.voters - tot.env_voters, tot.eligible)),
        turnout_lfl: r(tNow), turnout_lfl_base: r(tBase), turnout_lfl_delta: tNow != null && tBase != null ? r(tNow - tBase) : null,
        invalid_pct: r(pct(tot.invalid, tot.voters)), threshold_votes: Math.trunc(alloc.threshold_votes), lfl_coverage: r(pct(wTot, valid)), reporting_mode: mode };
      const bt = this.baseTot;
      const baseNational = { eligible: bt.eligible, voters: bt.voters, valid: bt.valid, invalid: bt.invalid, turnout: r(pct(bt.voters, bt.eligible)),
        turnout_ordinary: r(pct(bt.voters - bt.env_voters, bt.eligible)), invalid_pct: r(pct(bt.invalid, bt.voters)),
        seats: Object.fromEntries(Object.entries(this.baseSeats).filter(([, s]) => s)), seats_source: this.baseSeatsSource };
      const st = { meta: { ...(meta || {}), analyzed_at: nowIso(), current: { key: this.cur.key, name: this.cur.cfg.name, date: this.cur.cfg.date || null },
          baseline: { key: this.base.key, name: this.base.cfg.name, date: this.base.cfg.date || null }, threshold: this.threshold, seats: this.seats, majority: this.majority },
        progress, national, baseline_national: baseNational, parties, families, blocs, agreements: { active: alloc.pairs, void: alloc.void_pairs },
        sectors, projection: projection && { confidence: projection.confidence, expected_valid: projection.expected_valid }, cities };
      st.insights = this.insights(st);
      st.changes = this.changes(st, prev);
      return st;
    }
    reportingMode(cur) {
      let el = 0, vo = 0; for (const c of cur.values()) { el += c.eligible; if (c.code !== ENV) vo += c.voters; }
      return el > 0.95 * this.baseTot.eligible * this.growth && vo < 0.6 * (this.baseTot.voters - this.baseTot.env_voters) * this.growth ? "volume" : "eligible";
    }
    progress(cur, ballots, mode, tot) {
      const codes = new Set([...this.baseCities.keys(), ...cur.keys()]); codes.delete(ENV);
      const out = { localities_reporting: [...cur.keys()].filter(c => c !== ENV).length, localities_total: codes.size, envelopes_counted: cur.has(ENV) };
      const expEl = this.baseTot.eligible * this.growth, expVo = (this.baseTot.voters - this.baseTot.env_voters) * this.growth;
      out.by_eligible = expEl ? r(Math.min(1, tot.eligible / expEl)) : null;
      out.by_volume = expVo ? r(Math.min(1, (tot.voters - tot.env_voters) / expVo)) : null;
      if (ballots) {
        const counted = ballots.filter(x => x.voters || x.valid).length;
        out.ballots_counted = counted; out.ballots_listed = ballots.length;
        if (ballots.length > counted) { out.ballots_total = ballots.length; out.ballots_total_source = "feed"; }
        else { out.ballots_total = Object.values(this.baseBallots).reduce((a, b) => a + b, 0); out.ballots_total_source = "baseline ballot count (estimate)"; }
      }
      if (out.ballots_total) { out.fraction = r(Math.min(1, out.ballots_counted / out.ballots_total)); out.method = `ballot boxes counted (${out.ballots_total_source})`; }
      else if (mode === "eligible") { out.fraction = out.by_eligible; out.method = "eligible voters of counted ballots vs. register estimate"; }
      else { out.fraction = out.by_volume; out.method = "voters counted vs. baseline volume"; }
      return out;
    }
    sectors(cur) {
      const agg = {};
      const get = s => agg[s] || (agg[s] = { key: s, localities: 0, reporting: 0, base_valid: 0, now_eligible: 0, now_voters: 0, now_valid: 0, lfl_voters_base: 0, bloc_now: {}, bloc_lfl: {}, fam_now: {}, fam_lfl: {} });
      for (const [code, b] of this.baseCities) { const a = get(this.sectorOf(code)); a.localities++; a.base_valid += b.valid; }
      for (const [code, c] of cur) {
        const a = get(this.sectorOf(code)); a.reporting++;
        const b = this.baseCities.get(code); if (!b || !c.valid) continue;
        a.now_eligible += c.eligible; a.now_voters += c.voters; a.now_valid += c.valid;
        if (b.eligible) a.lfl_voters_base += c.eligible * b.voters / b.eligible;
        for (const k in c.bloc) add(a.bloc_now, k, c.bloc[k]);
        for (const k in b.bloc) add(a.bloc_lfl, k, c.valid * b.bloc[k] / b.valid);
        for (const k in c.fam) add(a.fam_now, k, c.fam[k]);
        for (const k in b.fam) add(a.fam_lfl, k, c.valid * b.fam[k] / b.valid);
      }
      const out = [];
      for (const s of ["arab", "haredi", "large", "jewish", "envelopes", "new"]) {
        const a = agg[s]; if (!a) continue;
        const nv = a.now_valid;
        const row = { key: s, name: SECTOR_EN[s], localities: a.localities, reporting: a.reporting, base_valid: a.base_valid, now_valid: nv,
          progress: a.base_valid ? r(Math.min(1, nv / (a.base_valid * this.growth))) : null,
          turnout: s !== "envelopes" ? r(pct(a.now_voters, a.now_eligible)) : null, turnout_base: s !== "envelopes" ? r(pct(a.lfl_voters_base, a.now_eligible)) : null,
          blocs: nv ? Object.fromEntries(Object.keys(this.cfg.blocs).map(k => [k, { pct: r(pct(a.bloc_now[k] || 0, nv)), lfl: r(pct(a.bloc_lfl[k] || 0, nv)) }])) : {},
          families: nv ? Object.fromEntries(Object.keys(this.cfg.families).filter(k => a.fam_now[k] || a.fam_lfl[k]).map(k => [k, { pct: r(pct(a.fam_now[k] || 0, nv)), lfl: r(pct(a.fam_lfl[k] || 0, nv)) }])) : {} };
        if (row.turnout != null && row.turnout_base != null) row.turnout_delta = r(row.turnout - row.turnout_base);
        out.push(row);
      }
      return out;
    }
    project(cur, rep, letters) {
      let validNow = 0; for (const c of cur.values()) validNow += c.valid;
      if (!validNow) return null;
      const L = Object.keys(letters), famLetters = {};
      for (const l of L) (famLetters[this.cur.family(l)] ||= []).push(l);
      const famTot = {}; for (const f in famLetters) famTot[f] = famLetters[f].reduce((a, l) => a + letters[l], 0);
      const split = {}; for (const l of L) { const f = this.cur.family(l); split[l] = famTot[f] ? letters[l] / famTot[f] : 1 / famLetters[f].length; }
      const fam = {}; for (const l of L) fam[l] = this.cur.family(l);
      const est = b => { const o = {}; for (const l of L) o[l] = share(b, b.fam, fam[l]) * split[l]; return o; };
      const natNow = {}, natLfl = {}, secNow = {}, secLfl = {}, secW = {}, tfNum = {}, tfDen = {}; let natW = 0;
      for (const [code, c] of cur) {
        const b = this.baseCities.get(code); if (!b || !c.valid || !b.valid) continue;
        const s = this.sectorOf(code), e = est(b);
        add(secW, s, c.valid); natW += c.valid;
        const sn = secNow[s] ||= {}, sl = secLfl[s] ||= {};
        for (const l of L) { const v = c.letters[l] || 0, x = c.valid * e[l]; add(sn, l, v); add(sl, l, x); add(natNow, l, v); add(natLfl, l, x); }
        if (code !== ENV && b.voters) { add(tfNum, s, c.voters); add(tfDen, s, b.voters * (rep.get(code) ?? 1) * this.growth); }
      }
      if (!natW) return null;
      const natSwing = {}; for (const l of L) natSwing[l] = ((natNow[l] || 0) - (natLfl[l] || 0)) / natW;
      const sumD = Object.values(tfDen).reduce((a, b) => a + b, 0), natTf = sumD ? Object.values(tfNum).reduce((a, b) => a + b, 0) / sumD : 1;
      const swings = {}, tfs = {};
      for (const s of new Set(this.sector.values())) {
        const w = secW[s] || 0, k = w / (w + SHRINK), o = {};
        for (const l of L) o[l] = k * (w ? (((secNow[s] || {})[l] || 0) - ((secLfl[s] || {})[l] || 0)) / w : 0) + (1 - k) * natSwing[l];
        swings[s] = o;
        const d = tfDen[s] || 0, kk = d / (d + SHRINK); tfs[s] = kk * (d ? (tfNum[s] || 0) / d : 1) + (1 - kk) * natTf;
      }
      const pv = {}; for (const l of L) pv[l] = letters[l];
      let expected = validNow;
      for (const [code, b] of this.baseCities) {
        const c = cur.get(code), rr = c ? (rep.get(code) ?? 0) : 0;
        if (rr >= 0.99 || !b.valid) continue;
        const s = this.sectorOf(code), tf = code === ENV ? natTf : tfs[s];
        const remaining = Math.max(0, b.valid * this.growth * tf - (c ? c.valid : 0));
        if (!remaining) continue;
        const e = est(b), sw = code === ENV ? natSwing : swings[s], sh = {}; let t = 0;
        for (const l of L) { sh[l] = Math.max(0, e[l] + sw[l]); t += sh[l]; }
        if (!t) continue;
        for (const l of L) pv[l] += remaining * sh[l] / t;
        expected += remaining;
      }
      const total = Object.values(pv).reduce((a, b) => a + b, 0);
      const seats = baderOfer(pv, this.cur.cfg.agreements || [], this.seats, this.threshold).seats;
      const fams = {}, blocs = {};
      for (const l of L) {
        const f = fams[fam[l]] ||= { votes: 0, seats: 0 }, b = blocs[this.cur.bloc(l)] ||= { votes: 0, seats: 0 };
        f.votes += pv[l]; f.seats += seats[l] || 0; b.votes += pv[l]; b.seats += seats[l] || 0;
      }
      for (const d of [fams, blocs]) for (const k in d) d[k].pct = total ? d[k].votes / total : 0;
      const sc = expected ? validNow / expected : 0;
      return { letters: Object.fromEntries(L.map(l => [l, { votes: pv[l], pct: total ? pv[l] / total : 0, seats: seats[l] || 0 }])), families: fams, blocs,
        confidence: sc >= 0.6 ? "high" : sc >= 0.25 ? "medium" : "low", expected_valid: Math.trunc(expected) };
    }
    cities(cur, rep, mode) {
      const out = [], codes = new Set([...this.baseCities.keys(), ...cur.keys()]), blocKeys = Object.keys(this.cfg.blocs);
      for (const code of codes) {
        const c = cur.get(code), b = this.baseCities.get(code), ref = c || b;
        const row = { code, name: ref.name, committee: ref.committee, sector: this.sectorOf(code), reporting: c ? r(rep.get(code) ?? 0, 3) : 0 };
        if (b) {
          const bw = argmax(b.letters);
          Object.assign(row, { base_eligible: b.eligible, base_voters: b.voters, base_valid: b.valid, base_turnout: r(pct(b.voters, b.eligible)),
            base_winner: bw, base_winner_family: bw ? this.base.family(bw) : null, base_winner_pct: bw ? r(share(b, b.letters, bw)) : null,
            base_blocs: Object.fromEntries(blocKeys.map(k => [k, r(share(b, b.bloc, k))])) });
        }
        if (c && c.valid) {
          const w = argmax(c.letters);
          Object.assign(row, { eligible: c.eligible, voters: c.voters, valid: c.valid, invalid: c.invalid, turnout: mode === "eligible" ? r(pct(c.voters, c.eligible)) : null,
            winner: w, winner_family: w ? this.cur.family(w) : null, winner_pct: w ? r(share(c, c.letters, w)) : null,
            blocs: Object.fromEntries(blocKeys.map(k => [k, r(share(c, c.bloc, k))])),
            top: Object.keys(c.letters).sort((x, y) => c.letters[y] - c.letters[x]).slice(0, 4).map(l => [l, r(share(c, c.letters, l))]) });
          if (b) {
            if (row.turnout != null && row.base_turnout != null) row.turnout_delta = r(row.turnout - row.base_turnout);
            row.bloc_swing = Object.fromEntries(blocKeys.map(k => [k, r(share(c, c.bloc, k) - share(b, b.bloc, k))]));
            const keys = new Set([...Object.keys(c.fam), ...Object.keys(b.fam)]);
            row.family_swing = Object.fromEntries([...keys].map(f => [f, share(c, c.fam, f) - share(b, b.fam, f)]).sort((x, y) => Math.abs(y[1]) - Math.abs(x[1])).slice(0, 5).map(([f, v]) => [f, r(v)]));
            row.flipped = !!(w && row.base_winner_family && this.cur.family(w) !== row.base_winner_family);
          }
        }
        out.push(row);
      }
      return out.sort((x, y) => (y.base_eligible || y.eligible || 0) - (x.base_eligible || x.eligible || 0));
    }
    cityDetail(curMap, code) {
      const b = this.baseCities.get(code), row = curMap && curMap.get(code), c = row ? cityAgg(row, this.cur) : null;
      if (!b && !c) return null;
      const fams = [];
      for (const [f, fc] of Object.entries(this.cfg.families)) {
        const now = c && c.valid ? share(c, c.fam, f) : null, base = b ? share(b, b.fam, f) : null;
        if (!now && !base) continue;
        fams.push({ key: f, name: fc.name, color: fc.color, pct: r(now), base_pct: r(base), delta: now != null && base != null ? r(now - base) : null });
      }
      fams.sort((x, y) => (y.pct || y.base_pct || 0) - (x.pct || x.base_pct || 0));
      const lst = (a, prof, lim) => Object.entries(a.letters).sort((x, y) => y[1] - x[1]).slice(0, lim).map(([l, v]) => ({ ...prof.party(l), votes: v, pct: r(share(a, a.letters, l)) }));
      return { code, name: (c || b).name, sector: SECTOR_EN[this.sectorOf(code)], sector_key: this.sectorOf(code), base_ballots: this.baseBallots[code] || null,
        now: c && { eligible: c.eligible, voters: c.voters, invalid: c.invalid, valid: c.valid, letters: lst(c, this.cur) },
        base: b && { eligible: b.eligible, voters: b.voters, invalid: b.invalid, valid: b.valid, turnout: r(pct(b.voters, b.eligible)), letters: lst(b, this.base, 20) },
        families: fams };
    }
    ballotSummary(x, prof) {
      const letters = {}; for (const l in x.votes) if (x.votes[l]) letters[l] = x.votes[l];
      const top = Object.keys(letters).sort((a, b) => letters[b] - letters[a]), blocs = {};
      for (const l in letters) add(blocs, prof.bloc(l), letters[l]);
      return { ballot: x.ballot, eligible: x.eligible, voters: x.voters, invalid: x.invalid, valid: x.valid, turnout: r(pct(x.voters, x.eligible)),
        winner: top[0] || null, winner_pct: top.length ? r(pct(letters[top[0]], x.valid)) : null, top: top.slice(0, 3).map(l => [l, r(pct(letters[l], x.valid))]),
        blocs: Object.fromEntries(Object.entries(blocs).map(([k, v]) => [k, r(pct(v, x.valid))])) };
    }
    cityBallots(ballots, code) {
      const now = new Map((ballots || []).filter(x => x.code === code).map(x => [x.ballot, x]));
      const base = new Map(); for (const [k, x] of this.baseBallotRows) if (x.code === code) base.set(x.ballot, x);
      const key = b => { const f = parseFloat(b); return Number.isFinite(f) && String(f) === String(parseFloat(b)) ? [0, f] : [1, String(b)]; };
      const all = [...new Set([...now.keys(), ...base.keys()])].sort((a, b) => { const x = key(a), y = key(b); return x[0] - y[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0); });
      const out = all.map(b => { const row = { ballot: b }, n = now.get(b); if (n && (n.voters || n.valid)) row.now = this.ballotSummary(n, this.cur); if (base.has(b)) row.base = this.ballotSummary(base.get(b), this.base); return row; });
      return { code, ballots: out, counted: out.filter(x => x.now).length, base_total: base.size, has_feed: !!ballots };
    }
    ballotDetail(ballots, code, ballot) {
      const x = (ballots || []).find(y => y.code === code && y.ballot === ballot), b = this.baseBallotRows.get(code + "|" + ballot);
      if (!x && !b) return null;
      const lst = (row, prof) => Object.entries(row.votes).filter(([, v]) => v).sort((p, q) => q[1] - p[1]).map(([l, v]) => ({ ...prof.party(l), votes: v, pct: r(pct(v, row.valid)) }));
      return { code, ballot, name: (x || b).name, now: x && { ...this.ballotSummary(x, this.cur), letters: lst(x, this.cur) }, base: b && { ...this.ballotSummary(b, this.base), letters: lst(b, this.base) } };
    }
    partyDetail(curMap, letter) {
      if (!curMap) return null;
      const p = this.cur.party(letter), fam = p.family, cities = [], bySec = {}; let totV = 0, totValid = 0;
      for (const [code, row] of curMap) {
        if (!row.valid) continue;
        const v = row.votes[letter] || 0; totV += v; totValid += row.valid;
        const b = this.baseCities.get(code), baseFam = b && fam !== "other" ? share(b, b.fam, fam) : null;
        let nf = 0; for (const l in row.votes) if (this.cur.family(l) === fam) nf += row.votes[l]; nf /= row.valid;
        const sec = this.sectorOf(code), a = bySec[sec] ||= { key: sec, votes: 0, valid: 0, lfl: 0 };
        a.votes += v; a.valid += row.valid; if (baseFam != null) a.lfl += row.valid * baseFam;
        cities.push({ code, name: row.name, sector: sec, valid: row.valid, votes: v, pct: r(v / row.valid), family_pct: r(nf), base_family_pct: r(baseFam), delta: baseFam != null ? r(nf - baseFam) : null });
      }
      const hist = new Array(20).fill(0);
      for (const c of cities) if (c.code !== ENV && c.valid >= 200) hist[Math.min(19, Math.trunc((c.pct || 0) * 20))]++;
      const big = cities.filter(c => c.valid >= 2000), withD = big.filter(c => c.delta != null);
      return { ...p, votes: totV, pct: r(pct(totV, totValid), 5), family_name: (this.cfg.families[fam] || {}).he,
        sectors: Object.entries(bySec).sort((x, y) => y[1].votes - x[1].votes).map(([k, a]) => ({ key: k, name: SECTOR_HE[k] || k, votes: a.votes, pct: r(pct(a.votes, a.valid)),
          share_of_party: r(pct(a.votes, totV)), lfl_family_pct: a.valid && fam !== "other" ? r(a.lfl / a.valid) : null })),
        strongest: [...big].sort((x, y) => (y.pct || 0) - (x.pct || 0)).slice(0, 25), most_votes: [...cities].sort((x, y) => y.votes - x.votes).slice(0, 25),
        gains: [...withD].sort((x, y) => y.delta - x.delta).slice(0, 15), losses: [...withD].sort((x, y) => x.delta - y.delta).slice(0, 15), histogram: hist, cities: cities.length };
    }
    search(parties, q) {
      q = q.trim(); if (!q) return [];
      const out = [];
      for (const [code, c] of this.baseCities) if (c.name.includes(q) || String(code) === q) out.push({ type: "city", code, name: c.name, eligible: c.eligible });
      out.sort((a, b) => (!a.name.startsWith(q)) - (!b.name.startsWith(q)) || b.eligible - a.eligible);
      const letters = new Set([...Object.keys(this.cur.cfg.parties || {}), ...Object.keys(this.cur.names), ...(parties || [])]);
      const ps = [...letters].filter(l => { const p = this.cur.party(l); return l.includes(q) || p.he.includes(q) || p.name.toLowerCase().includes(q.toLowerCase()); })
        .map(l => ({ type: "party", letter: l, name: this.cur.party(l).short_he, he: this.cur.party(l).he }));
      return ps.slice(0, 8).concat(out.slice(0, 20));
    }
    insights(st) {
      const ins = [], pg = st.progress, nat = st.national, famHe = {}, blocHe = {};
      for (const [k, v] of Object.entries(this.cfg.families)) famHe[k] = v.he || v.name;
      for (const [k, v] of Object.entries(this.cfg.blocs)) blocHe[k] = v.he || v.name;
      const a = (kind, level, text) => ins.push({ kind, level, text });
      if (!nat.valid) { a("status", "info", "טרם פורסמו תוצאות. המערכת ממשיכה לבדוק את האתר."); return ins; }
      a("status", "info", `נספרו כ-${((pg.fraction || 0) * 100).toFixed(1)}% מהקולות; ${fmt(pg.localities_reporting)} מתוך ${fmt(pg.localities_total)} יישובים דיווחו; מעטפות חיצוניות ${pg.envelopes_counted ? "נספרו" : "טרם נספרו"}.`);
      if (nat.turnout_lfl_delta != null) { const d = nat.turnout_lfl_delta;
        a("turnout", d >= 0.001 ? "up" : d <= -0.001 ? "down" : "info", `שיעור ההצבעה ביישובים שדיווחו: ${(nat.turnout_lfl * 100).toFixed(1)}% לעומת ${(nat.turnout_lfl_base * 100).toFixed(1)}% באותם יישובים בבחירות הקודמות (${sgn(d)} נק׳ אחוז).`); }
      for (const s of st.sectors) if (s.turnout_delta != null && s.now_valid > 2000 && Math.abs(s.turnout_delta) >= 0.02)
        a("sector", s.turnout_delta > 0 ? "up" : "down", `${SECTOR_HE[s.key] || s.name}: הצבעה ${(s.turnout * 100).toFixed(1)}% לעומת ${(s.turnout_base * 100).toFixed(1)}% (${sgn(s.turnout_delta)} נק׳), ${s.reporting}/${s.localities} יישובים.`);
      const n = st.blocs.find(b => b.key === "N");
      if (n) a("seats", "key", `${blocHe.N}: ${n.seats} מנדטים לפי הקולות שנספרו${n.proj_seats != null ? `, ${n.proj_seats} בתחזית` : ""} (בבחירות הקודמות ${n.base_seats}; רוב = ${this.majority}).`);
      for (const b of st.blocs) if (b.swing != null && Math.abs(b.swing) >= 0.01) a("swing", b.swing > 0 ? "up" : "down", `${blocHe[b.key]} ${sgn(b.swing)} נק׳ לעומת אותם יישובים בבחירות הקודמות (${(b.pct * 100).toFixed(1)}% כעת).`);
      const fams = st.families.filter(f => f.swing != null);
      if (fams.length && Math.max(...fams.map(f => Math.abs(f.swing))) >= 0.001) {
        const g = fams.reduce((x, y) => y.swing > x.swing ? y : x), l = fams.reduce((x, y) => y.swing < x.swing ? y : x);
        a("swing", "up", `המתחזקת ביותר: ${famHe[g.key]} ${sgn(g.swing)} נק׳ בהשוואה זהה.`);
        a("swing", "down", `הנחלשת ביותר: ${famHe[l.key]} ${sgn(l.swing)} נק׳ בהשוואה זהה.`);
      }
      for (const p of st.parties) { if (p.pct == null || p.pct < 0.025 || p.pct > 0.04) continue;
        a("threshold", "warn", p.passes ? `${p.short_he} (${p.letter}) מעל אחוז החסימה בפער קטן: ${(p.pct * 100).toFixed(2)}%.` : `${p.short_he} (${p.letter}) מתחת לאחוז החסימה: ${(p.pct * 100).toFixed(2)}%, חסרים ${fmt(p.to_threshold)} קולות.`); }
      if (st.agreements.void.length) a("seats", "info", "הסכמי עודפים שבוטלו (שותפה מתחת לחסימה): " + st.agreements.void.map(p => p.join("–")).join(", "));
      const solid = st.cities.filter(c => c.valid && (c.base_eligible || 0) >= 5000 && (c.reporting || 0) >= 0.8 && c.sector !== "envelopes");
      const tc = solid.filter(c => c.turnout_delta != null);
      const up = tc.filter(c => c.turnout_delta >= 0.005).sort((x, y) => y.turnout_delta - x.turnout_delta).slice(0, 5);
      const dn = tc.filter(c => c.turnout_delta <= -0.005).sort((x, y) => x.turnout_delta - y.turnout_delta).slice(0, 5);
      if (up.length) a("cities", "up", "העלייה הגדולה בהצבעה: " + up.map(c => `${c.name} ${sgn(c.turnout_delta)}`).join(", "));
      if (dn.length) a("cities", "down", "הירידה הגדולה בהצבעה: " + dn.map(c => `${c.name} ${sgn(c.turnout_delta)}`).join(", "));
      const flips = solid.filter(c => c.flipped);
      if (flips.length) a("cities", "key", `${flips.length} יישובים גדולים החליפו את המפלגה המובילה, למשל: ` + flips.slice(0, 6).map(c => `${c.name} (${famHe[c.base_winner_family]} ← ${famHe[c.winner_family]})`).join(", "));
      const mx = c => Object.entries(c.bloc_swing).reduce((x, y) => Math.abs(y[1] || 0) > Math.abs(x[1] || 0) ? y : x);
      const sw = solid.filter(c => c.bloc_swing && Math.abs(mx(c)[1] || 0) >= 0.01).sort((x, y) => Math.abs(mx(y)[1]) - Math.abs(mx(x)[1])).slice(0, 5);
      if (sw.length) a("cities", "info", "התזוזות הגדולות בין גושים: " + sw.map(c => `${c.name} (${blocHe[mx(c)[0]]} ${sgn(mx(c)[1])})`).join(", "));
      if (nat.invalid_pct != null) a("turnout", "info", `קולות פסולים: ${(nat.invalid_pct * 100).toFixed(2)}% מהמצביעים (בבחירות הקודמות ${(st.baseline_national.invalid_pct * 100).toFixed(2)}%).`);
      return ins;
    }
    changes(st, prev) {
      if (!prev) return { first: true };
      const pc = new Map(prev.cities.filter(c => c.valid).map(c => [c.code, c]));
      const nw = st.cities.filter(c => c.valid && !pc.has(c.code)), grew = st.cities.filter(c => c.valid && pc.has(c.code) && c.valid > pc.get(c.code).valid);
      const pp = new Map(prev.parties.map(p => [p.letter, p])), moves = [];
      for (const p of st.parties) { const o = pp.get(p.letter); if (o && p.pct != null && o.pct != null) moves.push({ letter: p.letter, name: p.name, delta: r(p.pct - o.pct, 5), seats_delta: p.seats - o.seats }); }
      moves.sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta));
      return { first: false, since: prev.meta.analyzed_at, votes_added: st.national.valid - prev.national.valid,
        new_localities: [...nw].sort((x, y) => (y.valid || 0) - (x.valid || 0)).slice(0, 15).map(c => c.name), new_localities_count: nw.length, new_codes: nw.slice(0, 300).map(c => c.code),
        updated_localities_count: grew.length, party_moves: moves.slice(0, 8) };
    }
  }

  // ------------------------------------------------------------------ mock live simulation (port of simulate.LiveSim)
  const SPEEDS = [500, 1000, 2000, 5000, 7000];
  const FAMILY_F = { likud: 0.88, lapid: 1.04, far_right: 1.12, gantz: 1.18, left: 1.25, shas: 0.97, utj: 1.03, yb: 1.22, raam: 1.08, joint: 0.95, right_alt: 0.6 };
  const TURNOUT_F = { arab: 1.07, haredi: 1.02, large: 0.98, jewish: 1, envelopes: 1.05 };
  function rng(seed) { let a = seed >>> 0; const u = () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
    const gauss = (m, s) => { let x = 0, y = 0; while (!x) x = u(); while (!y) y = u(); return m + s * Math.sqrt(-2 * Math.log(x)) * Math.cos(2 * Math.PI * y); };
    return { u, uniform: (a, b) => a + (b - a) * u(), gauss, logn: (m, s) => Math.exp(gauss(m, s)) }; }
  class LiveSim {
    constructor(src, parties, family, sectorOf, days = 5, speed = 100, seed = 26) {
      this.parties = parties; this.duration = days * 86400; this.speed = speed;
      const R = rng(seed), noise = new Map(), size = {};
      for (const x of src) add(size, x.code, x.eligible);
      this.rows = src.map(x => {
        let cn = noise.get(x.code);
        if (!cn) { cn = { t: R.gauss(0, 0.04), f: Object.fromEntries(parties.map(p => [p, R.logn(0, 0.12)])) }; noise.set(x.code, cn); }
        const sector = sectorOf(x.code), eligible = Math.round(x.eligible * 1.06);
        const turnout = (x.eligible ? x.voters / x.eligible : 0) * (TURNOUT_F[sector] || 1) * (1 + cn.t);
        const voters = x.code === ENV ? Math.round(x.voters * TURNOUT_F.envelopes) : Math.min(eligible, Math.round(eligible * turnout));
        const invalid = Math.round(voters * (x.voters ? x.invalid / x.voters : 0.006)), valid = Math.max(0, voters - invalid);
        const w = {}; let tw = 0;
        for (const p of parties) { w[p] = (x.votes[p] || 0) * (FAMILY_F[family(p)] || 1) * cn.f[p] * R.logn(0, 0.05); tw += w[p]; }
        const votes = {};
        if (tw) { let s = 0; for (const p of parties) { votes[p] = Math.floor(valid * w[p] / tw); s += votes[p]; }
          parties.slice().sort((a, b) => (valid * w[b] / tw - votes[b]) - (valid * w[a] / tw - votes[a])).slice(0, valid - s).forEach(p => votes[p]++); }
        else for (const p of parties) votes[p] = 0;
        return { committee: x.committee, name: x.name, code: x.code, ballot: x.ballot, eligible, voters, invalid, valid, votes };
      });
      const ranked = Object.keys(size).map(Number).sort((a, b) => size[a] - size[b]), rank = {};
      ranked.forEach((c, i) => rank[c] = i / Math.max(1, ranked.length - 1));
      const start = {}, span = {};
      for (const c of ranked) { start[c] = R.uniform(0, 0.35) * (0.3 + rank[c]); span[c] = R.uniform(0.02, 0.15) + 0.35 * rank[c]; }
      this.when = this.rows.map(x => (x.code === ENV ? R.uniform(0.55, 1) : Math.min(0.98, start[x.code] + R.u() * span[x.code]) ** 2) * this.duration);
      this.order = this.rows.map((_, i) => i).sort((a, b) => this.when[a] - this.when[b]);
      this.reset();
    }
    reset() { this.simSeconds = 0; this.reported = 0; this.running = true; this.last = performance.now(); this.cities = new Map(); this.ballots = []; }
    advance() {
      const now = performance.now();
      if (this.running) this.simSeconds = Math.min(this.duration, this.simSeconds + (now - this.last) / 1000 * this.speed);
      this.last = now;
      let changed = false;
      while (this.reported < this.order.length && this.when[this.order[this.reported]] <= this.simSeconds) {
        const x = this.rows[this.order[this.reported++]]; changed = true;
        this.ballots.push(x);
        const c = this.cities.get(x.code);
        if (!c) this.cities.set(x.code, { committee: x.committee, name: x.name, code: x.code, eligible: x.eligible, voters: x.voters, invalid: x.invalid, valid: x.valid, votes: { ...x.votes } });
        else { c.eligible += x.eligible; c.voters += x.voters; c.invalid += x.invalid; c.valid += x.valid; for (const p in x.votes) c.votes[p] += x.votes[p]; }
      }
      return changed;
    }
    control(speed, action) {
      this.advance();
      if (speed != null && SPEEDS.includes(+speed)) this.speed = +speed;
      if (action === "pause") this.running = false; else if (action === "resume") this.running = true; else if (action === "reset") this.reset();
      return this.status();
    }
    status() { return { sim_seconds: Math.trunc(this.simSeconds), duration: this.duration, speed: this.speed, running: this.running, finished: this.reported >= this.rows.length,
      ballots_reported: this.reported, ballots_total: this.rows.length, speeds: SPEEDS }; }
    clock() { const s = Math.trunc(this.simSeconds) + 22 * 3600; return `יום ${Math.floor(s / 86400) + 1} · ${String(Math.floor(s % 86400 / 3600)).padStart(2, "0")}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}`; }
  }

  // ------------------------------------------------------------------ the in-page "server"
  const M = { ready: null, an: null, sim: null, state: null, history: [], curMap: null, ballots: null, parties: [], mode: "sim", status: null, lastWork: 0 };
  const cfgP = fetch("data/config.json").then(x => x.json());
  M.ready = (async () => { await 0;
    const [cfg, national, expb] = await Promise.all([cfgP, fetch("data/national.json").then(x => x.json()), fetch("data/expb.csv").then(x => x.text())]);
    const base = parseResults(expb);
    M.cfg = cfg;
    M.an = new Analyzer(cfg, base.rows, cfg.current, cfg.baseline, national);
    M.parties = base.parties;
    M.sim = new LiveSim(base.rows, base.parties, l => M.an.base.family(l), c => M.an.sectorOf(c), 5, 100);
    M.status = { started: nowIso(), polls: 0, changes: 0, last_poll: null, last_change: null, last_error: null, expc_url: "סימולציה בדפדפן (נתוני דמה)",
      expb_url: "סימולציה בדפדפן (נתוני דמה)", poll_seconds: 1, candidates: { expc: [], expb: [] }, site: cfg.elections[cfg.current].site, site_status: null, site_totals: null, site_error: null };
    tick(); setInterval(tick, 1000);
  })();
  function ingest(cityRows, ballots, meta) {
    const prev = M.state;
    const st = M.an.analyze(cityRows, ballots, meta, prev);
    const entry = { t: st.meta.analyzed_at, vt: st.meta.sim_clock || null, progress: st.progress.fraction, valid: st.national.valid, turnout_lfl: st.national.turnout_lfl,
      turnout_lfl_base: st.national.turnout_lfl_base, blocs: Object.fromEntries(st.blocs.map(b => [b.key, [b.pct, b.seats, b.proj_seats]])),
      parties: Object.fromEntries(st.parties.filter(p => (p.pct || 0) >= 0.005).map(p => [p.letter, [p.pct, p.seats, p.proj_seats]])) };
    M.history.push(entry); if (M.history.length > 500) M.history.shift();
    st.history = M.history;
    st.meta.hash = String(M.status.changes + 1);
    M.state = st; M.curMap = new Map(cityRows.map(x => [x.code, x])); M.ballots = ballots;
    M.status.changes++; M.status.last_change = nowIso();
  }
  function tick() {
    M.status.polls++; M.status.last_poll = nowIso();
    if (M.mode !== "sim") return;
    const changed = M.sim.advance();
    // throttle heavy analysis: at most every 1.5 s
    if (changed && performance.now() - M.lastWork > 1500) {
      M.lastWork = performance.now();
      ingest([...M.sim.cities.values()], M.sim.ballots, { source: "simulation", simulation: true, sim_clock: M.sim.clock(), encoding: "utf-8" });
    } else if (changed) setTimeout(() => { if (performance.now() - M.lastWork > 1400) tick(); }, 1600);
  }
  function reset() { M.state = null; M.history = []; M.curMap = null; M.ballots = null; }

  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  async function handle(url, opts) {
    await M.ready;
    const u = new URL(url, location.href), path = u.pathname.replace(/^.*?(\/api\/)/, "/api/"), method = (opts && opts.method) || "GET";
    let m;
    if (path === "/api/state") return json(M.state || { empty: true, status: M.status, meta: { current: M.cfg.current, baseline: M.cfg.baseline } });
    if (path === "/api/status") return json({ ...M.status, sim: M.mode === "sim" ? M.sim.status() : null });
    if (path === "/api/config") {
      const letters = new Set([...Object.keys(M.an.cur.cfg.parties || {}), ...Object.keys(M.an.cur.names), ...M.parties]);
      return json({ families: M.cfg.families, blocs: M.cfg.blocs, current: Object.fromEntries([...letters].map(l => [l, M.an.cur.party(l)])),
        baseline: Object.fromEntries(Object.keys(M.an.baseLetters).map(l => [l, M.an.base.party(l)])) });
    }
    if (path === "/api/sim") {
      if (method === "POST") {
        const body = opts && opts.body ? JSON.parse(opts.body) : {};
        if (M.mode !== "sim") { M.mode = "sim"; body.action = "reset"; }
        const st = M.sim.control(body.speed, body.action);
        if (body.action === "reset") reset();
        M.lastWork = 0; tick();
        return json(st);
      }
      return json(M.sim.status());
    }
    if ((m = path.match(/^\/api\/city\/(\d+)\/ballots$/))) return json(M.an.cityBallots(M.ballots, +m[1]));
    if ((m = path.match(/^\/api\/ballot\/(\d+)\/([^/]+)$/))) { const d = M.an.ballotDetail(M.ballots, +m[1], decodeURIComponent(m[2])); return d ? json(d) : json({ error: "not found" }, 404); }
    if ((m = path.match(/^\/api\/party\/([^/]+)$/))) { const d = M.an.partyDetail(M.curMap, decodeURIComponent(m[1])); return d ? json(d) : json({ error: "no data" }, 404); }
    if (path === "/api/search") return json(M.an.search(M.parties, u.searchParams.get("q") || ""));
    if ((m = path.match(/^\/api\/city\/(\d+)$/))) { const d = M.an.cityDetail(M.curMap, +m[1]); return d ? json(d) : json({ error: "not found" }, 404); }
    if (path === "/api/refresh") { M.lastWork = 0; tick(); return json({ ok: true }); }
    if (path === "/api/upload") {
      // analyse a real expc.csv / expb.csv downloaded from the results site (stops the simulation)
      try {
        const buf = opts.body, bytes = new Uint8Array(buf instanceof ArrayBuffer ? buf : await new Response(buf).arrayBuffer());
        let text; try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch (e) { text = new TextDecoder("windows-1255").decode(bytes); }
        const f = parseResults(text);
        M.mode = "file"; M.sim.control(null, "pause"); reset();
        const ballots = f.kind === "ballot" ? f.rows : null, cities = [...byCity(f.rows).values()];
        M.status.expc_url = M.status.expb_url = "קובץ שהועלה";
        ingest(cities, ballots, { source: "upload", encoding: "utf-8" });
        return json({ ok: true });
      } catch (e) { return json({ error: String(e.message || e) }, 400); }
    }
    return json({ error: "not found" }, 404);
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => (typeof url === "string" && url.startsWith("/api/")) ? handle(url, opts) : realFetch(url, opts);
  window.__electionEngine = M;
  M.parseResults = parseResults; M.norm = norm; M.META = META;
})();
