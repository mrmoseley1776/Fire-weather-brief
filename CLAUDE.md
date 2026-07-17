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
- `requirements.txt` — `requests`, `PyYAML`, `matplotlib`.
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

## Key implementation notes

- NFDR CSV fields used: `ERC`, `SC`, `BI` (as of 2026-07 the live feed uses these
  short header names, not the older long forms like `energyReleaseComponent` —
  matched via `_col_exact()` since substring matching would also catch
  `MaxERCTime`/`MaxSCTime`/`MaxBITime`), `NFDRType` (`O`=observed else forecast),
  `observationTime`, `stationName`. `-999` is FEMS's missing-data sentinel →
  treated as None.
- Column matching is **loose/defensive**: `_col_exact()` tries an exact
  case-insensitive name match first (for short codes), falling back to `_col()`
  which squashes case/underscores and substring-matches long-form names (can
  prefer `min`/`max`). This combo survives FEMS header changes in either
  direction. Weather picks **min** RH and **max** wind/gust.
- Per-station brief shows: latest observed SC/ERC/BI, BI Δ vs. the prior observed
  day, 7-day forecast peak BI, and (if `weather.enabled`) Min RH / Wind / Gust.
- Adjective ratings (Low→Extreme chips) only render where the user provides
  per-station percentile breakpoints in `thresholds:` — absolute NFDR values are
  NOT comparable across stations, so never invent thresholds.
- Trend chart: `build_trend_chart()` (matplotlib) → diverging "BI vs. yesterday"
  bars, embedded **inline via CID** in email and as a data-URI in the preview.
  It's optional: if matplotlib is missing it returns None and the brief still
  sends. A station appears only once it has both a yesterday and today value.
- Everything degrades gracefully: a failed weather fetch, alert fetch, or chart
  never blocks the core danger tables from sending.

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
- **Change send time:** edit the `cron` hour in `fire-weather-brief.yml`
  (has a `timezone:` field pinned to America/Los_Angeles).

## Guardrails

- Don't put the SMTP password in `config.yaml` or commit it; it stays in the env var / a GitHub secret.
- Don't fabricate FEMS station IDs or `thresholds` — look them up or ask the user.
- Keep changes working offline-testable: prefer defensive parsing over hard-coding exact column names.
