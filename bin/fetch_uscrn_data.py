#!/usr/bin/env python3
"""Fetch NOAA USCRN daily data for CPER and normalise onto the common schema.

USCRN station Nunn 7 NNE (WBAN 94074) sits on the Central Plains Experimental
Range itself (its official name is "Ag. Res. Svc. Central Plains Exp. Range")
and is the primary public in-situ soil-moisture source for this workflow: five
soil-moisture and five soil-temperature depths (5/10/20/50/100 cm), anonymous
access, continuous since ~2009.

Data layout: one fixed-format whitespace-separated file per station-year,

    https://www.ncei.noaa.gov/pub/data/uscrn/products/daily01/
        {YYYY}/CRND0103-{YYYY}-CO_Nunn_7_NNE.txt

with missing values encoded as -9999.x (met) or -99.000 (soil moisture).
Station lat/lon are carried in each row; we prefer those over config values.

Depth-resolved variables keep the frozen long-format contract by encoding the
depth in the node id: ``USCRN:94074@5cm``. Non-depth variables use the bare
station node id.

This is a BEST-EFFORT source in a multi-source merge: on persistent failure
(or an empty window) the declared output is still written, the miss is logged
loudly, and the job exits 0 — harmonize fails only if EVERY source is empty.
This is not hypothetical: the station stopped publishing on 2026-05-28 (a
sensor/telemetry outage visible in both the daily and hourly products), and a
nowcast must keep running on the SCAN stations while it is down.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_uscrn")

COLUMNS = ["timestamp", "source", "node", "lat", "lon", "variable", "value", "unit"]

# daily01 format, verified against headers.txt on 2026-07-29 (28 fields).
DAILY01_FIELDS = [
    "WBANNO", "LST_DATE", "CRX_VN", "LONGITUDE", "LATITUDE",
    "T_DAILY_MAX", "T_DAILY_MIN", "T_DAILY_MEAN", "T_DAILY_AVG",
    "P_DAILY_CALC", "SOLARAD_DAILY",
    "SUR_TEMP_DAILY_TYPE", "SUR_TEMP_DAILY_MAX", "SUR_TEMP_DAILY_MIN",
    "SUR_TEMP_DAILY_AVG",
    "RH_DAILY_MAX", "RH_DAILY_MIN", "RH_DAILY_AVG",
    "SOIL_MOISTURE_5_DAILY", "SOIL_MOISTURE_10_DAILY", "SOIL_MOISTURE_20_DAILY",
    "SOIL_MOISTURE_50_DAILY", "SOIL_MOISTURE_100_DAILY",
    "SOIL_TEMP_5_DAILY", "SOIL_TEMP_10_DAILY", "SOIL_TEMP_20_DAILY",
    "SOIL_TEMP_50_DAILY", "SOIL_TEMP_100_DAILY",
]

SOIL_DEPTHS_CM = [5, 10, 20, 50, 100]

# Scalar (non-depth) variables: field -> (variable, unit).
SCALAR_MAP = {
    "T_DAILY_AVG": ("air_temp", "degC"),
    "P_DAILY_CALC": ("precip", "mm"),
    "RH_DAILY_AVG": ("rel_humidity", "percent"),
    "SUR_TEMP_DAILY_AVG": ("surface_temp", "degC"),
}

MISSING_CUTOFF = -98.0  # sentinels are -99.000 and -9999.x


def _get(url, timeout=90, retries=4, backoff=5):
    """GET with retry on transient failures; None on 404 (year not published)."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last = exc
            logger.warning("USCRN fetch attempt %d/%d for %s failed: %s",
                           attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"USCRN fetch failed after {retries} attempts: {last}")


def parse_year(text, node):
    """Parse one station-year daily01 file into long-format rows."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != len(DAILY01_FIELDS):
            continue
        rec = dict(zip(DAILY01_FIELDS, parts))
        date = rec["LST_DATE"]
        try:
            lat, lon = float(rec["LATITUDE"]), float(rec["LONGITUDE"])
        except ValueError:
            lat = lon = None

        def emit(field, variable, unit, depth_cm=None):
            try:
                val = float(rec[field])
            except (ValueError, KeyError):
                return
            if val <= MISSING_CUTOFF:
                return
            rows.append({
                "timestamp": date,
                "source": "uscrn",
                "node": f"{node}@{depth_cm}cm" if depth_cm else node,
                "lat": lat,
                "lon": lon,
                "variable": variable,
                "value": val,
                "unit": unit,
            })

        # Air temp: prefer the true daily average, fall back to (max+min)/2.
        try:
            has_avg = float(rec["T_DAILY_AVG"]) > MISSING_CUTOFF
        except ValueError:
            has_avg = False
        emit("T_DAILY_AVG" if has_avg else "T_DAILY_MEAN", "air_temp", "degC")
        for field, (variable, unit) in SCALAR_MAP.items():
            if field == "T_DAILY_AVG":
                continue
            emit(field, variable, unit)
        for d in SOIL_DEPTHS_CM:
            emit(f"SOIL_MOISTURE_{d}_DAILY", "soil_moisture", "m3/m3", depth_cm=d)
            emit(f"SOIL_TEMP_{d}_DAILY", "soil_temp", "degC", depth_cm=d)
    return rows


def fetch(uscrn: dict, start: str, end: str) -> pd.DataFrame:
    """Fetch and parse every station-year overlapping [start, end].

    Raises RuntimeError only when a needed year fails persistently with a
    non-404 error; missing years (404) are skipped with a warning.
    """
    slug = uscrn["station_file_slug"]
    node = uscrn.get("node", f"USCRN:{uscrn.get('wban', slug)}")
    base = uscrn.get("base_url",
                     "https://www.ncei.noaa.gov/pub/data/uscrn/products/daily01")
    y0, y1 = int(start[:4]), int(end[:4])

    rows = []
    for year in range(y0, y1 + 1):
        url = f"{base}/{year}/CRND0103-{year}-{slug}.txt"
        text = _get(url)
        if text is None:
            logger.warning("USCRN file for %d not published (404); skipping", year)
            continue
        year_rows = parse_year(text, node)
        rows.extend(year_rows)
        logger.info("USCRN %d: %d observations", year, len(year_rows))

    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d", utc=True)
        df = df[(df["timestamp"] >= pd.Timestamp(start, tz="UTC"))
                & (df["timestamp"] <= pd.Timestamp(end, tz="UTC"))]
    return df


def main():
    ap = argparse.ArgumentParser(description="Fetch NOAA USCRN daily data")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--start-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--end-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--output", required=True, help="Output observations CSV")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    uscrn = config.get("uscrn")
    if not uscrn or not uscrn.get("station_file_slug"):
        logger.error("No uscrn.station_file_slug configured")
        pd.DataFrame(columns=COLUMNS).to_csv(args.output, index=False)
        sys.exit(1)

    dr = config.get("date_range", {})
    start = args.start_date or dr.get("start")
    end = args.end_date or dr.get("end") or datetime.utcnow().strftime("%Y-%m-%d")
    if not start:
        logger.error("start date required (--start-date or config date_range.start)")
        pd.DataFrame(columns=COLUMNS).to_csv(args.output, index=False)
        sys.exit(1)

    # BEST-EFFORT source: always write the declared output, log misses loudly,
    # exit 0. harmonize fails the run only when every source is empty.
    try:
        df = fetch(uscrn, start, end)
    except RuntimeError as exc:
        logger.error("USCRN unavailable after retries; continuing with an "
                     "empty file (harmonize fails only if every source is "
                     "empty): %s", exc)
        df = pd.DataFrame(columns=COLUMNS)

    df.to_csv(args.output, index=False)
    if df.empty:
        logger.error("No USCRN observations for %s..%s (station outage? "
                     "Nunn 7 NNE last published 2026-05-28 as of this "
                     "writing); continuing empty.", start, end)
        return
    logger.info("Wrote %d USCRN observations (%d variables, %d nodes) -> %s",
                len(df), df["variable"].nunique(), df["node"].nunique(),
                args.output)


if __name__ == "__main__":
    main()
