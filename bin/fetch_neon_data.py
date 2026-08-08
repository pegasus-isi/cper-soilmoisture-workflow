#!/usr/bin/env python3
"""Fetch NEON soil water content at CPER and normalise onto the common schema.

NEON site CPER (domain D10) instruments five soil plots with profile soil-water
sensors, published monthly as DP1.00094.001 ("Soil water content and water ion
content"), 2016-07 to present.

    GET {api}/sites/CPER                                   (anonymous)
    GET {api}/data/DP1.00094.001/CPER/{YYYY-MM}?package=basic
        with header  X-API-Token: <token>                  (403 without it)

The API token is free (data.neonscience.org user profile) but it is a
credential: HTCondor runs jobs in a clean environment, so the generator
captures NEON_TOKEN at generation time and injects it with add_env(). An
exported shell variable never reaches the job.

Per month we read the 30-minute SWS files (one per soil-plot x depth-level),
keep VSWCMean where VSWCFinalQF == 0, aggregate to daily means, and emit on
depth-suffixed nodes ``NEON:CPER:SP{plot}@{depth}cm``. Depths and per-plot
coordinates come from the month's sensor_positions file.

This is a BEST-EFFORT source in a multi-source merge: any persistent failure
(including a missing token) logs an ERROR, writes an empty output, and exits 0.
harmonize fails only if every source came back empty.
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_neon")

COLUMNS = ["timestamp", "source", "node", "lat", "lon", "variable", "value", "unit"]

# NEON.D10.CPER.DP1.00094.001.<HOR>.<VER>.030.SWS_30_minute.<YYYY-MM>...csv
SWS_NAME_RE = re.compile(
    r"\.(?P<hor>\d{3})\.(?P<ver>\d{3})\.030\.SWS_30_minute\."
)


def _get(url, token=None, timeout=120, retries=4, backoff=10, as_json=True):
    """GET with retry on transient failures; raises RuntimeError at the end."""
    headers = {"X-API-Token": token} if token else {}
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, ValueError) as exc:
            last = exc
            logger.warning("NEON fetch attempt %d/%d for %s failed: %s",
                           attempt, retries, url.split("?")[0], exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"NEON fetch failed after {retries} attempts: {last}")


def available_months(api_base, site, product, start, end):
    """Intersect the product's published months with the [start, end] window."""
    payload = _get(f"{api_base}/sites/{site}")
    months = []
    for p in payload["data"]["dataProducts"]:
        if p["dataProductCode"] == product:
            months = p["availableMonths"]
            break
    lo, hi = start[:7], end[:7]
    return [m for m in months if lo <= m <= hi]


def parse_sensor_positions(text):
    """sensor_positions CSV -> {"HOR.VER": (depth_cm, lat, lon)}.

    Column names have drifted across NEON releases (referenceLatitude vs
    locationReferenceLatitude), so match by substring.
    """
    df = pd.read_csv(io.StringIO(text), dtype=str)

    def col(*subs):
        for c in df.columns:
            if all(s.lower() in c.lower() for s in subs):
                return c
        return None

    key_col = col("HOR.VER") or df.columns[1]
    z_col = col("zOffset")
    lat_col = col("reference", "latitude") or col("latitude")
    lon_col = col("reference", "longitude") or col("longitude")

    positions = {}
    for _, row in df.iterrows():
        try:
            z = float(row[z_col])
            positions[str(row[key_col])] = (
                int(round(abs(z) * 100)),
                float(row[lat_col]) if lat_col else None,
                float(row[lon_col]) if lon_col else None,
            )
        except (TypeError, ValueError, KeyError):
            continue
    return positions


def fetch_month(api_base, site, product, month, package, token):
    """Fetch one product-month; returns long-format daily rows."""
    listing = _get(f"{api_base}/data/{product}/{site}/{month}?package={package}",
                   token=token)
    files = listing["data"].get("files", [])

    positions = {}
    pos_file = next((f for f in files if "sensor_positions" in f["name"]), None)
    if pos_file:
        positions = parse_sensor_positions(
            _get(pos_file["url"], token=token, as_json=False)
        )

    rows = []
    for f in files:
        m = SWS_NAME_RE.search(f["name"])
        if not m or not f["name"].endswith(".csv"):
            continue
        hor, ver = m.group("hor"), m.group("ver")
        depth_cm, lat, lon = positions.get(f"{hor}.{ver}", (None, None, None))
        df = pd.read_csv(io.StringIO(_get(f["url"], token=token, as_json=False)))
        if "VSWCMean" not in df.columns:
            continue
        ok = df[(df.get("VSWCFinalQF", 0) == 0) & df["VSWCMean"].notna()].copy()
        if ok.empty:
            continue
        ok["timestamp"] = pd.to_datetime(ok["startDateTime"], utc=True)
        daily = ok.set_index("timestamp")["VSWCMean"].resample("1D").mean().dropna()
        plot = f"SP{int(hor)}"
        node = (f"NEON:{site}:{plot}@{depth_cm}cm" if depth_cm is not None
                else f"NEON:{site}:{plot}.{ver}")
        # str(ts) ("YYYY-MM-DD HH:MM:SS+00:00"), NOT ts.isoformat(): harmonize
        # parses the concatenated timestamp column with the format pandas>=2.0
        # infers from the first row, so a "T" separator here would coerce every
        # NEON row to NaT (silently dropped) when merged after another source.
        rows.extend({
            "timestamp": str(ts), "source": "neon", "node": node,
            "lat": lat, "lon": lon, "variable": "soil_moisture",
            "value": round(float(v), 4), "unit": "m3/m3",
        } for ts, v in daily.items())
    return rows


def main():
    ap = argparse.ArgumentParser(description="Fetch NEON soil water content")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--start-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--end-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--month", help="Fetch a single YYYY-MM (per-month fan-out)")
    ap.add_argument("--output", required=True, help="Output observations CSV")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    neon = config.get("neon", {})
    site = neon.get("site", "CPER")
    product = neon.get("products", {}).get("soil_moisture", "DP1.00094.001")
    package = neon.get("package", "basic")
    api_base = neon.get("api_base", "https://data.neonscience.org/api/v0")

    def bail(msg):
        # BEST-EFFORT source: declared output first, then a clean exit 0.
        logger.error("%s Continuing with an empty file (harmonize fails only "
                     "if every source is empty).", msg)
        pd.DataFrame(columns=COLUMNS).to_csv(args.output, index=False)
        sys.exit(0)

    token = os.environ.get("NEON_TOKEN")
    if not token:
        bail("NEON_TOKEN is not set in the job environment; the NEON data API "
             "returns 403 without it. The generator must inject it via "
             "add_env(NEON_TOKEN=...) - an exported shell variable does not "
             "reach an HTCondor job.")

    dr = config.get("date_range", {})
    start = args.start_date or dr.get("start")
    end = args.end_date or dr.get("end")
    if args.month:
        months = [args.month]
    elif start and end:
        try:
            months = available_months(api_base, site, product, start, end)
        except RuntimeError as exc:
            bail(f"NEON site listing unavailable: {exc}.")
    else:
        bail("No date range: pass --month or --start-date/--end-date.")

    rows, failed = [], []
    for month in months:
        try:
            month_rows = fetch_month(api_base, site, product, month,
                                     package, token)
            rows.extend(month_rows)
            logger.info("NEON %s %s: %d daily observations",
                        product, month, len(month_rows))
        except RuntimeError as exc:
            failed.append(month)
            logger.error("NEON month %s failed after retries: %s", month, exc)

    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_csv(args.output, index=False)
    if failed:
        logger.error("NEON: %d/%d months failed (%s); partial output written.",
                     len(failed), len(months), ", ".join(failed))
    if df.empty:
        logger.error("No NEON observations for %s; continuing empty "
                     "(best-effort source).", months or "requested window")
    else:
        logger.info("Wrote %d NEON observations (%d nodes) -> %s",
                    len(df), df["node"].nunique(), args.output)


if __name__ == "__main__":
    main()
