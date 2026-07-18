# CLAUDE.md — project context for Claude Code

This file orients any Claude Code session working on this project. Read it first,
then `README.md` for the full human-facing setup guide.

## What this project is

A single Python tool that emails a **fire weather morning brief** to the Prodigy
sales team. It pulls NFDRS fire danger + fire weather from the USFS **FEMS** API
and active fire alerts from the **NWS** API, for RAWS stations grouped by
Geographic Area Coordination Center (GACC) across the West Coast and Rockies,
then renders an HTML/plain-text email and sends it. Recipient:
`michael@prodigywildfire.com`.

## Files

- `fire_weather_brief.py` — the whole program (fetch, parse, render, email, chart).
- `config.yaml` — all user settings. **This is the only file the user normally edits.**
- `requirements.txt` — `requests`, `PyYAML`, `playwright` (PDF-of-the-brief
  export, optional at runtime), `pdfplumber` (parses the NICC sitrep PDF for the
  National Sitrep Summary box — unrelated to the Playwright PDF export).
- `.github/workflows/fire-weather-brief.yml` — daily 6 AM Pacific run (GitHub Actions).
- `.github/workflows/keepalive.yml` — weekly commit so the schedule isn't auto-disabled after 60 days.
- `README.md` — full setup, scheduling, and field reference for the user.

## How to run / test

```bash
pip install -r requirements.txt
export FEMS_SMTP_PASSWORD='...'                 # only needed for a real send
python fire_weather_brief.py --verify           # check which station IDs resolve (safe, no email)
python fire_weather_brief.py --dry-run          # build brief.html + print text, NO email
python fire_weather_brief.py                    # build and send the email
python fire_weather_brief.py --config other.yaml --out preview.html
```

**Live internet is required** — the tool hits `fems.fs2c.usda.gov` and
`api.weather.gov`. In restricted/offline sandboxes those calls fail (that's
expected); test parsing/rendering with synthetic CSV/JSON instead of assuming the
code is broken.

## The one thing the user must set (in config.yaml)

1. **Station IDs.** Every `REPLACE_ME_*` under `coordination_centers[].stations[].id`
   must become a real numeric FEMS station id. To find one: open
   https://fems.fs2c.usda.gov/download , search a station, click **Copy Data
   Link**, and read the number after `stationIds=` in the URL. Or query FEMS's
   own GraphQL station-metadata API directly (`POST
   https://fems.fs2c.usda.gov/api/climatology/graphql`, operation
   `GetStationMetaDataSearch` with `returnAll: true`) to search/filter by name
   or state without the UI. `--verify` confirms IDs resolve.

Email delivery is **not** the default path — the daily GitHub Actions workflow
runs `fire_weather_brief.py --dry-run` and publishes `brief.html` to GitHub
Pages instead, so no SMTP secret is required. The `email:` block in
`config.yaml` and `send_email()` still exist and work if the user opts back
into emailing (see README "Optional: send by email instead"), but don't assume
it's wired up by default.

## Data sources (all keyless, public)

- FEMS fire danger: `GET https://fems.fs2c.usda.gov/api/ext-climatology/download-nfdr-daily-summary/`
- FEMS weather: `GET https://fems.fs2c.usda.gov/api/ext-climatology/download-wx-daily-summary/`
  - shared params: `stationIds` (CSV), `dataset` (observation|forecast|all),
    `presetDate=-5Days7Days` (rolling window, always current), `dataFormat=csv`,
    and `fuelModels` (NFDR only).
- NWS alerts: `GET https://api.weather.gov/alerts/active?area=<states>` — filtered
  in code to `event in {"Red Flag Warning","Fire Weather Watch"}`. NWS requires a
  descriptive `User-Agent` with a contact; the tool builds it from
  `significant_fire_potential.contact_email`.
- NWS evacuation alerts: same endpoint/params as above, filtered instead to
  `event in {"Evacuation Immediate","Civil Emergency Message"}`
  (`EVAC_ALERT_EVENTS`) — issued by local emergency management via IPAWS, not
  a dedicated evacuation feed. Fetched as a separate request
  (`fetch_evacuation_alerts()`) rather than reusing `fetch_fire_alerts()`'s
  response, so this box degrades independently if one call fails but not the
  other.

## Key implementation notes

- NFDR CSV fields used: `ERC`, `SC`, `BI` (as of 2026-07 the live feed uses these
  short header names, not the older long forms like `energyReleaseComponent` —
  matched via `_col_exact()` since substring matching would also catch
  `MaxERCTime`/`MaxSCTime`/`MaxBITime`), `100HrFM`/`1000HrFM` (dead fuel
  moisture %, matched the same way), `NFDRType` (`O`=observed else forecast),
  `observationTime`, `stationName`. `-999` is FEMS's missing-data sentinel →
  treated as None. The feed also has `1HrFM`/`10HrFM`/`KBDI`/`GSI`/`WoodyFM`/
  `HerbFM` if more fields are ever wanted — note NFDRS has no "10,000-hr" fuel
  class, so don't invent one if asked.
- Column matching is **loose/defensive**: `_col_exact()` tries an exact
  case-insensitive name match first (for short codes), falling back to `_col()`
  which squashes case/underscores and substring-matches long-form names (can
  prefer `min`/`max`). This combo survives FEMS header changes in either
  direction. Weather picks **min** RH and **max** wind/gust.
- Per-station brief shows: latest observed SC/ERC/BI, BI Δ vs. the prior observed
  day, 7-day forecast peak BI, (if `weather.enabled`) Min RH / Wind / Gust, and
  (if `fuel_moisture.enabled`) 100-hr/1000-hr dead fuel moisture % — the latter
  comes free from the same NFDR fetch, no extra API call needed.
- Adjective ratings (Low→Extreme chips) only render where the user provides
  per-station percentile breakpoints in `thresholds:` — absolute NFDR values are
  NOT comparable across stations, so never invent thresholds.
- No trend chart: an earlier version rendered a matplotlib PNG bar chart of BI
  change vs. yesterday, embedded inline via CID. It was removed (per-station
  BI trend arrow + the plain-text "Overnight movement" list already cover this,
  and `predictive_services_links` now includes a link to NIFC's National
  Sitrep PDF instead). `collect_movers()` still exists and feeds the plain-text
  section — don't reintroduce `matplotlib` to `requirements.txt` unless a chart
  is explicitly requested again.
- Logo: `assets/logo.png` (recolored so its text is white, for the current
  black header) is embedded the same way — CID in email, data-URI in preview —
  via `logo_bytes`/`logo_preview_src`/`logo_email_src` in `main()`.
- PDF attachment: `render_pdf()` uses Playwright's headless Chromium (chosen
  over WeasyPrint since it needs no system Pango/Cairo libraries, just
  `playwright install chromium`) to render `preview_html` — the base64-data-URI
  version, not `email_html`, since a standalone browser page can't resolve
  `cid:` references — to PDF bytes, only when actually emailing (never during
  `--dry-run`). It's wrapped in try/except and returns `None` on any failure
  (missing package, missing browser binary, render error), so a missing/broken
  Playwright install never blocks the email from sending — it just sends
  without the attachment and prints a console note. `send_email()`'s
  `pdf_bytes`/`pdf_filename` params wrap the existing `alternative`/`related`
  body in an outer `mixed` MIME container (attachments must live in `mixed`,
  not `alternative`).
- Everything degrades gracefully: a failed weather fetch, alert fetch, or PDF
  render never blocks the core danger tables / email from sending.
- Evacuation Orders box: `build_evacuations()` reuses
  `significant_fire_potential.states`/`contact_email` (same monitored
  footprint) but is toggled independently via `evacuation_orders.enabled`.
  Rendered by `render_evac_html()`/inline in `render_text()`, positioned
  right after the "Highest fire danger" box and before Significant Fire
  Potential — evacuations are the most acute/actionable signal, so they lead.
  `_extract_fire_name()`/`_extract_headcount()` are regex best-effort scans of
  each alert's headline+description text (`_FIRE_NAME_RE`/`_HEADCOUNT_RE`);
  both are commonly `None` since neither is a structured CAP field — never
  backfill/guess either one, a blank field means the alert didn't state it,
  not that parsing failed. No free public feed has structured city/fire-name/
  evacuee-count data (Genasys Protect/Zonehaven etc. aren't open APIs), so
  this NWS-alert-text scan is the best available free/keyless source.
- Evacuation Orders live refresh: unlike every other box (which only updates
  on the once-daily GitHub Actions build), the Evacuation Orders box also
  refreshes itself client-side on every page load, since evacuation status is
  the most time-critical signal in the brief. `render_evac_html()` appends a
  `<script>` block (`_EVAC_LIVE_SCRIPT_TEMPLATE`, with a `__STATES_JSON__`
  placeholder substituted via `json.dumps(states)`) that independently
  `fetch()`es `api.weather.gov/alerts/active` straight from the browser —
  confirmed viable via `curl -I -H "Origin: ..." ".../alerts/active?area=CA"`
  returning `access-control-allow-origin: *`. The JS is a hand-mirrored port
  of `fetch_evacuation_alerts()`/`_extract_fire_name()`/`_extract_headcount()`
  (same event filter, same regexes) — keep the two in sync if either changes.
  On success it replaces `#fw-evac-body`'s innerHTML and shows a "live as of
  HH:MM" badge (`#fw-evac-live-badge`); on failure (offline, CORS hiccup, JS
  disabled) it silently no-ops and leaves the server-rendered snapshot from
  the last daily build in place — same degrade-gracefully pattern as
  everything else. Browsers don't let `fetch()` set a custom `User-Agent`, so
  unlike the Python side this relies on NWS not strictly requiring one for
  browser-originated requests. Deliberately scoped to this box only, not
  Significant Fire Potential (Red Flag Warning/Fire Weather Watch) — the
  once-a-day refresh is considered sufficient there.
- National Sitrep Summary box: `fetch_sitrep_pdf()` downloads the same NICC
  sitrep PDF linked in `predictive_services_links` (`SITREP_URL`), and
  `parse_sitrep_summary()` uses `pdfplumber` to regex-extract page-1 headline
  numbers (National Preparedness Level, initial attack level/count, new/
  contained/uncontained large fires) plus a per-GACC `PL / incidents /
  cumulative acres` row for each configured GACC code, matched against the
  report's fixed-width summary table. `build_sitrep_summary()` (called from
  `main()`) wraps fetch+parse in try/except and honors
  `national_sitrep.enabled` (default true) — returns `None` on any failure
  (network error, changed PDF layout, missing `pdfplumber`), which silently
  skips the whole box; never invent/guess sitrep numbers if parsing fails.
  Rendered by `render_sitrep_html()` (HTML/PDF) and inline in `render_text()`
  (plain text), positioned **after** the SC/ERC/BI legend paragraph and
  **before** the daily quote. The box has both `fw-box` and `fw-sitrep-box`
  CSS classes — the extra class exists solely so the `@media print` block can
  give it a bigger top margin (`margin-top: 24px !important`) without being
  clobbered by the generic `.fw-box` print rule's `margin: 0 0 10px
  !important` (same-specificity longhand-after-shorthand override); don't
  collapse these two classes back into one without re-checking that the PDF
  export still shows the extra spacing.
- Link previews: `render_html()`'s `public_url` param (sourced from
  `site.public_url` in config.yaml, `None`/unset by default) drives a
  `<title>` + Open Graph + Twitter Card `<meta>` block in `<head>`, so sharing
  the published GitHub Pages link in iMessage/Slack/etc. shows a rich card
  instead of a plain link. `og:image`/`twitter:image` point at
  `{public_url}/logo.png` — link-preview crawlers need a real HTTP(S) URL, not
  the `data:` URI used for the on-page logo, so `fire-weather-brief.yml`'s
  build step also copies `assets/logo.png` next to `public/index.html`. Only
  wired into `preview_html` (the file actually published), not `email_html` —
  no reason for an email body to carry OG tags. `None`/blank `public_url`
  skips the whole block except a plain `<title>`; never fabricate a URL.
- Daily quote: `MOTIVATIONAL_QUOTES` (a fixed list of short original lines)
  and `daily_quote(on_date)` — picks
  `MOTIVATIONAL_QUOTES[on_date.timetuple().tm_yday % len(MOTIVATIONAL_QUOTES)]`,
  so it's stable for a given day and rotates daily with no external state.
  Rendered as the very last element in both `render_html()` and
  `render_text()` — keep it last if the sitrep box is ever repositioned again.

## Common tasks

- **Add a station:** add an `{ id: "<fems id>", name: "<STATION NAME>" }` under the
  right center in `config.yaml`. Optional `name` should match the FEMS
  `stationName` so rows line up.
- **Add a coordination center (e.g. Southwest AZ/NM):** append a new
  `coordination_centers` entry (`code`, `name`, `fuel_models`, `stations`) and add
  its states to `significant_fire_potential.states`.
- **Change fuel model:** per-center `fuel_models` (V/W/X/Y/Z); use the model from
  that area's Fire Danger Operating Plan (Y=timber default, X=chaparral common in SoCal).
- **Turn off weather columns:** `weather: { enabled: false }`.
- **Turn off fuel moisture columns:** `fuel_moisture: { enabled: false }`.
- **Turn off the National Sitrep Summary box:** `national_sitrep: { enabled: false }`.
- **Turn off the Evacuation Orders box:** `evacuation_orders: { enabled: false }`.
- **Enable rich link previews:** set `site.public_url` to the GitHub Pages URL.
- **Change send time:** edit the `cron` hour in `fire-weather-brief.yml`
  (has a `timezone:` field pinned to America/Los_Angeles).

## Guardrails

- Don't put the SMTP password in `config.yaml` or commit it; it stays in the env var / a GitHub secret.
- Don't fabricate FEMS station IDs or `thresholds` — look them up or ask the user.
- Keep changes working offline-testable: prefer defensive parsing over hard-coding exact column names.
