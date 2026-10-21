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

## The new site

`config.json` assumes the new site follows the same pattern: `votes26.bechirot.gov.il` and `media26.bechirot.gov.il/files/expc.csv` / `expb.csv`. These are **guesses**: on 21 Oct 2026 both answered 404. To cope with different main or sub links, the monitor:

1. tries every candidate URL and remembers the one that works,
2. scans the site's pages (`discover_pages`) for links to `exp*.csv`, including `window.open(...)` links like on votes25, and repeats this every 10 failed polls,
3. reads the site's national page every few polls for the official list names and the status headline,
4. accepts manual files: put `expc.csv` / `expb.csv` into `data/inbox/` or use **Upload CSV** in the dashboard.

If the new site uses different addresses, put them in `config.json` → `elections.26.site`, `sources`, `discover_pages`.

## What it shows

* **National**: counting progress (ballot boxes counted from `expb.csv`), valid votes, turnout, invalid ballots, threshold, seats per list (Bader-Ofer).
* **Like-for-like comparison**: during the count, every share and turnout figure is compared with the *same localities* in 2022, weighted by what has been counted so far.
* **Projection**: votes still expected in each locality are estimated from its 2022 result plus the swing in its sector. Seats are then allocated. Confidence is labelled low/medium/high.
* **Per locality**: share counted, turnout now vs. 2022, leader now vs. 2022 (flags changes), bloc shares and swings. Click a locality for every list now vs. 2022.
* **Sectors** (derived from the 2022 result): Arab & Bedouin, Haredi, large cities, other Jewish localities, double envelopes. Each shows turnout and bloc swing.
* **Insights**, **since-last-update** panel, **timeline**, CSV export, archive of every version of the feed.

## Analysis assumptions you should check (`config.json`)

These are my classifications, not data from the site:
* **Blocs** of the 2022 lists (`N` Netanyahu bloc, `O` Jewish opposition, `A` Arab parties, `X` other).
* **Families**, which group lists so they can be compared across elections.
* **Sectors** thresholds.
* **2026 lists**: none are configured. A 2026 list whose ballot letter existed in 2022 is assumed to be the same list (bloc and family copied) and is marked *assumed by letter* in the dashboard. New letters appear as *unclassified*. Add entries under `elections.26.parties` to classify them correctly.
* **Surplus agreements** for 2026 (`elections.26.agreements`): empty. Add them if you want exact seat counts.
* `eligible_growth` (1.06): expected growth of the voter register since 2022. It's only used when `expb.csv` is not available.

## Run

```bash
python3 -m election_monitor serve              # election night: 26th Knesset vs. 2022 → http://localhost:8025
python3 -m election_monitor probe              # which result URLs / site pages respond from this machine
python3 -m election_monitor sync --election 25 # (re)download the 2022 site data
python3 -m election_monitor --current 25 --baseline 25 serve --simulate --sim-steps 40 --sim-step-seconds 5
                                               # rehearsal: replays the 2022 count ballot by ballot
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
election_monitor/simulate.py  ballot-by-ballot replay of a final expb.csv
static/index.html             dashboard
data/baseline/k25/            2022 data downloaded from votes25.bechirot.gov.il
tests/                        tests; fixtures are pages saved from votes25.bechirot.gov.il
```
