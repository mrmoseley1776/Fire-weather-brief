#!/usr/bin/env python3
"""
Prodigy Fire Weather Morning Brief
==================================

Pulls NFDRS fire danger outputs (Spread Component, Energy Release Component,
Burning Index) AND fire weather (min RH, max wind, max gust) from the U.S.
Forest Service Fire Environment Mapping System (FEMS) for RAWS stations grouped
by Geographic Area Coordination Center (GACC), adds a Significant Fire Potential
section driven by live NWS Red Flag Warnings / Fire Weather Watches plus links to
Predictive Services 7-day outlooks, and emails the briefing to the sales team.

Data sources (public, no key required):
  * FEMS NFDR:     https://fems.fs2c.usda.gov/api/ext-climatology/download-nfdr-daily-summary/
  * FEMS Weather:  https://fems.fs2c.usda.gov/api/ext-climatology/download-wx-daily-summary/
  * NWS alerts:    https://api.weather.gov/alerts/active

Usage:
    python fire_weather_brief.py                 # build + email
    python fire_weather_brief.py --dry-run       # build + save HTML, no email
    python fire_weather_brief.py --verify        # check which stations resolve
    python fire_weather_brief.py --config other.yaml
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
import os
import smtplib
import sys
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
import yaml

FEMS_BASE = "https://fems.fs2c.usda.gov/api/ext-climatology"
NFDR_DAILY_ENDPOINT = f"{FEMS_BASE}/download-nfdr-daily-summary/"
WX_DAILY_ENDPOINT = f"{FEMS_BASE}/download-wx-daily-summary/"
NWS_ALERTS_ENDPOINT = "https://api.weather.gov/alerts/active"

FIRE_ALERT_EVENTS = ("Red Flag Warning", "Fire Weather Watch")


# --------------------------------------------------------------------------- #
# Config models
# --------------------------------------------------------------------------- #
@dataclass
class Station:
    id: str
    label: Optional[str] = None


@dataclass
class Gacc:
    code: str
    name: str
    fuel_models: str
    stations: list[Station] = field(default_factory=list)


@dataclass
class DayValues:
    date: str
    sc: Optional[float]
    erc: Optional[float]
    bi: Optional[float]
    observed: bool
    fm100: Optional[float] = None   # 100-hr dead fuel moisture, %
    fm1000: Optional[float] = None  # 1000-hr dead fuel moisture, %


@dataclass
class Weather:
    min_rh: Optional[float] = None
    max_wind: Optional[float] = None
    max_gust: Optional[float] = None


@dataclass
class StationReport:
    station_id: str
    name: str
    latest: Optional[DayValues]           # most recent observed day
    previous: Optional[DayValues]         # observed day before that (for trend)
    peak_bi_7day: Optional[float]         # highest BI across forecast horizon
    weather: Weather = field(default_factory=Weather)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not cfg:
        raise SystemExit(f"Config {path} is empty.")
    return cfg


def parse_gaccs(cfg: dict) -> list[Gacc]:
    default_fm = str(cfg.get("data", {}).get("fuel_models_default", "Y"))
    gaccs: list[Gacc] = []
    for g in cfg.get("coordination_centers", []):
        stations: list[Station] = []
        for s in g.get("stations", []):
            if isinstance(s, dict):
                stations.append(Station(id=str(s["id"]), label=s.get("name")))
            else:
                stations.append(Station(id=str(s)))
        gaccs.append(
            Gacc(
                code=g["code"],
                name=g.get("name", g["code"]),
                fuel_models=str(g.get("fuel_models", default_fm)),
                stations=stations,
            )
        )
    return gaccs


# --------------------------------------------------------------------------- #
# Shared CSV helpers
# --------------------------------------------------------------------------- #
def _col(fieldnames: list[str], needle: str, prefer: str = "max",
         avoid: Optional[str] = None) -> Optional[str]:
    """Find the real column whose squashed lowercase name contains `needle`.
    When several match, prefer one containing `prefer` (e.g. 'max'/'min').
    `avoid` lets us skip a token (e.g. skip 'gust' when looking for wind speed)."""
    def squash(c: str) -> str:
        return c.lower().replace("_", "").replace(" ", "")
    matches = [c for c in fieldnames if needle in squash(c)]
    if avoid:
        matches = [c for c in matches if avoid not in squash(c)]
    if not matches:
        return None
    for c in matches:
        if prefer and prefer in squash(c):
            return c
    return matches[0]


def _col_exact(fieldnames: list[str], *names: str) -> Optional[str]:
    """Find the real column whose name case-insensitively equals one of `names`.
    Used for short codes (e.g. 'SC') where substring matching in `_col` would
    also catch unrelated columns like 'MaxSCTime'."""
    lookup = {c.lower(): c for c in fieldnames}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def _to_float(v) -> Optional[float]:
    v = ("" if v is None else str(v)).strip()
    if v in ("", "-999", "-999.0", "NaN", "null", "None"):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if f <= -998 else f


# --------------------------------------------------------------------------- #
# FEMS fire danger (NFDR)
# --------------------------------------------------------------------------- #
def fetch_nfdr_csv(station_ids: list[str], fuel_models: str, dataset: str,
                   timeout: int = 60) -> str:
    params = {
        "dataset": dataset,
        "presetDate": "-5Days7Days",
        "dataFormat": "csv",
        "stationIds": ",".join(station_ids),
        "fuelModels": fuel_models,
    }
    resp = requests.get(NFDR_DAILY_ENDPOINT, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_reports(csv_text: str) -> dict[str, StationReport]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {}
    fn = reader.fieldnames
    c_station = _col(fn, "stationname") or "stationName"
    c_sc = _col_exact(fn, "SC") or _col(fn, "spreadcomponent")
    c_erc = _col_exact(fn, "ERC") or _col(fn, "energyreleasecomponent")
    c_bi = _col_exact(fn, "BI") or _col(fn, "burningindex")
    c_fm100 = _col_exact(fn, "100HrFM") or _col(fn, "100hrfm")
    c_fm1000 = _col_exact(fn, "1000HrFM") or _col(fn, "1000hrfm")
    c_type = _col(fn, "nfdrtype")
    c_time = (_col(fn, "observationtime") or _col(fn, "nfdrdate")
              or _col(fn, "date"))

    rows_by_station: dict[str, list[DayValues]] = {}
    for row in reader:
        name = (row.get(c_station) or "UNKNOWN").strip()
        raw_type = (row.get(c_type) or "").strip().upper() if c_type else ""
        observed = raw_type in ("", "O")
        date = ((row.get(c_time) or "").strip() if c_time else "")[:10]
        dv = DayValues(
            date=date,
            sc=_to_float(row.get(c_sc)) if c_sc else None,
            erc=_to_float(row.get(c_erc)) if c_erc else None,
            bi=_to_float(row.get(c_bi)) if c_bi else None,
            observed=observed,
            fm100=_to_float(row.get(c_fm100)) if c_fm100 else None,
            fm1000=_to_float(row.get(c_fm1000)) if c_fm1000 else None,
        )
        rows_by_station.setdefault(name, []).append(dv)

    reports: dict[str, StationReport] = {}
    for name, days in rows_by_station.items():
        days.sort(key=lambda d: d.date)
        observed_days = [d for d in days if d.observed and d.bi is not None]
        forecast_days = [d for d in days if not d.observed and d.bi is not None]
        latest = observed_days[-1] if observed_days else (days[-1] if days else None)
        previous = observed_days[-2] if len(observed_days) >= 2 else None
        peak_bi = max((d.bi for d in forecast_days), default=None)
        reports[name] = StationReport(
            station_id="", name=name, latest=latest, previous=previous,
            peak_bi_7day=peak_bi,
        )
    return reports


# --------------------------------------------------------------------------- #
# FEMS weather (min RH / max wind / max gust)
# --------------------------------------------------------------------------- #
def fetch_wx_csv(station_ids: list[str], dataset: str, timeout: int = 60) -> str:
    params = {
        "dataset": dataset,
        "presetDate": "-5Days7Days",
        "dataFormat": "csv",
        "stationIds": ",".join(station_ids),
    }
    resp = requests.get(WX_DAILY_ENDPOINT, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_weather(csv_text: str) -> dict[str, Weather]:
    """Return latest-observed daily min RH and max wind/gust per station name.
    Column names are matched loosely so the tool survives FEMS naming changes."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return {}
    fn = reader.fieldnames
    c_station = _col(fn, "stationname") or "stationName"
    c_type = _col(fn, "nfdrtype") or _col(fn, "recordtype")
    c_time = (_col(fn, "observationtime") or _col(fn, "date") or _col(fn, "nfdrdate"))
    # RH: prefer a "min" column (fire weather cares about the daily low).
    c_rh = _col(fn, "relativehumidity", prefer="min") or _col(fn, "humidity", prefer="min")
    # Wind speed: max, but not the gust column.
    c_wind = _col(fn, "windspeed", prefer="max", avoid="gust") or _col(fn, "wind", prefer="max", avoid="gust")
    c_gust = _col(fn, "gust", prefer="max")

    rows_by_station: dict[str, list[tuple[str, bool, Weather]]] = {}
    for row in reader:
        name = (row.get(c_station) or "UNKNOWN").strip()
        raw_type = (row.get(c_type) or "").strip().upper() if c_type else ""
        observed = raw_type in ("", "O")
        date = ((row.get(c_time) or "").strip() if c_time else "")[:10]
        w = Weather(
            min_rh=_to_float(row.get(c_rh)) if c_rh else None,
            max_wind=_to_float(row.get(c_wind)) if c_wind else None,
            max_gust=_to_float(row.get(c_gust)) if c_gust else None,
        )
        rows_by_station.setdefault(name, []).append((date, observed, w))

    out: dict[str, Weather] = {}
    for name, rows in rows_by_station.items():
        rows.sort(key=lambda r: r[0])
        observed_rows = [r for r in rows if r[1]]
        chosen = observed_rows[-1] if observed_rows else (rows[-1] if rows else None)
        out[name] = chosen[2] if chosen else Weather()
    return out


# --------------------------------------------------------------------------- #
# NWS Red Flag Warnings / Fire Weather Watches
# --------------------------------------------------------------------------- #
def fetch_fire_alerts(states: list[str], contact: str,
                      timeout: int = 30) -> list[dict]:
    """Return active Red Flag Warnings and Fire Weather Watches for the given
    states, as a list of simplified dicts. NWS requires a descriptive
    User-Agent that includes a contact."""
    if not states:
        return []
    headers = {
        "User-Agent": f"ProdigyFireWeatherBrief ({contact})",
        "Accept": "application/geo+json",
    }
    params = {"area": ",".join(states), "status": "actual", "message_type": "alert"}
    resp = requests.get(NWS_ALERTS_ENDPOINT, params=params, headers=headers,
                        timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    alerts = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        event = (p.get("event") or "").strip()
        if event not in FIRE_ALERT_EVENTS:
            continue
        alerts.append({
            "event": event,
            "area": p.get("areaDesc", ""),
            "headline": p.get("headline", ""),
            "sender": p.get("senderName", ""),
            "onset": p.get("onset") or p.get("effective") or "",
            "ends": p.get("ends") or p.get("expires") or "",
            "states": _states_from_geocode(p.get("geocode", {})),
        })
    return alerts


def _states_from_geocode(geocode: dict) -> set[str]:
    """Derive state postal codes from UGC zone codes (first two letters)."""
    states = set()
    for ugc in geocode.get("UGC", []) or []:
        if len(ugc) >= 2 and ugc[:2].isalpha():
            states.add(ugc[:2].upper())
    return states


def group_alerts_by_state(alerts: list[dict], states_order: list[str]) -> dict:
    grouped: dict[str, list[dict]] = {s: [] for s in states_order}
    other: list[dict] = []
    for a in alerts:
        placed = False
        for s in a["states"]:
            if s in grouped:
                grouped[s].append(a)
                placed = True
        if not placed:
            # fall back to matching the areaDesc text
            for s in states_order:
                if s in (a["area"] or ""):
                    grouped[s].append(a)
                    placed = True
                    break
        if not placed:
            other.append(a)
    return {s: v for s, v in grouped.items() if v}, other


# --------------------------------------------------------------------------- #
# Rating helpers
# --------------------------------------------------------------------------- #
LEVELS = ["Low", "Moderate", "High", "Very High", "Extreme"]
LEVEL_COLORS = {
    "Low": "#2e7d32", "Moderate": "#9e9d24", "High": "#f9a825",
    "Very High": "#ef6c00", "Extreme": "#c62828", "": "#607d8b",
}


def classify(value: Optional[float], breakpoints: Optional[list[float]]) -> str:
    if value is None or not breakpoints or len(breakpoints) != 4:
        return ""
    b1, b2, b3, b4 = breakpoints
    if value <= b1:
        return "Low"
    if value <= b2:
        return "Moderate"
    if value <= b3:
        return "High"
    if value <= b4:
        return "Very High"
    return "Extreme"


def trend_arrow(latest: Optional[float], previous: Optional[float]) -> str:
    if latest is None or previous is None:
        return "-"
    diff = latest - previous
    if abs(diff) < 0.5:
        return "= steady"
    return f"+{diff:.0f}" if diff > 0 else f"{diff:.0f}"


def fmt(v: Optional[float], suffix: str = "") -> str:
    return "-" if v is None else f"{v:.0f}{suffix}"


def _fmt_alert_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%b %d %I%p").replace(" 0", " ")
    except ValueError:
        return iso[:16]


# --------------------------------------------------------------------------- #
# Report building
# --------------------------------------------------------------------------- #
def build_gacc_reports(gaccs: list[Gacc], dataset: str, want_weather: bool):
    out, flat = [], []
    for g in gaccs:
        ids = [s.id for s in g.stations if s.id and not s.id.startswith("REPLACE")]
        placeholders = [s.id for s in g.stations if s.id.startswith("REPLACE")]
        rows = []
        if not ids:
            for ph in placeholders:
                rows.append((Station(id=ph),
                             StationReport("", ph, None, None, None,
                                           error="placeholder - set a real station ID")))
            out.append((g, rows))
            continue

        try:
            reports_by_name = parse_reports(fetch_nfdr_csv(ids, g.fuel_models, dataset))
        except Exception as exc:  # noqa: BLE001
            for s in g.stations:
                rows.append((s, StationReport(s.id, s.label or s.id, None, None,
                                              None, error=f"fetch error: {exc}")))
            out.append((g, rows))
            continue

        wx_by_name: dict[str, Weather] = {}
        if want_weather:
            try:
                wx_by_name = parse_weather(fetch_wx_csv(ids, dataset))
            except Exception:  # noqa: BLE001 - weather is a nice-to-have, never fatal
                wx_by_name = {}

        used = set()
        for s in g.stations:
            rep = None
            if s.label:
                for nm, r in reports_by_name.items():
                    if nm.lower() == s.label.lower() and nm not in used:
                        rep, used = r, used | {nm}
                        break
            if rep is None:
                for nm, r in reports_by_name.items():
                    if nm not in used:
                        rep, used = r, used | {nm}
                        break
            if rep is None:
                rep = StationReport(s.id, s.label or s.id, None, None, None,
                                    error="no data returned")
            rep.station_id = s.id
            # attach weather by matching name
            if rep.name in wx_by_name:
                rep.weather = wx_by_name[rep.name]
            rows.append((s, rep))
            if rep.latest and rep.latest.bi is not None:
                flat.append((g, rep))
        out.append((g, rows))
    flat.sort(key=lambda x: (x[1].latest.bi if x[1].latest else -1), reverse=True)
    return out, flat


# --------------------------------------------------------------------------- #
# Trend chart (Burning Index change vs. yesterday)
# --------------------------------------------------------------------------- #
def collect_movers(gacc_rows) -> list[tuple[str, str, float, float]]:
    """(label, gacc_code, delta_bi, today_bi) for stations with today+yesterday."""
    movers = []
    for g, rows in gacc_rows:
        for s, rep in rows:
            if rep.error or not rep.latest or rep.latest.bi is None:
                continue
            if not rep.previous or rep.previous.bi is None:
                continue
            movers.append((rep.name, g.code,
                           rep.latest.bi - rep.previous.bi, rep.latest.bi))
    return movers



# --------------------------------------------------------------------------- #
# Rendering — HTML
# --------------------------------------------------------------------------- #
def render_html(gacc_rows, ranked, thresholds, generated, sfp=None,
                want_weather=True, want_fm=True, logo_src=None) -> str:
    def rating_cell(val, station_id, index_key):
        bp = thresholds.get(str(station_id), {}).get(index_key)
        level = classify(val, bp)
        color = LEVEL_COLORS.get(level, "#607d8b")
        chip = (f'<span style="background:{color};color:#fff;padding:1px 7px;'
                f'border-radius:10px;font-size:11px;">{level}</span>'
                if level else "")
        return f"{fmt(val)} {chip}"

    top = ranked[:5]
    top_html = ""
    if top:
        items = "".join(
            f'<li><b>{r.name}</b> ({g.code}) &mdash; BI {fmt(r.latest.bi)}, '
            f'ERC {fmt(r.latest.erc)}, SC {fmt(r.latest.sc)}'
            + (f', min RH {fmt(r.weather.min_rh, "%")}, '
               f'wind {fmt(r.weather.max_wind, " mph")}' if want_weather else "")
            + '</li>'
            for g, r in top
        )
        top_html = (
            '<div class="fw-box" style="background:#faf3df;border-left:4px solid #B18C19;'
            'padding:10px 14px;margin:0 0 18px;'
            'page-break-inside:avoid;break-inside:avoid;">'
            '<div style="font-weight:700;color:#9c7a16;margin-bottom:4px;">'
            'Highest fire danger this morning</div>'
            f'<ul style="margin:4px 0 0 18px;padding:0;">{items}</ul></div>'
        )

    sfp_html = render_sfp_html(sfp) if sfp else ""

    # weather columns
    wx_head = ('<th align="center">Min RH</th><th align="center">Wind</th>'
               '<th align="center">Gust</th>') if want_weather else ""
    # dead fuel moisture columns
    fm_head = ('<th align="center">100-hr FM</th><th align="center">1000-hr FM</th>'
              ) if want_fm else ""
    ncols = 6 + (3 if want_weather else 0) + (2 if want_fm else 0)

    blocks = []
    for g, rows in gacc_rows:
        tr = []
        for s, rep in rows:
            if rep.error:
                tr.append(f'<tr><td>{rep.name}</td>'
                          f'<td colspan="{ncols-1}" style="color:#b71c1c;'
                          f'font-size:12px;">{rep.error}</td></tr>')
                continue
            l = rep.latest
            sc = rating_cell(l.sc if l else None, rep.station_id, "sc")
            erc = rating_cell(l.erc if l else None, rep.station_id, "erc")
            bi = rating_cell(l.bi if l else None, rep.station_id, "bi")
            trend = trend_arrow(l.bi if l else None,
                                rep.previous.bi if rep.previous else None)
            peak = fmt(rep.peak_bi_7day)
            wx_cells = ""
            if want_weather:
                w = rep.weather
                rh_color = "#c62828" if (w.min_rh is not None and w.min_rh <= 15) else "#555"
                wd_color = "#c62828" if (w.max_wind is not None and w.max_wind >= 25) else "#555"
                wx_cells = (
                    f'<td align="center" style="color:{rh_color};">{fmt(w.min_rh, "%")}</td>'
                    f'<td align="center" style="color:{wd_color};">{fmt(w.max_wind)}</td>'
                    f'<td align="center" style="color:#555;">{fmt(w.max_gust)}</td>')
            fm_cells = ""
            if want_fm:
                fm100 = l.fm100 if l else None
                fm1000 = l.fm1000 if l else None
                fm_cells = (
                    f'<td align="center" style="color:#555;">{fmt(fm100, "%")}</td>'
                    f'<td align="center" style="color:#555;">{fmt(fm1000, "%")}</td>')
            tr.append(
                f'<tr><td style="font-weight:600;">{rep.name}</td>'
                f'<td align="center">{sc}</td>'
                f'<td align="center">{erc}</td>'
                f'<td align="center">{bi}</td>'
                f'<td align="center" style="font-size:12px;color:#555;">{trend}</td>'
                f'<td align="center" style="font-size:12px;color:#555;">{peak}</td>'
                f'{wx_cells}{fm_cells}</tr>')
        table = (
            '<table class="fw-table" cellspacing="0" cellpadding="7" '
            'style="border-collapse:collapse;width:100%;font-size:13px;">'
            '<thead><tr style="background:#15171c;color:#fff;text-align:left;">'
            '<th>Station</th><th align="center">SC</th><th align="center">ERC</th>'
            '<th align="center">BI</th><th align="center">BI &Delta;</th>'
            f'<th align="center">7d peak</th>{wx_head}{fm_head}</tr></thead>'
            f'<tbody>{"".join(tr)}</tbody></table>')
        blocks.append(
            '<div class="fw-gacc-block" style="page-break-inside:avoid;break-inside:avoid;">'
            f'<h3 class="fw-h3" style="margin:22px 0 6px;color:#15171c;border-bottom:2px solid '
            f'#B18C19;padding-bottom:4px;">{g.name} '
            f'<span style="color:#8a8574;font-weight:400;font-size:13px;">'
            f'({g.code} &middot; fuel model {g.fuel_models})</span></h3>{table}</div>')

    logo_html = (f'<img src="{logo_src}" alt="Prodigy Wildfire Solutions" '
                 'style="height:64px;width:auto;display:block;">'
                 if logo_src else "")
    stamp = generated.strftime("%A, %B %d, %Y")
    wx_legend = (" &middot; Min RH = daily minimum relative humidity &middot; "
                 "Wind/Gust = daily max mph. Red = RH&le;15% or wind&ge;25 mph."
                 if want_weather else "")
    fm_legend = (" &middot; 100-hr/1000-hr FM = dead fuel moisture (%), how much "
                 "water is in medium/large dead fuel &mdash; lower = drier = more "
                 "available to burn." if want_fm else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
/* Only affects printing / PDF export (Playwright emulates print media for
   render_pdf()) -- has no effect on screen preview or email clients, which
   ignore @media print. Tightens vertical spacing so the page-break-inside:avoid
   rule on .fw-gacc-block leaves less unused space at the bottom of a page. */
@media print {{
  body {{ padding: 10px !important; }}
  .fw-h3 {{ margin: 12px 0 4px !important; font-size: 15px !important; }}
  .fw-table {{ font-size: 11px !important; }}
  .fw-table td, .fw-table th {{ padding: 4px 6px !important; }}
  .fw-gacc-block {{ page-break-inside: avoid; break-inside: avoid; }}
  .fw-box {{ padding: 8px 12px !important; margin: 0 0 10px !important; }}
}}
</style>
</head><body style="margin:0;background:#f5f4f0;
padding:20px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:#15171c;">
<div style="max-width:820px;margin:0 auto;background:#fff;border-radius:10px;
overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.15);">
<div style="background:#15171c;color:#fff;
padding:16px 24px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<tr>
<td style="vertical-align:middle;">
<div style="font-size:20px;font-weight:800;letter-spacing:.3px;">
Prodigy Fire Weather Brief</div>
<div style="opacity:.9;font-size:13px;margin-top:2px;">{stamp}</div>
</td>
<td style="vertical-align:middle;text-align:right;width:1%;white-space:nowrap;padding-left:16px;">
{logo_html}
</td>
</tr>
</table>
</div>
<div style="padding:20px 24px;">
{top_html}
{sfp_html}
{''.join(blocks)}
<p style="font-size:11px;color:#8a8574;margin-top:24px;line-height:1.5;">
SC = Spread Component &middot; ERC = Energy Release Component &middot;
BI = Burning Index (NFDRS daily max).{wx_legend}{fm_legend}
Sources: USFS FEMS and NWS. Adjective ratings appear only where station
percentile breakpoints are configured; absolute index values are not directly
comparable between stations.</p>
</div></div></body></html>"""


def render_sfp_html(sfp: dict) -> str:
    grouped = sfp.get("grouped", {})
    other = sfp.get("other", [])
    links = sfp.get("links", [])
    err = sfp.get("error")

    inner = ""
    if err:
        inner += (f'<div style="font-size:12px;color:#b71c1c;">'
                  f'Live alert feed unavailable: {err}</div>')
    elif not grouped and not other:
        inner += ('<div style="font-size:13px;color:#2e7d32;">'
                  'No active Red Flag Warnings or Fire Weather Watches in the '
                  'monitored states.</div>')
    else:
        def alert_line(a):
            tag_color = "#c62828" if a["event"] == "Red Flag Warning" else "#ef6c00"
            when = _fmt_alert_time(a["onset"])
            ends = _fmt_alert_time(a["ends"])
            span = f" ({when}&ndash;{ends})" if when or ends else ""
            return (f'<li style="margin-bottom:3px;"><span style="background:'
                    f'{tag_color};color:#fff;font-size:10px;padding:1px 6px;'
                    f'border-radius:8px;">{a["event"]}</span> {a["area"]}{span}</li>')
        parts = []
        for state, alist in grouped.items():
            parts.append(f'<div style="margin:6px 0 2px;font-weight:700;'
                         f'color:#37474f;">{state}</div>'
                         f'<ul style="margin:0 0 6px 18px;padding:0;">'
                         + "".join(alert_line(a) for a in alist) + '</ul>')
        for a in other:
            parts.append(f'<ul style="margin:0 0 6px 18px;padding:0;">'
                         f'{alert_line(a)}</ul>')
        inner += "".join(parts)

    link_html = ""
    if links:
        link_html = ('<div style="margin-top:8px;font-size:12px;">'
                     '<b>Predictive Services 7-day outlooks:</b> '
                     + " &middot; ".join(
                         f'<a href="{l["url"]}" style="color:#1565c0;">{l["label"]}</a>'
                         for l in links) + '</div>')

    return (
        '<div class="fw-box" style="background:#faf3df;border:1px solid #d9c48a;border-radius:8px;'
        'padding:12px 16px;margin:0 0 18px;">'
        '<div style="font-weight:800;color:#9c7a16;font-size:15px;margin-bottom:6px;">'
        'Significant Fire Potential</div>'
        f'{inner}{link_html}</div>')


# --------------------------------------------------------------------------- #
# Rendering — plain text
# --------------------------------------------------------------------------- #
def render_text(gacc_rows, ranked, generated, sfp=None, want_weather=True,
                want_fm=True) -> str:
    lines = [f"PRODIGY FIRE WEATHER BRIEF - {generated:%A %B %d, %Y}", ""]
    if ranked[:5]:
        lines.append("HIGHEST FIRE DANGER THIS MORNING")
        for g, r in ranked[:5]:
            extra = (f"  RH {fmt(r.weather.min_rh,'%')}  wind {fmt(r.weather.max_wind)}"
                     if want_weather else "")
            lines.append(f"  {r.name} ({g.code}): BI {fmt(r.latest.bi)}  "
                         f"ERC {fmt(r.latest.erc)}  SC {fmt(r.latest.sc)}{extra}")
        lines.append("")

    movers = collect_movers(gacc_rows)
    if movers:
        movers.sort(key=lambda m: m[2], reverse=True)
        lines.append("OVERNIGHT MOVEMENT (Burning Index vs. yesterday)")
        for name, code, delta, today in movers[:8]:
            lines.append(f"  {name} ({code}): {delta:+.0f}  ->  BI {today:.0f}")
        lines.append("")

    if sfp:
        lines.append("SIGNIFICANT FIRE POTENTIAL (active NWS alerts)")
        if sfp.get("error"):
            lines.append(f"  feed unavailable: {sfp['error']}")
        elif not sfp.get("grouped") and not sfp.get("other"):
            lines.append("  No active Red Flag Warnings or Fire Weather Watches.")
        else:
            for state, alist in sfp.get("grouped", {}).items():
                lines.append(f"  {state}:")
                for a in alist:
                    lines.append(f"    [{a['event']}] {a['area']}")
            for a in sfp.get("other", []):
                lines.append(f"  [{a['event']}] {a['area']}")
        for l in sfp.get("links", []):
            lines.append(f"  outlook: {l['label']} -> {l['url']}")
        lines.append("")

    for g, rows in gacc_rows:
        lines.append(f"{g.name} ({g.code}, fuel model {g.fuel_models})")
        hdr = f"  {'Station':22} {'SC':>4} {'ERC':>4} {'BI':>4} {'7dPk':>5}"
        if want_weather:
            hdr += f" {'RH%':>4} {'Wind':>5} {'Gust':>5}"
        if want_fm:
            hdr += f" {'100FM':>6} {'1000FM':>7}"
        lines.append(hdr)
        for s, rep in rows:
            if rep.error:
                lines.append(f"  {rep.name:22} {rep.error}")
                continue
            l = rep.latest
            row = (f"  {rep.name[:22]:22} {fmt(l.sc if l else None):>4} "
                   f"{fmt(l.erc if l else None):>4} {fmt(l.bi if l else None):>4} "
                   f"{fmt(rep.peak_bi_7day):>5}")
            if want_weather:
                w = rep.weather
                row += (f" {fmt(w.min_rh):>4} {fmt(w.max_wind):>5} "
                        f"{fmt(w.max_gust):>5}")
            if want_fm:
                row += (f" {fmt(l.fm100 if l else None):>6} "
                        f"{fmt(l.fm1000 if l else None):>7}")
            lines.append(row)
        lines.append("")
    lines.append("SC=Spread Component ERC=Energy Release Component BI=Burning Index")
    lines.append("Sources: USFS FEMS and NWS.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Significant fire potential assembly
# --------------------------------------------------------------------------- #
def build_sfp(cfg: dict) -> Optional[dict]:
    sc = cfg.get("significant_fire_potential", {})
    if not sc or not sc.get("enabled", True):
        return None
    states = [str(s).upper() for s in sc.get("states", [])]
    contact = sc.get("contact_email") or cfg.get("email", {}).get("from_addr", "n/a")
    links = sc.get("predictive_services_links", []) or []
    result = {"grouped": {}, "other": [], "links": links, "error": None}
    try:
        alerts = fetch_fire_alerts(states, contact)
        grouped, other = group_alerts_by_state(alerts, states)
        result["grouped"], result["other"] = grouped, other
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def render_pdf(html: str) -> Optional[bytes]:
    """Render the brief HTML (with base64-embedded images, not CID references)
    to PDF bytes using a headless Chromium via Playwright. Returns None if
    Playwright -- or its bundled Chromium browser -- isn't installed; the email
    still sends fine without a PDF attachment in that case."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            page.emulate_media(media="print")
            pdf_bytes = page.pdf(
                format="Letter",
                print_background=True,
                margin={"top": "0.4in", "bottom": "0.4in",
                        "left": "0.4in", "right": "0.4in"},
            )
            browser.close()
            return pdf_bytes
    except Exception:  # noqa: BLE001 - never block the email over a PDF failure
        return None


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(cfg: dict, subject: str, html: str, text: str,
               image_bytes: Optional[bytes] = None, image_cid: str = "trend",
               inline_images: Optional[dict] = None,
               pdf_bytes: Optional[bytes] = None,
               pdf_filename: str = "fire_weather_brief.pdf") -> None:
    ec = cfg["email"]
    pw = os.environ.get(ec.get("smtp_password_env", "FEMS_SMTP_PASSWORD"), "")
    if not pw:
        raise SystemExit(
            f"No SMTP password in env var "
            f"'{ec.get('smtp_password_env', 'FEMS_SMTP_PASSWORD')}'. "
            "Set it, or use --dry-run.")
    to_addrs = ec["to_addrs"]

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))

    # Merge the legacy single-image args with the new multi-image dict.
    images = dict(inline_images or {})
    if image_bytes:
        images.setdefault(image_cid, image_bytes)

    if images:
        from email.mime.image import MIMEImage
        related = MIMEMultipart("related")
        related.attach(alt)
        for cid, data in images.items():
            img = MIMEImage(data, _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            related.attach(img)
        body = related
    else:
        body = alt

    # A real file attachment (the PDF) has to live in a "mixed" container
    # wrapped around the alternative/related body -- attaching it directly to
    # an "alternative" part isn't valid per the MIME spec.
    if pdf_bytes:
        from email.mime.application import MIMEApplication
        mixed = MIMEMultipart("mixed")
        mixed.attach(body)
        pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
        pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
        mixed.attach(pdf_part)
        msg = mixed
    else:
        msg = body

    msg["Subject"] = subject
    msg["From"] = ec["from_addr"]
    msg["To"] = ", ".join(to_addrs)

    with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"]), timeout=30) as server:
        server.starttls()
        server.login(ec["smtp_user"], pw)
        server.sendmail(ec["from_addr"], to_addrs, msg.as_string())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Prodigy fire weather morning brief")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--out", default="brief.html")
    args = ap.parse_args()

    cfg = load_config(args.config)
    gaccs = parse_gaccs(cfg)
    dataset = str(cfg.get("data", {}).get("dataset", "all"))
    thresholds = cfg.get("thresholds", {}) or {}
    want_weather = bool(cfg.get("weather", {}).get("enabled", True))
    want_fm = bool(cfg.get("fuel_moisture", {}).get("enabled", True))

    now = dt.datetime.now().astimezone()

    if args.verify:
        gacc_rows, _ = build_gacc_reports(gaccs, dataset, want_weather)
        for g, rows in gacc_rows:
            print(f"\n{g.code} - {g.name} (fuel model {g.fuel_models})")
            for s, rep in rows:
                if rep.error:
                    status = rep.error
                elif rep.latest:
                    status = (f"OK  SC={fmt(rep.latest.sc)} ERC={fmt(rep.latest.erc)} "
                              f"BI={fmt(rep.latest.bi)} RH={fmt(rep.weather.min_rh)} "
                              f"wind={fmt(rep.weather.max_wind)}")
                else:
                    status = "no data"
                print(f"   id={s.id:<10} {rep.name:<24} {status}")
        return 0

    gacc_rows, ranked = build_gacc_reports(gaccs, dataset, want_weather)
    sfp = build_sfp(cfg)

    logo_path = Path(__file__).resolve().parent / "assets" / "logo.png"
    logo_bytes = logo_path.read_bytes() if logo_path.exists() else None
    logo_preview_src = None
    logo_email_src = None
    if logo_bytes:
        logo_preview_src = "data:image/png;base64," + base64.b64encode(logo_bytes).decode()
        logo_email_src = "cid:logo"

    text = render_text(gacc_rows, ranked, now, sfp, want_weather, want_fm)
    preview_html = render_html(gacc_rows, ranked, thresholds, now, sfp,
                               want_weather, want_fm,
                               logo_src=logo_preview_src)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(preview_html)
    print(f"Wrote preview -> {args.out}")

    prefix = cfg["email"].get("subject_prefix", "Fire Weather Brief")
    subject = f"{prefix} - {now:%a %b %d}"

    if args.dry_run:
        print("Dry run: email not sent.\n")
        print(text)
        return 0

    email_html = render_html(gacc_rows, ranked, thresholds, now, sfp,
                             want_weather, want_fm,
                             logo_src=logo_email_src)
    inline_images = {}
    if logo_bytes:
        inline_images["logo"] = logo_bytes

    pdf_bytes = render_pdf(preview_html)
    if pdf_bytes is None:
        print("Note: PDF attachment skipped (Playwright/Chromium not available).")
    pdf_filename = f"fire_weather_brief_{now:%Y-%m-%d}.pdf"

    send_email(cfg, subject, email_html, text, inline_images=inline_images,
               pdf_bytes=pdf_bytes, pdf_filename=pdf_filename)
    print(f"Email sent to: {', '.join(cfg['email']['to_addrs'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
