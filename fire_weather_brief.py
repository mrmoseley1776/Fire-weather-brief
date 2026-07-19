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
import json
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

FEMS_BASE = "https://fems.fs2c.usda.gov/api/ext-climatology"
NFDR_DAILY_ENDPOINT = f"{FEMS_BASE}/download-nfdr-daily-summary/"
WX_DAILY_ENDPOINT = f"{FEMS_BASE}/download-wx-daily-summary/"
NWS_ALERTS_ENDPOINT = "https://api.weather.gov/alerts/active"

# InciWeb (inciweb.wildfire.gov) is the interagency incident-information system
# PIOs post directly to -- free, keyless, national. Its RSS feed lists every
# actively-updated incident (fire name, state, coordinates, and a free-text
# overview that *sometimes* includes an EVACUATIONS section, at the PIO's
# discretion). Used here as a complementary "known active named incidents"
# list, not as a structured evacuation-order source -- see build_active_incidents().
INCIWEB_RSS_URL = "https://inciweb.wildfire.gov/incidents/rss.xml"

# Full state/territory name -> USPS abbreviation, for matching InciWeb's
# "State: <full name>" text against the abbreviations used everywhere else in
# this config (significant_fire_potential.states etc).
US_STATE_NAME_TO_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC",
}

FIRE_ALERT_EVENTS = ("Red Flag Warning", "Fire Weather Watch")

# Evacuation-related CAP alert types local emergency management issues through
# IPAWS and which NWS relays on the same alerts feed. "Evacuation Immediate" is
# the standard wildfire evacuation-order type; "Civil Emergency Message" is a
# catch-all some counties use instead/also.
EVAC_ALERT_EVENTS = ("Evacuation Immediate", "Civil Emergency Message")

# Best-effort text scan for a fire name inside an evacuation alert's free-text
# headline+description. Not a structured CAP field -- most alerts won't match,
# and that's a real gap in the source alert, not a parsing failure. Never
# fabricate a value if this doesn't match.
_FIRE_NAME_RE = re.compile(
    r"\b((?:[A-Z][\w'.-]*\s){0,3}[A-Z][\w'.-]*\s+Fire)\b")


def _extract_fire_name(text: str) -> Optional[str]:
    m = _FIRE_NAME_RE.search(text or "")
    return m.group(1).strip() if m else None


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


def fetch_evacuation_alerts(states: list[str], contact: str,
                            timeout: int = 30) -> list[dict]:
    """Return active evacuation-order alerts (Evacuation Immediate / Civil
    Emergency Message) for the given states, as a list of simplified dicts.
    Same endpoint/params as fetch_fire_alerts() but a different event filter --
    kept as a separate request (rather than sharing one fetch) so this box
    degrades independently if the feed hiccups on one call but not the other.
    fire_name is a best-effort text-scan extraction and is often None; that
    reflects what the alert actually said, not a parsing failure."""
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
        if event not in EVAC_ALERT_EVENTS:
            continue
        text = " ".join(filter(None, [p.get("headline"), p.get("description")]))
        alerts.append({
            "event": event,
            "area": p.get("areaDesc", ""),
            "headline": p.get("headline", ""),
            "sender": p.get("senderName", ""),
            "onset": p.get("onset") or p.get("effective") or "",
            "ends": p.get("ends") or p.get("expires") or "",
            "states": _states_from_geocode(p.get("geocode", {})),
            "fire_name": _extract_fire_name(text),
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


_INCIWEB_STATE_RE = re.compile(r"State:\s*([A-Za-z .]+?)\s*(?:\n|---|$)")
_INCIWEB_EVAC_RE = re.compile(r"evacuat", re.IGNORECASE)


def fetch_inciweb_incidents(states: list[str], contact: str, timeout: int = 20,
                             max_items: int = 15) -> list[dict]:
    """Return actively-updated InciWeb incidents (fire name + link) for the
    given states, alphabetical by state (then name). Free/keyless national
    RSS feed maintained by incident PIOs -- a good complementary "known named
    fires" list, but NOT a structured evacuation-order source: whether an
    incident's free-text overview mentions evacuations at all is entirely up
    to that incident's PIO (see README/CLAUDE.md). Best-effort like every
    other feed here: any fetch/parse failure should be caught by the caller,
    not here. Selection: incidents whose description text mentions
    "evacuat..." are ALWAYS kept (can push the list past max_items on a bad
    day -- an active evacuation is never dropped for a recency cutoff), then
    remaining slots up to max_items are filled by most-recently-updated. Only
    the final display order is alphabetical, so a quiet, longstanding
    incident can't push a brand-new one out of the list."""
    if not states:
        return []
    headers = {"User-Agent": f"ProdigyFireWeatherBrief ({contact})"}
    resp = requests.get(INCIWEB_RSS_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    wanted = set(states)
    incidents = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = item.findtext("description") or ""
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        m = _INCIWEB_STATE_RE.search(desc)
        if not m:
            continue
        abbr = US_STATE_NAME_TO_ABBR.get(m.group(1).strip())
        if abbr not in wanted:
            continue
        try:
            updated = parsedate_to_datetime(pub_date_raw) if pub_date_raw else None
        except (TypeError, ValueError):
            updated = None
        incidents.append({
            "name": title,
            "state": abbr,
            "link": link,
            "updated": updated,
            "evac": bool(_INCIWEB_EVAC_RE.search(desc)),
        })
    incidents.sort(key=lambda i: i["updated"] or dt.datetime.min.replace(
        tzinfo=dt.timezone.utc), reverse=True)
    evac_incidents = [i for i in incidents if i["evac"]]
    other_incidents = [i for i in incidents if not i["evac"]]
    remaining_slots = max(max_items - len(evac_incidents), 0)
    selected = evac_incidents + other_incidents[:remaining_slots]
    selected.sort(key=lambda i: (i["state"], i["name"]))
    return selected


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
                want_weather=True, want_fm=True, logo_src=None,
                sitrep=None, public_url=None, evac=None, incidents=None) -> str:
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

    evac_html = render_evac_html(evac) if evac else ""
    sfp_html = render_sfp_html(sfp) if sfp else ""
    incidents_html = render_incidents_html(incidents) if incidents else ""
    sitrep_html = render_sitrep_html(sitrep) if sitrep else ""

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

    # <title> + Open Graph / Twitter Card tags. These are what iMessage, Slack,
    # etc. read to build a rich link preview when the published brief.html URL
    # is shared -- without them the link shows as plain text. og:image needs an
    # absolute URL (data: URIs are not supported by link-preview crawlers), so
    # this only activates when `site.public_url` is set in config.yaml (the
    # GitHub Actions workflow also copies assets/logo.png next to index.html so
    # `{public_url}/logo.png` resolves). No public_url configured -> just a
    # plain <title>, no preview image/description (never guess a URL).
    page_title = f"Prodigy Fire Weather Brief — {stamp}"
    if public_url:
        base = public_url.rstrip("/") + "/"
        img_url = base + "logo.png"
        og_desc = ("Daily NFDRS fire danger and significant fire potential "
                   "briefing for the West Coast and Rockies.")
        head_meta = (
            f'<title>{page_title}</title>'
            f'<meta property="og:title" content="{page_title}">'
            f'<meta property="og:description" content="{og_desc}">'
            f'<meta property="og:type" content="website">'
            f'<meta property="og:url" content="{base}">'
            f'<meta property="og:image" content="{img_url}">'
            f'<meta name="twitter:card" content="summary_large_image">'
            f'<meta name="twitter:title" content="{page_title}">'
            f'<meta name="twitter:description" content="{og_desc}">'
            f'<meta name="twitter:image" content="{img_url}">')
    else:
        head_meta = f'<title>{page_title}</title>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
{head_meta}
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
  .fw-sitrep-box {{ margin-top: 24px !important; }}
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
{evac_html}
{sfp_html}
{incidents_html}
{''.join(blocks)}
<p style="font-size:11px;color:#8a8574;margin-top:24px;line-height:1.5;">
SC = Spread Component &middot; ERC = Energy Release Component &middot;
BI = Burning Index (NFDRS daily max).{wx_legend}{fm_legend}
Sources: USFS FEMS and NWS. Adjective ratings appear only where station
percentile breakpoints are configured; absolute index values are not directly
comparable between stations.</p>
{sitrep_html}
<p style="font-size:12px;color:#9c7a16;font-style:italic;text-align:center;
margin:16px 0 0;">{daily_quote(generated.date())}</p>
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


# Client-side live refresh for the Evacuation Orders box. NWS's alerts API
# allows direct browser calls (confirmed: Access-Control-Allow-Origin: *), so
# this fetches straight from api.weather.gov on every page load/refresh --
# genuinely live, not tied to the once-daily GitHub Actions rebuild. Mirrors
# fetch_evacuation_alerts()/_extract_fire_name() in JS; keep the two in sync
# if either changes. The server-rendered box (built from
# whatever build_evacuations() fetched at the last daily run) stays in the
# DOM as an immediate fallback and is only replaced if this fetch succeeds --
# if it fails (offline, CORS hiccup, JS disabled), the page still shows that
# last-build snapshot rather than breaking. Browsers won't let JS set a custom
# User-Agent (unlike fetch_evacuation_alerts()'s contact string), so this
# relies on NWS not strictly requiring one for browser-originated requests.
# No-ops (skips the fetch) if no states are configured.
_EVAC_LIVE_SCRIPT_TEMPLATE = """
<script>
(function(){
  var STATES = __STATES_JSON__;
  if (!STATES.length) { return; }
  var EVAC_EVENTS = ["Evacuation Immediate", "Civil Emergency Message"];
  var FIRE_RE = /\\b((?:[A-Z][\\w'.-]*\\s){0,3}[A-Z][\\w'.-]*\\s+Fire)\\b/;

  function esc(s){
    var d = document.createElement('div');
    d.textContent = (s === null || s === undefined) ? '' : String(s);
    return d.innerHTML;
  }
  function fmtTime(iso){
    if (!iso) { return ''; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return ''; }
    return d.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric'});
  }
  function evacLine(a){
    var tagColor = a.event === 'Evacuation Immediate' ? '#b71c1c' : '#6a1b9a';
    var when = fmtTime(a.onset);
    var ends = fmtTime(a.ends);
    var span = (when || ends) ? (' (' + esc(when) + '&ndash;' + esc(ends) + ')') : '';
    var fire = a.fire ? (' &mdash; <b>' + esc(a.fire) + '</b>') : '';
    return '<li style="margin-bottom:3px;"><span style="background:' + tagColor +
      ';color:#fff;font-size:10px;padding:1px 6px;border-radius:8px;">' + esc(a.event) +
      '</span> ' + esc(a.area) + fire + span + '</li>';
  }

  fetch('https://api.weather.gov/alerts/active?area=' + STATES.join(',') +
        '&status=actual&message_type=alert',
        {headers: {'Accept': 'application/geo+json'}})
    .then(function(r){ if (!r.ok) { throw new Error('HTTP ' + r.status); } return r.json(); })
    .then(function(data){
      var alerts = [];
      (data.features || []).forEach(function(feat){
        var p = feat.properties || {};
        var event = (p.event || '').trim();
        if (EVAC_EVENTS.indexOf(event) === -1) { return; }
        var text = [p.headline, p.description].filter(Boolean).join(' ');
        var fireM = FIRE_RE.exec(text);
        var states = [];
        ((p.geocode && p.geocode.UGC) || []).forEach(function(u){
          if (u.length >= 2) { states.push(u.slice(0, 2).toUpperCase()); }
        });
        alerts.push({
          event: event,
          area: p.areaDesc || '',
          onset: p.onset || p.effective || '',
          ends: p.ends || p.expires || '',
          states: states,
          fire: fireM ? fireM[1].trim() : null
        });
      });

      var grouped = {};
      STATES.forEach(function(s){ grouped[s] = []; });
      var other = [];
      alerts.forEach(function(a){
        var placed = false;
        a.states.forEach(function(s){
          if (Object.prototype.hasOwnProperty.call(grouped, s)) {
            grouped[s].push(a);
            placed = true;
          }
        });
        if (!placed) { other.push(a); }
      });

      var html = '';
      if (!alerts.length) {
        html = '<div style="font-size:13px;color:#2e7d32;">No active evacuation ' +
               'orders in the monitored states.</div>';
      } else {
        STATES.forEach(function(s){
          var list = grouped[s];
          if (!list.length) { return; }
          html += '<div style="margin:6px 0 2px;font-weight:700;color:#37474f;">' +
                  esc(s) + '</div><ul style="margin:0 0 6px 18px;padding:0;">' +
                  list.map(evacLine).join('') + '</ul>';
        });
        other.forEach(function(a){
          html += '<ul style="margin:0 0 6px 18px;padding:0;">' + evacLine(a) + '</ul>';
        });
      }

      var body = document.getElementById('fw-evac-body');
      if (body) { body.innerHTML = html; }
      var badge = document.getElementById('fw-evac-live-badge');
      if (badge) {
        var t = new Date().toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
        badge.textContent = ' \\u00b7 live as of ' + t;
        badge.style.display = 'inline';
      }
    })
    .catch(function(){
      // Live refresh failed (offline, CORS hiccup, etc.) -- leave the
      // server-rendered snapshot from the last daily build in place.
    });
})();
</script>
"""


def render_evac_html(evac: dict) -> str:
    grouped = evac.get("grouped", {})
    other = evac.get("other", [])
    err = evac.get("error")
    states = evac.get("states", [])

    inner = ""
    if err:
        inner += (f'<div style="font-size:12px;color:#7a1913;">'
                  f'Live alert feed unavailable: {err}</div>')
    elif not grouped and not other:
        inner += ('<div style="font-size:13px;color:#2e7d32;">'
                  'No active evacuation orders in the monitored states.</div>')
    else:
        def evac_line(a):
            tag_color = "#b71c1c" if a["event"] == "Evacuation Immediate" else "#6a1b9a"
            when = _fmt_alert_time(a["onset"])
            ends = _fmt_alert_time(a["ends"])
            span = f" ({when}&ndash;{ends})" if when or ends else ""
            fire = f' &mdash; <b>{a["fire_name"]}</b>' if a.get("fire_name") else ""
            return (f'<li style="margin-bottom:3px;"><span style="background:'
                    f'{tag_color};color:#fff;font-size:10px;padding:1px 6px;'
                    f'border-radius:8px;">{a["event"]}</span> {a["area"]}'
                    f'{fire}{span}</li>')
        parts = []
        for state, alist in grouped.items():
            parts.append(f'<div style="margin:6px 0 2px;font-weight:700;'
                         f'color:#37474f;">{state}</div>'
                         f'<ul style="margin:0 0 6px 18px;padding:0;">'
                         + "".join(evac_line(a) for a in alist) + '</ul>')
        for a in other:
            parts.append(f'<ul style="margin:0 0 6px 18px;padding:0;">'
                         f'{evac_line(a)}</ul>')
        inner += "".join(parts)

    script = (_EVAC_LIVE_SCRIPT_TEMPLATE.replace("__STATES_JSON__", json.dumps(states))
              if states else "")

    return (
        '<div class="fw-box" style="background:#fdecea;border:1px solid #f1a9a0;border-radius:8px;'
        'padding:12px 16px;margin:0 0 18px;">'
        '<div style="font-weight:800;color:#b71c1c;font-size:15px;margin-bottom:6px;">'
        'Evacuation Orders'
        '<span id="fw-evac-live-badge" style="display:none;font-weight:400;'
        'font-size:11px;color:#8a8574;"></span></div>'
        f'<div id="fw-evac-body">{inner}</div>'
        '<div style="margin-top:6px;font-size:11px;color:#8a8574;">'
        'Source: active NWS alerts (Evacuation Immediate / Civil Emergency '
        'Message). Fire name shown only when stated in the alert text '
        '&mdash; often not included by the issuing agency.</div></div>'
        f'{script}')


def render_incidents_html(incidents_data: dict) -> str:
    incidents = incidents_data.get("incidents", [])
    err = incidents_data.get("error")

    if err:
        inner = (f'<div style="font-size:12px;color:#b71c1c;">'
                 f'Live incident feed unavailable: {err}</div>')
    elif not incidents:
        inner = ('<div style="font-size:13px;color:#2e7d32;">'
                 'No actively-updated named incidents in the monitored states.</div>')
    else:
        def inc_line(i):
            evac_tag = (' <span style="background:#c62828;color:#fff;font-size:10px;'
                        'font-weight:700;padding:1px 5px;border-radius:3px;">EVAC</span>'
                        if i.get("evac") else "")
            return (f'<li style="margin-bottom:3px;">'
                    f'<a href="{i["link"]}" style="color:#1565c0;font-weight:600;'
                    f'text-decoration:none;">{i["name"]}</a> '
                    f'<span style="color:#607d8b;font-size:12px;">({i["state"]})</span>'
                    f'{evac_tag}</li>')

        def col(items):
            return ('<ul style="margin:0;padding:0 0 0 18px;">'
                    + "".join(inc_line(i) for i in items) + '</ul>')

        # Already sorted alphabetically by state (then name) -- split into two
        # columns sequentially (not interleaved) so each column reads as a
        # contiguous alphabetical run, table-based for email client
        # compatibility (CSS multi-column isn't reliably supported there).
        half = (len(incidents) + 1) // 2
        left, right = incidents[:half], incidents[half:]
        inner = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;">'
            '<tr>'
            f'<td valign="top" style="width:50%;">{col(left)}</td>'
            f'<td valign="top" style="width:50%;">{col(right) if right else ""}</td>'
            '</tr></table>'
        )

    return (
        '<div class="fw-box" style="background:#eef3f8;border:1px solid #b8cfe0;border-radius:8px;'
        'padding:12px 16px;margin:0 0 18px;">'
        '<div style="font-weight:800;color:#1565c0;font-size:15px;margin-bottom:6px;">'
        'Active Incidents (InciWeb)</div>'
        f'{inner}'
        '<div style="margin-top:6px;font-size:11px;color:#8a8574;">'
        'Source: InciWeb national incident feed, filtered to your monitored states. '
        'Each link goes to that incident&rsquo;s official page &mdash; the best place '
        'to check for evacuation detail the source feeds above may not have caught. '
        'A red EVAC tag means that incident&rsquo;s InciWeb overview text mentions '
        'evacuations &mdash; those incidents are never dropped from this list, even '
        'on a high-activity day.'
        '</div></div>')


def render_sitrep_html(sitrep: dict) -> str:
    parts = []
    if sitrep.get("pl") is not None:
        parts.append(f'National Preparedness Level <b>{sitrep["pl"]}</b>')
    if sitrep.get("ia_count") is not None:
        level = sitrep.get("ia_level") or ""
        parts.append(f'Initial attack {level.lower()} ({sitrep["ia_count"]} fires)')
    if sitrep.get("new_large") is not None:
        parts.append(f'{sitrep["new_large"]} new large incidents')
    if sitrep.get("uncontained") is not None:
        parts.append(f'{sitrep["uncontained"]} uncontained large fires')
    if sitrep.get("contained") is not None:
        parts.append(f'{sitrep["contained"]} contained')
    if not parts:
        return ""
    summary_line = " &middot; ".join(parts)

    gacc_line = ""
    if sitrep.get("gaccs"):
        items = " &middot; ".join(
            f'{g["code"]} PL{g["pl"]} ({g["incidents"]} incidents, {g["acres"]:,} ac)'
            for g in sitrep["gaccs"])
        gacc_line = (f'<div style="margin-top:6px;font-size:12px;color:#555;">'
                     f'<b>Your GACCs:</b> {items}</div>')

    return (
        '<div class="fw-box fw-sitrep-box" style="background:#faf3df;border:1px solid #d9c48a;'
        'border-radius:8px;padding:12px 16px;margin:32px 0 0;">'
        '<div style="font-weight:800;color:#9c7a16;font-size:15px;margin-bottom:6px;">'
        'National Sitrep Summary</div>'
        f'<div style="font-size:13px;">{summary_line}</div>{gacc_line}'
        '<div style="margin-top:6px;font-size:11px;color:#8a8574;">'
        'Source: NICC Incident Management Situation Report '
        '(see National Sitrep link above for full detail).</div></div>')


# --------------------------------------------------------------------------- #
# Rendering — plain text
# --------------------------------------------------------------------------- #
def render_text(gacc_rows, ranked, generated, sfp=None, want_weather=True,
                want_fm=True, sitrep=None, evac=None, incidents=None) -> str:
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

    if evac:
        lines.append("EVACUATION ORDERS (active NWS alerts)")
        if evac.get("error"):
            lines.append(f"  feed unavailable: {evac['error']}")
        elif not evac.get("grouped") and not evac.get("other"):
            lines.append("  No active evacuation orders.")
        else:
            def fmt_evac(a):
                fire = f" - {a['fire_name']}" if a.get("fire_name") else ""
                return f"    [{a['event']}] {a['area']}{fire}"
            for state, alist in evac.get("grouped", {}).items():
                lines.append(f"  {state}:")
                for a in alist:
                    lines.append(fmt_evac(a))
            for a in evac.get("other", []):
                lines.append(fmt_evac(a))
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

    if incidents:
        lines.append("ACTIVE INCIDENTS (InciWeb)")
        if incidents.get("error"):
            lines.append(f"  feed unavailable: {incidents['error']}")
        elif not incidents.get("incidents"):
            lines.append("  No actively-updated named incidents in the monitored states.")
        else:
            for i in incidents["incidents"]:
                evac_tag = " [EVAC]" if i.get("evac") else ""
                lines.append(f"  {i['name']} ({i['state']}){evac_tag} -> {i['link']}")
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
    lines.append("")

    if sitrep:
        parts = []
        if sitrep.get("pl") is not None:
            parts.append(f"National Preparedness Level {sitrep['pl']}")
        if sitrep.get("ia_count") is not None:
            level = sitrep.get("ia_level") or ""
            parts.append(f"Initial attack {level.lower()} ({sitrep['ia_count']} fires)")
        if sitrep.get("new_large") is not None:
            parts.append(f"{sitrep['new_large']} new large incidents")
        if sitrep.get("uncontained") is not None:
            parts.append(f"{sitrep['uncontained']} uncontained large fires")
        if sitrep.get("contained") is not None:
            parts.append(f"{sitrep['contained']} contained")
        if parts:
            lines.append("NATIONAL SITREP SUMMARY (NICC Incident Management Situation Report)")
            lines.append("  " + "  |  ".join(parts))
            if sitrep.get("gaccs"):
                for g in sitrep["gaccs"]:
                    lines.append(f"    {g['code']}: PL{g['pl']}  "
                                 f"{g['incidents']} incidents  {g['acres']:,} ac")
            lines.append("")

    lines.append(daily_quote(generated.date()))
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
# Evacuation orders assembly
# --------------------------------------------------------------------------- #
def build_evacuations(cfg: dict) -> Optional[dict]:
    """Reuses significant_fire_potential's states/contact_email since it's the
    same monitored footprint -- toggled independently via evacuation_orders."""
    ec = cfg.get("evacuation_orders", {})
    if not ec.get("enabled", True):
        return None
    sc = cfg.get("significant_fire_potential", {})
    states = [str(s).upper() for s in sc.get("states", [])]
    if not states:
        return None
    contact = sc.get("contact_email") or cfg.get("email", {}).get("from_addr", "n/a")
    # "states" travels with the result (even on failure/empty) so
    # render_evac_html() can embed it for the client-side live-refresh script
    # without needing a separate path back to config.yaml.
    result = {"grouped": {}, "other": [], "error": None, "states": states}
    try:
        alerts = fetch_evacuation_alerts(states, contact)
        grouped, other = group_alerts_by_state(alerts, states)
        result["grouped"], result["other"] = grouped, other
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# Active incidents (InciWeb) assembly
# --------------------------------------------------------------------------- #
def build_active_incidents(cfg: dict) -> Optional[dict]:
    """Reuses significant_fire_potential's states/contact_email (same
    monitored footprint) but is toggled independently via
    active_incidents.enabled. Free/keyless complement to the Evacuation
    Orders box -- lists named active incidents with a link to each one's
    official InciWeb page, which is the best place to check for evacuation
    detail InciWeb's own RSS text didn't happen to mention."""
    ac = cfg.get("active_incidents", {})
    if not ac.get("enabled", True):
        return None
    sc = cfg.get("significant_fire_potential", {})
    states = [str(s).upper() for s in sc.get("states", [])]
    if not states:
        return None
    contact = sc.get("contact_email") or cfg.get("email", {}).get("from_addr", "n/a")
    result = {"incidents": [], "error": None}
    try:
        result["incidents"] = fetch_inciweb_incidents(states, contact)
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# National sitrep summary (NICC Incident Management Situation Report)
# --------------------------------------------------------------------------- #
SITREP_URL = "https://www.nifc.gov/nicc-files/sitreprt.pdf"


def fetch_sitrep_pdf(contact: str = "n/a", timeout: int = 30) -> bytes:
    headers = {"User-Agent": f"prodigy-fire-weather-brief/1.0 ({contact})"}
    resp = requests.get(SITREP_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_sitrep_summary(pdf_bytes: bytes, gacc_codes: list[str]) -> Optional[dict]:
    """Best-effort extraction of headline numbers from page 1 of the daily
    NICC Incident Management Situation Report. Returns None if the expected
    text isn't found (e.g. NICC changes the report layout) -- callers should
    treat that the same as a fetch failure and just skip the summary."""
    try:
        import pdfplumber
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception:  # noqa: BLE001
        return None

    def _num(pattern: str):
        m = re.search(pattern, text)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            return None

    result = {
        "pl": _num(r"National Preparedness Level\s+(\d+)"),
        "new_large": _num(r"New large incidents:\s*([\d,]+)"),
        "contained": _num(r"Large fires contained:\s*([\d,]+)"),
        "uncontained": _num(r"Uncontained large fires:\s*([\d,]+)"),
        "ia_count": None,
        "ia_level": None,
        "gaccs": [],
    }
    ia_m = re.search(r"Initial attack activity:\s*([A-Za-z]+)\s*\((\d+)\s*fires\)", text)
    if ia_m:
        result["ia_level"], result["ia_count"] = ia_m.group(1), int(ia_m.group(2))

    for code in gacc_codes:
        row_m = re.search(
            rf"\b{re.escape(code)}\s+(\d+)\s+(\d+)\s+([\d,]+)\s+\d+\s+\d+\s+\d+\s+[\d,]+\s+-?[\d,]+",
            text)
        if row_m:
            result["gaccs"].append({
                "code": code,
                "pl": int(row_m.group(1)),
                "incidents": int(row_m.group(2)),
                "acres": int(row_m.group(3).replace(",", "")),
            })

    # If none of the expected fields were found, the report layout likely
    # changed -- signal failure rather than rendering an empty-looking box.
    if result["pl"] is None and not result["gaccs"]:
        return None
    return result


def build_sitrep_summary(cfg: dict, gacc_codes: list[str]) -> Optional[dict]:
    sc = cfg.get("national_sitrep", {})
    if not sc.get("enabled", True):
        return None
    contact = (cfg.get("significant_fire_potential", {}).get("contact_email")
               or cfg.get("email", {}).get("from_addr", "n/a"))
    try:
        pdf_bytes = fetch_sitrep_pdf(contact)
        return parse_sitrep_summary(pdf_bytes, gacc_codes)
    except Exception:  # noqa: BLE001 - never block the brief over this
        return None


# --------------------------------------------------------------------------- #
# Motivational quote (rotates daily, purely for the brief's closing line)
# --------------------------------------------------------------------------- #
MOTIVATIONAL_QUOTES = [
    "Preparedness today is containment tomorrow.",
    "The best fire season is the one nobody has to talk about.",
    "Every acre saved starts with the call made before the smoke.",
    "Readiness isn't a season -- it's a standard.",
    "The data doesn't fight fire. The people who trust it do.",
    "Know the fuels. Know the wind. Know your customer's risk.",
    "A quiet dispatch desk is the result of loud preparation.",
    "Fire danger doesn't wait for a good time to show up. Neither should we.",
    "The best briefing is the one that changes a decision.",
    "Complacency is the only fuel model that's always available.",
]


def daily_quote(on_date: dt.date) -> str:
    idx = on_date.timetuple().tm_yday % len(MOTIVATIONAL_QUOTES)
    return MOTIVATIONAL_QUOTES[idx]


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
    evac = build_evacuations(cfg)
    incidents = build_active_incidents(cfg)
    sitrep = build_sitrep_summary(cfg, [g.code for g, _ in gacc_rows])
    public_url = cfg.get("site", {}).get("public_url")

    logo_path = Path(__file__).resolve().parent / "assets" / "logo.png"
    logo_bytes = logo_path.read_bytes() if logo_path.exists() else None
    logo_preview_src = None
    logo_email_src = None
    if logo_bytes:
        logo_preview_src = "data:image/png;base64," + base64.b64encode(logo_bytes).decode()
        logo_email_src = "cid:logo"

    text = render_text(gacc_rows, ranked, now, sfp, want_weather, want_fm,
                       sitrep=sitrep, evac=evac, incidents=incidents)
    preview_html = render_html(gacc_rows, ranked, thresholds, now, sfp,
                               want_weather, want_fm,
                               logo_src=logo_preview_src, sitrep=sitrep,
                               public_url=public_url, evac=evac,
                               incidents=incidents)

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
                             logo_src=logo_email_src, sitrep=sitrep, evac=evac,
                             incidents=incidents)
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
