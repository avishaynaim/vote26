# Knesset Live Election Monitor

Real-time monitor for Israeli Knesset election results. It polls the Central Elections Committee's results website, compares every locality with the previous election (2022, 25th Knesset) and serves a live dashboard.

Pure Python 3.10+ standard library. No dependencies.

## The results site (analysed on https://votes25.bechirot.gov.il/)

| Page / file | Content |
|---|---|
| `/` = `/nationalresults` | national totals (eligible, voters, turnout, valid, invalid) and all 40 lists: full name, ballot letters, seats, %, votes. A heading gives the status ("תוצאות סופיות רשמיות" = official final results). |
| `/cityresults?cityID=<locality code>` | the same tables for one locality |
| `/ballotresults?cityID=<code>&BallotNumber=<n>` | the same for one ballot box (heading: polling place, ballot serial "ברזל" number) |
| `https://media25.bechirot.gov.il/files/expc.csv` | download link on every page: one row per locality (1,216 rows) |
| `https://media25.bechirot.gov.il/files/expb.csv` | download link on every page: one row per ballot box (12,545 rows, of which 838 are double-envelope ballots) |

* `expc.csv` columns: `סמל ועדה, שם ישוב, סמל ישוב, בזב, מצביעים, פסולים, כשרים` (committee, locality, locality code, eligible, voters, invalid, valid), then one column per ballot letter.
* `expb.csv` adds `ברזל` (ballot serial), `קלפי` (ballot number), `ריכוז`, `שופט`.
* Double envelopes (soldiers, diplomats, hospitals, prisons) are locality code `9999`.
* Both files are UTF-8 with BOM. windows-1255 is also handled.
* Checked against each other: the CSV totals equal the national page for every list, `expb.csv` summed per locality equals `expc.csv`, and the Tel Aviv city page and a ballot page equal their CSV rows. These checks are tests in `tests/`.
* The site does **not** publish the surplus-vote agreements. Seats computed without them differ from the official 2022 seats by one seat for two lists (Likud 31 instead of 32, Labor 5 instead of 4). For the baseline the monitor therefore uses the **official seats from the national page**.

`data/baseline/k25/` contains `expc.csv`, `expb.csv` and `national.json` (the parsed national page), downloaded from the site with `python3 -m election_monitor sync --election 25`.

## Which site is monitored

By default the app monitors **https://votes26.bechirot.gov.il/** — the expected URL for the 26th Knesset results, following the Central Elections Committee's existing `votesNN.bechirot.gov.il` pattern. The site goes online a few days before election day; the discovery pipeline below picks up CSV links from it automatically once it's up. Set `elections.live.site` (or pass `--site URL`) if the real host turns out to differ.

1. at start (and every 10 failed polls) it reads the site's home and `nationalresults` pages and takes the `expc.csv` / `expb.csv` links from them, exactly as the votes25 pages link to `media25.bechirot.gov.il/files/…`,
2. every few polls it reads the national page for the official list names and the status headline,
3. manual fallback: put `expc.csv` / `expb.csv` into `data/inbox/` or use **Upload CSV** in the dashboard.

When the new site is published (expected to work like votes25, possibly with different main/sub links), change `site` in `config.json` or run `python3 -m election_monitor --site <new link> serve`. Comparisons are always against the 2022 data in `data/baseline/k25/`.

## Quick start

```bash
git clone https://github.com/avishaynaim/vote26 && cd vote26
python3 -m election_monitor serve --simulate      # mock live election, see the dashboard update
# open http://localhost:8025
python3 -m election_monitor serve                 # real monitoring of the configured site
```

## The dashboard (Hebrew, right-to-left, light/dark, phone friendly)

Every view has its own link (`#/party/מחל`, `#/city/5000`, `#/ballot/5000/1`) and updates live.

| Tab | What it shows |
|---|---|
| סקירה (Overview) | ballot boxes counted, valid votes, turnout vs. the same localities in 2022, seats; **Knesset seating chart** (counted / projected / 2022); blocs now vs. 2022; all lists with threshold line; insights; what changed since the last update; the big cities |
| מפלגות (Parties) | every list: votes, %, seats, projection, change vs. 2022, distance to the threshold; threshold-watch chart; cross-election families. **Party page**: results by sector, distribution across localities, strongest localities, biggest gains/losses vs. 2022 |
| יישובים (Localities) | scatter of turnout change vs. bloc swing for every locality; sortable/filterable table (sector, counted, changed leader, size). **Locality page**: like the site's city page plus 2022 comparison, families now vs. then, and **every ballot box** |
| קלפיות (Ballot boxes) | like the site's ballot search (locality + ballot) with the same ballot in 2022 |
| מגזרים (Sectors) | turnout and bloc/family swing in Arab, Haredi, large-city and other localities and double envelopes |
| מנדטים וקואליציה | **coalition builder** (click parties, majority meter) and a **seat calculator** to try surplus-vote agreements (the site does not publish them) |
| לאורך הספירה (Timeline) | projected seats, party shares and counting pace over the night |
| מקור הנתונים (Source) | which site/files are polled, status, errors |

Global search (press `/`), toasts on every update, newly reported localities flash.

## Simulation (mock live data flow)

`python3 -m election_monitor serve --simulate [--speed 100] [--days 5]`

* Mock results = the 2022 ballot-box results from the site with random changes (per family, per sector turnout, per locality and per ballot noise; fixed seed). They are clearly labelled as a simulation in the dashboard.
* Every ballot box gets its own arrival time over **5 simulated days**: small localities first, big cities over a longer period, double envelopes on days 3-5.
* The files are written exactly like the site's `expc.csv` / `expb.csv`, and the monitor polls them every second, so the whole pipeline runs as on election night.
* A control bar in the dashboard switches speed **×2 ×5 ×10 ×30 ×50 ×100 ×1000**, pauses, or restarts the simulation and shows the simulated clock and time left (×1000 runs the 5 days in about 7 minutes).

## What is analysed

* **National**: counting progress (ballot boxes counted from `expb.csv`), valid votes, turnout, invalid ballots, threshold, seats per list (Bader-Ofer).
* **Like-for-like comparison**: during the count, every share and turnout figure is compared with the *same localities* in 2022, weighted by what has been counted so far.
* **Projection**: votes still expected in each locality are estimated from its 2022 result plus the swing in its sector. Seats are then allocated. Confidence is labelled low/medium/high.
* **Per locality**: share counted, turnout now vs. 2022, leader now vs. 2022 (flags changes), bloc shares and swings. Click a locality for every list now vs. 2022.
* **Sectors** (derived from the 2022 result): Arab & Bedouin, Haredi, large cities, other Jewish localities, double envelopes. Each shows turnout and bloc swing.
* **Insights**, **since-last-update** panel, **timeline**, CSV export, archive of every version of the feed.

## Hosted version (no server)

`web/engine.js` is a JavaScript port of the engine (analysis, Bader-Ofer seats, the 5-day mock simulation). It answers the dashboard's `/api/...` requests inside the browser, so the same `static/index.html` runs as a static page with no server. That makes it possible to host on a claude.ai Artifact or GitHub Pages.

```bash
python3 tools/build_web.py      # writes web/dist/ (index.html, engine.js, data/)
python3 -m http.server --directory web/dist 8040   # try it locally
```

* It runs the simulation on the 2022 data from the site. You can also upload a real `expc.csv` / `expb.csv` with **Upload CSV**.
* It cannot poll the live site, because browsers block cross-site requests. For live monitoring on election night use the Python server.
* The JS engine was checked against the Python engine on the full 2022 data. Every national, party, family, bloc, sector, projection and locality value matches.

## Analysis assumptions you should check (`config.json`)

These are my classifications, not data from the site:
* **Blocs** of the 2022 lists (`N` Netanyahu bloc, `O` Jewish opposition, `A` Arab parties, `X` other).
* **Families**, which group lists so they can be compared across elections.
* **Sectors** thresholds.
* **Lists of the monitored site** that are not in config: a list whose ballot letter existed in 2022 is assumed to be the same list (bloc and family copied) and is marked *assumed by letter* in the dashboard. New letters appear as *unclassified*. Add entries under `elections.live.parties` to classify them correctly.
* **Surplus agreements** of the monitored election (`elections.live.agreements`): empty. Add them if you want exact seat counts.
* `eligible_growth` (1.06): expected growth of the voter register since 2022. It's only used when `expb.csv` is not available.

## Run

```bash
python3 -m election_monitor serve              # monitor votes25 (default) vs. 2022 → http://localhost:8025
python3 -m election_monitor --site <new link> serve   # monitor the new site when it is published
python3 -m election_monitor probe              # which result URLs / site pages respond from this machine
python3 -m election_monitor sync --election 25 # (re)download the 2022 site data
python3 -m election_monitor serve --simulate    # mock live election with speed controls (see above)
python3 -m election_monitor serve --file ~/Downloads/expc.csv    # watch a local file
python3 -m unittest discover -s tests
```

Options: `--port`, `--host 0.0.0.0` (open on your network), `--poll SECONDS`.

The site may block requests from abroad or from cloud servers, so run it from a normal connection in Israel.

## API

`GET /api/state`, `/api/city/<code>`, `/api/status`, `/api/config`, `/api/export/cities.csv`; `POST /api/refresh`, `POST /api/upload?kind=expc|expb`.

## Files

```
election_monitor/site.py      reads the results site: national/city/ballot pages, CSV links, sync
election_monitor/feed.py      downloads the CSVs (ETag/If-Modified-Since, encoding) and parses them
election_monitor/seats.py     Bader-Ofer seat allocation
election_monitor/analysis.py  like-for-like comparison, sectors, projection, localities, insights
election_monitor/server.py    polling loop, snapshots/history, HTTP API
election_monitor/simulate.py  mock live election (LiveSim, speed controls) and a simple replay
static/index.html             dashboard (served by the Python server, or with web/engine.js as a static page)
web/engine.js                 in-browser engine (JS port) for the hosted version; tools/build_web.py builds web/dist
data/baseline/k25/            2022 data downloaded from votes25.bechirot.gov.il
tests/                        tests; fixtures are pages saved from votes25.bechirot.gov.il
```
