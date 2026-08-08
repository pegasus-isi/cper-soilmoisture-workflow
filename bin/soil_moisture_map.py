#!/usr/bin/env python3
"""Point-scale soil moisture layer for CPER.

Ported from drought-workflow's soil_moisture_map.py and adapted for the CPER
node convention, where depth-resolved sensors publish on nodes like
``USCRN:94074@20cm`` (station id + '@' + depth). Emits per-node current value,
period statistics, anomaly and dryness class, plus a per-station surface-zone
aggregation and an IDW grid over the site bbox.

The IDW grid here is a point-scale preview only: in the target architecture
(SPEC.md section 6) the real spatial product comes from the
zone-anchored upscaling stage, which replaces distance-only interpolation with
soil/terrain-aware estimation. This layer is the stage that feeds it.

Dryness breakpoints come from config analysis.sm_thresholds (shortgrass-steppe
defaults) rather than the forest defaults.

Staleness: observations are truncated to --as-of (the fetch-window end,
injected by the generator; falls back to run time), so "current" is the last
value at or before that date and period statistics never include later data.
Nodes older than analysis.max_current_age_days are flagged stale and excluded
from the surface aggregation, the region mean, and the IDW grid — with USCRN
Nunn dark since 2026-05-28, a weeks-old reading must not be published as
current conditions (SPEC.md section 9, constraint C5).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import drought_common as dc  # noqa: E402

DEFAULT_THRESHOLDS = [0.06, 0.12, 0.20, 0.30]
DEFAULT_LABELS = ["very_dry", "dry", "moderate", "moist", "saturated"]
DEFAULT_MAX_AGE_DAYS = 5

NODE_DEPTH_RE = re.compile(r"^(?P<station>.+)@(?P<depth>\d+)cm$")


def split_node(node):
    """'SCAN:2197@20cm' -> ('SCAN:2197', 20); bare nodes -> (node, None)."""
    m = NODE_DEPTH_RE.match(str(node))
    if m:
        return m.group("station"), int(m.group("depth"))
    return str(node), None


def build(obs_paths, config, as_of=None):
    df = dc.load_observations(obs_paths)
    bbox = config.get("bbox", {})
    analysis = config.get("analysis", {})
    sm_cfg = analysis.get("sm_thresholds", {})
    thresholds = sm_cfg.get("breakpoints", DEFAULT_THRESHOLDS)
    labels = sm_cfg.get("labels", DEFAULT_LABELS)
    surface_max_cm = analysis.get("surface_depth_max_cm", 20)
    max_age_days = analysis.get("max_current_age_days", DEFAULT_MAX_AGE_DAYS)
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    locs = dc.node_locations(df)

    # Hard-bound everything to as_of: an observation after the reference date
    # must not become "current" (it would carry a negative age and can never
    # be flagged stale) nor leak into the period statistics.
    df = df[df["timestamp"] <= as_of]

    sm = df[df["variable"] == "soil_moisture"]
    points = []
    series = {}
    for node, grp in sm.groupby("node"):
        daily = dc.daily_series(grp, "soil_moisture", node=node)
        if daily.empty:
            continue
        lat, lon = locs.get(node, (None, None))
        station, depth = split_node(node)
        current = float(daily.iloc[-1])
        last_ts = daily.index[-1]
        age_days = (as_of - last_ts).total_seconds() / 86400.0
        points.append({
            "node": node,
            "station": station,
            "depth_cm": depth,
            "lat": lat,
            "lon": lon,
            "current": round(current, 4),
            "current_date": str(last_ts.date()),
            "age_days": round(age_days, 1),
            "stale": age_days > max_age_days,
            "period_mean": round(float(daily.mean()), 4),
            "period_min": round(float(daily.min()), 4),
            "anomaly": round(current - float(daily.mean()), 4),
            "class": dc.classify(current, thresholds, labels),
        })
        series[node] = {
            str(d.date()): round(float(v), 4) for d, v in daily.items()
        }

    # Per-station surface zone (depths <= surface_max_cm, or depth-less nodes):
    # one value per physical station, so the IDW preview never stacks several
    # depths at identical coordinates. Stale nodes are excluded — everything
    # from here down claims to describe conditions at as_of.
    stations = {}
    for p in points:
        if p["stale"]:
            continue
        if p["depth_cm"] is not None and p["depth_cm"] > surface_max_cm:
            continue
        stations.setdefault(p["station"], []).append(p)
    station_points = []
    for station, plist in sorted(stations.items()):
        vals = [p["current"] for p in plist]
        anoms = [p["anomaly"] for p in plist]
        lat = next((p["lat"] for p in plist if p["lat"] is not None), None)
        lon = next((p["lon"] for p in plist if p["lon"] is not None), None)
        current = sum(vals) / len(vals)
        station_points.append({
            "station": station,
            "lat": lat,
            "lon": lon,
            "n_depths": len(plist),
            "surface_current": round(current, 4),
            "surface_anomaly": round(sum(anoms) / len(anoms), 4),
            "class": dc.classify(current, thresholds, labels),
        })

    grid = dc.idw_grid(
        [(p["lat"], p["lon"], p["surface_current"]) for p in station_points],
        bbox,
    )
    region_current = (
        round(sum(p["surface_current"] for p in station_points)
              / len(station_points), 4)
        if station_points else None
    )

    return {
        "layer": "soil_moisture_points",
        "units": "m3/m3 (volumetric fraction)",
        "site": config.get("site"),
        "as_of": str(as_of.date()),
        "max_current_age_days": max_age_days,
        "surface_depth_max_cm": surface_max_cm,
        "n_nodes": len(points),
        "n_stale_nodes": sum(1 for p in points if p["stale"]),
        "n_stations": len(station_points),
        "region_mean_surface_current": region_current,
        "classes": labels,
        "thresholds": thresholds,
        "stations": station_points,
        "points": points,
        "grid": (
            {"lats": grid[0], "lons": grid[1], "values": grid[2],
             "note": "distance-only IDW preview; superseded by zone-anchored "
                     "upscaling in M4"}
            if grid else None
        ),
        "daily_series": series,
    }


def main():
    ap = argparse.ArgumentParser(description="Build point-scale soil moisture layer")
    ap.add_argument("--observations", nargs="+", required=True,
                    help="Harmonized observation CSV(s)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--as-of",
                    help="Reference date (YYYY-MM-DD) that 'current' is "
                         "measured against; the generator passes the fetch-"
                         "window end. Defaults to run time (UTC).")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    result = build(args.observations, config, as_of=args.as_of)
    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"soil_moisture_points: {result['n_nodes']} depth-nodes "
          f"({result['n_stale_nodes']} stale) across {result['n_stations']} "
          f"fresh stations as of {result['as_of']}, region surface mean "
          f"{result['region_mean_surface_current']}")


if __name__ == "__main__":
    main()
