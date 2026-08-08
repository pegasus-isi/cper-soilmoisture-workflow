#!/usr/bin/env python3
"""Fetch NRCS AWDB (SCAN) data for CPER and normalise onto the common schema.

Generalisation of the drought workflow's ``fetch_snotel_data.py`` to any set of
AWDB station triplets. At CPER the stations are the two SCAN sites:

    2197:CO:SCAN  "CPER"     (soil moisture from 2013-09)
    2017:CO:SCAN  "Nunn #1"  (soil moisture from 1997-03)

Same anonymous AWDB REST API as SNOTEL:

    GET https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data
        ?stationTriplets=2197:CO:SCAN&elements=SMS:*,STO:*,PRCP&duration=DAILY

API gotchas learned here and in drought-workflow:
  * ``/stations`` silently ignores ``stateCodes``/``networkCodes`` filters —
    always pass explicit ``stationTriplets``.
  * A bare depth-resolved element code (``SMS``) can return nothing; the
    ``SMS:*`` wildcard form returns one series per sensor depth, with the depth
    in ``stationElement.heightDepth`` (inches, negative below surface).
  * Some elements carry duplicate sensors (``ordinal`` 2); we keep ordinal 1
    only so harmonize's (source, node, variable, timestamp) dedup cannot mix
    readings from two different physical sensors.

Depth-resolved variables encode the depth in the node id (``SCAN:2197@20cm``),
keeping the frozen long-format contract. Station lat/lon come from the AWDB
metadata endpoint unless overridden in config.

This is a BEST-EFFORT source in a multi-source merge: on persistent failure
(or an empty window) the declared output is still written, the miss is logged
loudly, and the job exits 0 — harmonize fails only if EVERY source is empty.
(USCRN Nunn 7 NNE went dark on 2026-05-28; no single station may be allowed
to kill a nowcast while others on site are live.)
"""

import argparse
import json
import logging
import sys
import time

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_awdb")

BASE_URL = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

COLUMNS = ["timestamp", "source", "node", "lat", "lon", "variable", "value", "unit"]

IN_TO_MM = lambda v: v * 25.4
F_TO_C = lambda v: (v - 32.0) * 5.0 / 9.0
PCT_TO_FRAC = lambda v: v / 100.0
MPH_TO_MS = lambda v: v * 0.44704

# element code -> (normalised variable, unit, converter)
ELEMENT_MAP = {
    "SMS": ("soil_moisture", "m3/m3", PCT_TO_FRAC),
    "STO": ("soil_temp", "degC", F_TO_C),
    "PRCP": ("precip", "mm", IN_TO_MM),
    "TOBS": ("air_temp", "degC", F_TO_C),
    "TAVG": ("air_temp", "degC", F_TO_C),
    "RHUM": ("rel_humidity", "percent", lambda v: v),
    "WSPD": ("wind_speed", "m/s", MPH_TO_MS),
}

# Elements measured at multiple depths; queried with the ":*" wildcard and
# emitted on depth-suffixed nodes.
DEPTH_ELEMENTS = {"SMS", "STO"}


def _get(path, params, timeout=90, retries=4, backoff=5):
    """GET the AWDB REST API with retry on transient failures."""
    last = None
    url = f"{BASE_URL}/{path}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            logger.warning("AWDB %s attempt %d/%d failed: %s",
                           path, attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"AWDB {path} failed after {retries} attempts: {last}")


def station_metadata(triplets):
    """Return {triplet: {lat, lon, elevation_m, name}} from /stations."""
    payload = _get("stations", {"stationTriplets": ",".join(triplets)})
    meta = {}
    for st in payload or []:
        meta[st.get("stationTriplet")] = {
            "lat": st.get("latitude"),
            "lon": st.get("longitude"),
            "elevation_m": (st.get("elevation") or 0) * 0.3048 or None,
            "name": st.get("name"),
        }
    return meta


def depth_cm(height_depth_in):
    """AWDB heightDepth (inches, negative below surface) -> depth in cm."""
    return int(round(abs(float(height_depth_in)) * 2.54))


def fetch(awdb: dict, start: str, end: str) -> pd.DataFrame:
    """Query data for every configured station and normalise the response."""
    stations = awdb.get("stations", [])
    triplets = [s["station_triplet"] for s in stations]
    by_triplet = {s["station_triplet"]: s for s in stations}
    meta = station_metadata(triplets)

    elements = []
    for code in awdb.get("elements", list(ELEMENT_MAP) + ["PREC"]):
        elements.append(f"{code}:*" if code in DEPTH_ELEMENTS else code)

    payload = _get("data", {
        "stationTriplets": ",".join(triplets),
        "elements": ",".join(elements),
        "duration": awdb.get("duration", "DAILY"),
        "beginDate": start,
        "endDate": end,
        "periodRef": "END",
        "returnFlags": "false",
    })
    if isinstance(payload, dict):
        payload = [payload]

    rows = []
    for station in payload or []:
        triplet = station.get("stationTriplet")
        cfg = by_triplet.get(triplet, {})
        node = cfg.get("node", f"AWDB:{triplet}")
        m = meta.get(triplet, {})
        lat = cfg.get("lat") or m.get("lat")
        lon = cfg.get("lon") or m.get("lon")

        have_increment = False
        prec_accum = []
        for series in station.get("data", []):
            elem = series.get("stationElement", {}) or {}
            code = elem.get("elementCode")
            ordinal = elem.get("ordinal")
            values = series.get("values", []) or []
            if ordinal not in (None, 1):
                continue  # duplicate physical sensor; keep the primary only
            if code == "PRCP" and values:
                have_increment = True
            if code == "PREC":
                prec_accum.extend(
                    (v.get("date"), float(v["value"]))
                    for v in values if v.get("value") is not None
                )
                continue
            mapping = ELEMENT_MAP.get(code)
            if not mapping:
                continue
            variable, unit, convert = mapping
            if code in DEPTH_ELEMENTS and elem.get("heightDepth") is not None:
                series_node = f"{node}@{depth_cm(elem['heightDepth'])}cm"
            else:
                series_node = node
            for v in values:
                val = v.get("value")
                if val is None:
                    continue
                rows.append({
                    "timestamp": v.get("date"),
                    "source": "scan",
                    "node": series_node,
                    "lat": lat,
                    "lon": lon,
                    "variable": variable,
                    "value": convert(float(val)),
                    "unit": unit,
                })

        # No increment precip returned: derive it from accumulated PREC.
        if not have_increment and prec_accum:
            s = pd.Series(dict(prec_accum)).sort_index()
            incr = s.diff().clip(lower=0).fillna(0.0)
            rows.extend({
                "timestamp": d, "source": "scan", "node": node,
                "lat": lat, "lon": lon, "variable": "precip",
                "value": IN_TO_MM(float(v)), "unit": "mm",
            } for d, v in incr.items())

    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp"])
    return df


def main():
    ap = argparse.ArgumentParser(description="Fetch NRCS AWDB (SCAN) data")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--start-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--end-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--output", required=True, help="Output observations CSV")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    awdb = config.get("awdb")
    if not awdb or not awdb.get("stations"):
        logger.error("No awdb.stations configured")
        pd.DataFrame(columns=COLUMNS).to_csv(args.output, index=False)
        sys.exit(1)

    dr = config.get("date_range", {})
    start = args.start_date or dr.get("start")
    end = args.end_date or dr.get("end")
    if not start or not end:
        logger.error("start/end date required")
        pd.DataFrame(columns=COLUMNS).to_csv(args.output, index=False)
        sys.exit(1)

    # BEST-EFFORT source: always write the declared output, log misses loudly,
    # exit 0. harmonize fails the run only when every source is empty.
    try:
        df = fetch(awdb, start, end)
    except RuntimeError as exc:
        logger.error("AWDB unavailable after retries; continuing with an "
                     "empty file (harmonize fails only if every source is "
                     "empty): %s", exc)
        df = pd.DataFrame(columns=COLUMNS)

    df.to_csv(args.output, index=False)
    if df.empty:
        logger.error("No AWDB observations for %s..%s; continuing empty.",
                     start, end)
        return
    logger.info("Wrote %d AWDB observations (%d variables, %d nodes) -> %s",
                len(df), df["variable"].nunique(), df["node"].nunique(),
                args.output)


if __name__ == "__main__":
    main()
