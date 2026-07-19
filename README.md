# Prodigy Fire Weather Morning Brief

Pulls **NFDRS fire danger outputs** (Spread Component, Energy Release Component,
Burning Index) from the U.S. Forest Service **Fire Environment Mapping System
(FEMS)** for RAWS stations grouped by **Geographic Area Coordination Center
(GACC)** across the West Coast and Rocky Mountains, then emails a formatted
morning briefing to the sales team.

FEMS is the official NFDRS system that replaced WIMS. Its public read-only API
serves current data (5 days back + 7-day forecast) with no login for short
windows — which is exactly what this tool uses.

---

## What the brief contains

- **One table per coordination center** (NWCC, ONCC, OSCC, GBCC, NRCC, RMCC),
  each row listing **SC**, **ERC**, and **BI** for a station.
- A **"Highest fire danger this morning"** summary ranking the hottest stations
  across every center by Burning Index.
- **BI trend** vs. the prior day and the **7-day forecast peak BI** per station.
- Optional **Low → Extreme** color chips where you supply station percentile
  breakpoints (see `thresholds` in `config.yaml`).
- **Evacuation Orders** — active NWS evacuation alerts, with fire name when
  the alert states it (see below).
- **Active Incidents (InciWeb)** — a linked list of actively-updated named
  fires in your monitored states, from the free national InciWeb feed (see
  below).

---

## 1. Install

```bash
pip install -r requirements.txt
```
Requires Python 3.9+.

## 2. Pick your stations (one-time)

The API identifies stations by a numeric FEMS **station id** (e.g. `101222`).
To find the ids for the stations you care about:

1. Open <https://fems.fs2c.usda.gov/download>
2. In **Selected Station(s)**, search by station name, station ID, or WRCC ID.
3. Set Data Subject Area = **Fire Danger**, then click **Copy Data Link**.
4. In the copied URL, read the value after `stationIds=` — those numbers are the
   ids. Paste them into `config.yaml`, replacing every `REPLACE_ME_#`.

Then confirm they resolve:
```bash
python fire_weather_brief.py --verify
```
This prints each station with its latest SC / ERC / BI (or an error) without
sending anything.

> Tip: for a sales briefing, pick a few representative RAWS in each GACC — ideally
> near your key accounts or the Fire Danger Rating Areas you sell into. Set each
> center's `fuel_models` to the model named in that area's Fire Danger Operating
> Plan (Y = timber is the common default; SoCal chaparral often uses X).

## 3. No email needed — the brief publishes to a URL instead

By default this tool never sends email. It builds `brief.html` and (via the
included GitHub Actions workflow) publishes it to **GitHub Pages**, so you get
a bookmarkable link instead of an inbox — no SMTP account, app password, or
secrets to configure.

**Rich link previews (iMessage, Slack, etc.).** Set `site.public_url` in
`config.yaml` to your GitHub Pages URL (`https://<username>.github.io/<repo>/`)
and shared links show a title + description + logo card instead of a plain
link. Leave it blank to skip — nothing guesses the URL for you. The workflow
already copies `assets/logo.png` next to `index.html` so the preview image
resolves.

## 4. Test it

```bash
python fire_weather_brief.py --dry-run     # builds brief.html + prints text, no email
```
Open `brief.html` in a browser to preview it.

---

## 5. Schedule it every morning

### Option A — GitHub Actions + GitHub Pages (recommended; runs even when your laptop is off)
1. Put this folder in a GitHub repo.
2. Repo **Settings → Pages → Build and deployment → Source** → select
   **"GitHub Actions"** → Save. (One-time.)
3. The included `.github/workflows/fire-weather-brief.yml` runs daily at 8 AM
   Pacific, builds the brief, and publishes it to
   `https://<your-github-username>.github.io/<repo-name>/`. Edit the `cron:`
   line to change the time. (Pinned to 8 AM, not 6, so the National Sitrep
   Summary box has that day's NICC report available — it publishes ~0730 MDT
   / ~0630 Pacific.)

### Option B — cron (Mac/Linux, machine must be on)
```bash
crontab -e
# 6:00 AM daily — adjust path and python as needed:
0 6 * * *  cd /path/to/tool && /usr/bin/python3 fire_weather_brief.py --dry-run >> brief.log 2>&1
```
This regenerates `brief.html` locally each morning; open it in a browser.

### Option C — Windows Task Scheduler
Create a Basic Task → Daily → 6:00 AM → Action "Start a program":
- Program: `python`
- Arguments: `fire_weather_brief.py --dry-run`
- Start in: the tool folder

---

## Optional: send by email instead

If you'd rather have the brief emailed than published to a link, the original
email path still works:

1. Edit the `email:` block in `config.yaml` (sending account; recipient is
   already set to michael@prodigywildfire.com). The password is **never
   stored in the file** — it's read from an environment variable:
   ```bash
   export FEMS_SMTP_PASSWORD='your-app-password'
   ```
   - **Google Workspace** (`smtp.gmail.com`): create an *App Password*
     (myaccount.google.com → Security → 2-Step Verification → App passwords).
   - **Microsoft 365** (`smtp.office365.com`): use an app password / SMTP AUTH
     account.
2. Run `python fire_weather_brief.py` (no `--dry-run`) to build and email it.
3. For GitHub Actions, add secret `FEMS_SMTP_PASSWORD` under **Settings →
   Secrets and variables → Actions**, then change the workflow's build step to
   run `python fire_weather_brief.py` instead of the `--dry-run` + Pages steps.

**PDF attachment.** When sending by email (not `--dry-run`), the brief is also
rendered to a PDF and attached (`fire_weather_brief_YYYY-MM-DD.pdf`), using a
headless Chromium via [Playwright](https://playwright.dev/python/) — no
Homebrew or system libraries required, just a one-time browser download:
```bash
pip install -r requirements.txt        # installs the playwright package
playwright install chromium            # one-time: downloads the headless browser
```
This is best-effort: if Playwright or its browser isn't installed, the email
still sends fine, just without the PDF attachment (a note prints to the
console). If you switch GitHub Actions from the default dry-run+Pages flow to
actually emailing, add a `playwright install --with-deps chromium` step to the
workflow before the build step so the PDF attaches there too.

---

## Field reference

| Field | Meaning |
|------|---------|
| **SC** — Spread Component | rate-of-spread potential (open-ended) |
| **ERC** — Energy Release Component | available energy / drought signal (slow-moving) |
| **BI** — Burning Index | ~ effort to contain; roughly 10× estimated flame length |
| **100-hr FM** — 100-hr dead fuel moisture | % water in branch/limb-sized dead fuel (~1-3" diameter); lower = drier |
| **1000-hr FM** — 1000-hr dead fuel moisture | % water in log-sized dead fuel (~3-8" diameter); lower = drier, slow-moving like ERC |

Values shown are the **daily maximum** for each index (FEMS daily-summary feed).

## Notes & limits
- No-login API requests are limited to short windows; this tool uses the rolling
  "5 days back + 7-day forecast" preset, which stays well inside that limit.
- Absolute index values are **not comparable between stations** without
  station-specific percentiles — that's why adjective ratings appear only where
  you configure breakpoints.
- FEMS occasionally revises endpoints. If a fetch starts failing, regenerate a
  Copy Data Link from the FEMS download page and compare the query parameters in
  `fetch_nfdr_csv()`.
- Data source: USFS Fire Environment Mapping System (FEMS),
  `https://fems.fs2c.usda.gov`.


---

## What's new: weather + significant fire potential

**Weather columns.** Each station row now also shows **Min RH** (daily minimum
relative humidity), **Wind** and **Gust** (daily max mph), pulled from the FEMS
weather feed. RH ≤ 15% or wind ≥ 25 mph is flagged red. Turn this off with
`weather: {enabled: false}` in `config.yaml`.

**Fuel moisture columns.** Each station row also shows **100-hr FM** and
**1000-hr FM** — the percentage of water in medium and large dead fuel (branches,
logs). Lower % = drier = more available to burn. These come from the same FEMS
fire-danger feed already being fetched, so there's no extra API call. Turn this
off with `fuel_moisture: {enabled: false}`. Note: FEMS only provides
1-hr/10-hr/100-hr/1000-hr timelag classes — there's no "10,000-hr" class in
NFDRS, so it isn't (and can't be) included.

**Significant Fire Potential section.** At the top of the brief:
- **Live NWS alerts** — active **Red Flag Warnings** and **Fire Weather Watches**
  for the states in `significant_fire_potential.states`, grouped by state, from
  the official `api.weather.gov` feed (no key needed). NWS requires a contact in
  the request header; the tool uses `contact_email` from the config.
- **Predictive Services 7-day outlook links** — the national viewer plus each
  GACC's 7-Day Significant Fire Potential page, for the forecaster narrative that
  can't be reduced to a number, plus a direct link to the **National Sitrep
  (PDF)** from NIFC (`nifc.gov/nicc-files/sitreprt.pdf`, always the latest one).

Both degrade gracefully: if the alert feed is briefly unreachable, the brief
still sends with the fire danger tables and outlook links, noting the feed was
unavailable.

**Evacuation Orders box.** Right above Significant Fire Potential, a red box
shows active evacuation-order alerts (`Evacuation Immediate` / `Civil
Emergency Message`) for the same states, from the same `api.weather.gov` feed.
Turn it off with `evacuation_orders: { enabled: false }`. Each alert shows the
affected **area** (NWS alert zones are usually county-level, not exact city
names) plus, **only when the issuing agency's alert text explicitly states
it**, the **fire name**. Most evacuation alerts don't name the fire at all —
that's a real gap in the source alert, not a bug here, so don't expect it to
be populated every time. There's no free public feed with structured
city/fire-name data (tools like Genasys Protect/Zonehaven exist but aren't
open APIs), so this is the best available free/keyless signal. Degrades the
same way as the alerts above:
a feed hiccup just shows "feed unavailable" without blocking the rest of the
brief, and most mornings this box will simply read "No active evacuation
orders" (a good thing, not a broken feature).

**Live updates.** This box alone refreshes itself every time the page is
opened or reloaded — it fetches the latest NWS alerts directly in your
browser, so it's never more than page-load-fresh, not just once-a-day-fresh
like the rest of the brief. A small "live as of HH:MM" note appears once that
refresh completes. If the live check fails for any reason (offline, feed
hiccup), the box just keeps showing the snapshot from that morning's build —
nothing breaks either way. (Every other section still only updates on the
daily 8 AM build.)

**Active Incidents (InciWeb) box.** Below Significant Fire Potential, a blue
box lists actively-updated named fires in your monitored states, sorted
alphabetically by state and laid out in two columns, pulled from InciWeb
(`inciweb.wildfire.gov`) — the free, keyless, national system fire incident
PIOs post directly to. Turn it off with
`active_incidents: { enabled: false }`. Each entry links straight to that
fire's official InciWeb page. **Why this exists:** not every county's
evacuation order gets relayed through NWS/IPAWS (the source the Evacuation
Orders box above uses), so a real, active evacuation can sometimes go
unreported there — this box is a free complement, giving you a quick way to
click through to a fire's official page and check for evacuation detail
yourself. It is *not* a structured evacuation feed itself: whether an
incident's InciWeb page mentions evacuations at all is up to that incident's
PIO. Any incident whose InciWeb overview text mentions evacuations gets a red
**EVAC** badge and is never dropped from the list, even on a day with more
than 15 active incidents nationwide — everything else is capped at 15,
most-recently-updated first. A fire without the badge may still have an
active evacuation its PIO simply didn't mention in the overview text.
Degrades the same way as every other box — a feed hiccup just shows
"feed unavailable," and a quiet stretch with no actively-updated incidents in
your states will just read "No actively-updated named incidents."

**National Sitrep Summary box.** Below the SC/ERC/BI legend, a gold summary
box pulls the headline numbers off page 1 of the same daily NICC Incident
Management Situation Report PDF linked above (parsed with `pdfplumber`):
National Preparedness Level, initial attack activity, new/contained/
uncontained large fires, and a one-line PL / incident count / cumulative
acres readout for each of your configured GACCs. Turn it off with
`national_sitrep: { enabled: false }` in `config.yaml`. Best-effort: if NICC
changes the report's layout, this box is silently skipped — the National
Sitrep link itself still works either way.

**Daily quote.** A short motivational line appears at the very bottom of the
brief, picked deterministically from a fixed list so it's stable for the day
but rotates day to day. Cosmetic only — no config toggle.


---

## Overnight movement (no chart)

Earlier versions rendered a pre-rendered PNG bar chart of Burning Index change
vs. yesterday. It's been removed in favor of the **National Sitrep (PDF)** link
in the Significant Fire Potential box (see below) — the official NICC daily
sitrep is a more authoritative one-glance read on where things stand
nationally. The BI trend arrow (per-station table) and the plain-text
"Overnight movement" list still show each station's day-over-day change.

---

## Keeping the schedule alive (the 60-day thing)

GitHub disables scheduled workflows after 60 days with no commits to the default
branch — and workflow runs, tags, and releases don't count as commits. The
included `.github/workflows/keepalive.yml` prevents this by making one tiny
commit every Monday, which resets the clock for the whole repo. It sustains
itself, so once it's in place the morning brief runs indefinitely untouched.

**One-time setting so it can push its commit:** Repo **Settings -> Actions ->
General -> Workflow permissions -> "Read and write permissions" -> Save.** After
that, open the **Actions** tab, pick **Keepalive**, and hit **Run workflow** once
to confirm it can commit successfully.
