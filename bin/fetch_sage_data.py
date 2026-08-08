#!/usr/bin/env python3
"""Fetch edge-sensor observations from the Sage Continuum / Waggle data API.

Sage exposes an anonymous public query API:
    POST https://data.sagecontinuum.org/api/v1/query
    {"start": "<iso|relative>", "end": "<iso>", "filter": {"name": "<measurement>"}}

We query the measurement names listed in region_config.json, normalise them onto
a common variable vocabulary, attach node coordinates (from the config) and emit
a long-format CSV that every downstream layer consumes:

    timestamp, source, node, lat, lon, variable, value, unit

If Sage returns nothing for the region/period (the network is sparse and may have
no nodes near GLEES), we still write a valid, possibly empty, CSV and warn — we do
not fabricate observations.
"""

import argparse
import json
import logging
import sys

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_sage")

SAGE_QUERY_URL = "https://data.sagecontinuum.org/api/v1/query"

# Canonical units for each normalised variable.
UNITS = {
    "soil_moisture": "m3/m3",
    "soil_temp": "degC",
    "air_temp": "degC",
    "rel_humidity": "percent",
    "pressure": "Pa",
    "wind_speed": "m/s",
    "precip": "mm",
}

COLUMNS = ["timestamp", "source", "node", "lat", "lon", "variable", "value", "unit"]


def query_sage(measurement: str, start: str, end: str, vsn=None, timeout: int = 120):
    """Run a single Sage data-API query, return a list of records (possibly empty).

    A node (vsn) filter is strongly recommended: an unscoped query over a common
    measurement (e.g. env.temperature) spans the whole network and can return
    enormous results, so callers without configured nodes should not get here.
    """
    flt = {"name": measurement}
    if vsn:
        flt["vsn"] = vsn
    body = {"start": start, "end": end, "filter": flt}
    try:
        resp = requests.post(SAGE_QUERY_URL, json=body, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Sage query for %s failed: %s", measurement, exc)
        return []

    records = []
    # The API streams newline-delimited JSON objects.
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def fetch(config: dict, start: str, end: str) -> pd.DataFrame:
    """Fetch every configured measurement and normalise into the common schema."""
    sage_cfg = config.get("sage", {})
    bbox = config.get("bbox", {})
    node_coords = {n["vsn"]: n for n in sage_cfg.get("nodes", []) if "vsn" in n}
    measurements = sage_cfg.get("measurements", {})
    vsns = list(node_coords.keys())

    if not vsns:
        logger.warning(
            "No Sage nodes (vsn) configured under sage.nodes. Skipping the Sage "
            "query to avoid an unbounded network-wide pull; the workflow will use "
            "GLEES only. Add nodes with vsn/lat/lon to region_config.json to "
            "include edge observations."
        )
        return pd.DataFrame(columns=COLUMNS)

    rows = []
    for variable, names in measurements.items():
        for name in names:
            for vsn in vsns:
                logger.info("Querying Sage '%s' -> %s @ %s", name, variable, vsn)
                for rec in query_sage(name, start, end, vsn=vsn):
                    meta = rec.get("meta", {})
                    rvsn = meta.get("vsn") or meta.get("node") or vsn
                    coord = node_coords.get(rvsn, {})
                    lat = coord.get("lat")
                    lon = coord.get("lon")

                    # Keep nodes inside the region when we know where they are.
                    if lat is not None and lon is not None and bbox:
                        if not (
                            bbox.get("min_lat", -90) <= lat <= bbox.get("max_lat", 90)
                            and bbox.get("min_lon", -180) <= lon <= bbox.get("max_lon", 180)
                        ):
                            continue

                    value = rec.get("value")
                    if value is None:
                        continue
                    rows.append(
                        {
                            "timestamp": rec.get("timestamp"),
                            "source": "sage",
                            "node": rvsn,
                            "lat": lat,
                            "lon": lon,
                            "variable": variable,
                            "value": value,
                            "unit": UNITS.get(variable, ""),
                        }
                    )

    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        # Sage relative humidity / soil moisture are sometimes reported in % — keep
        # volumetric soil moisture as a 0-1 fraction for consistency downstream.
        sm = df["variable"] == "soil_moisture"
        df.loc[sm & (df["value"] > 1.5), "value"] /= 100.0
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp"])
    return df


def main():
    ap = argparse.ArgumentParser(description="Fetch Sage Continuum edge-sensor data")
    ap.add_argument("--config", required=True, help="region_config.json")
    ap.add_argument("--start-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--end-date", help="YYYY-MM-DD (overrides config)")
    ap.add_argument("--output", required=True, help="Output observations CSV")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)

    dr = config.get("date_range", {})
    start = args.start_date or dr.get("start")
    end = args.end_date or dr.get("end")
    if not start or not end:
        logger.error("start/end date required (via --start-date/--end-date or config)")
        sys.exit(1)

    df = fetch(config, start, end)
    df.to_csv(args.output, index=False)

    if df.empty:
        logger.warning(
            "No Sage observations returned for %s..%s in this region. "
            "Wrote an empty observations file; downstream layers will rely on GLEES.",
            start,
            end,
        )
    else:
        logger.info(
            "Wrote %d Sage observations (%d nodes, %d variables) -> %s",
            len(df),
            df["node"].nunique(),
            df["variable"].nunique(),
            args.output,
        )


if __name__ == "__main__":
    main()
