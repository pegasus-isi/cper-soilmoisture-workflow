#!/usr/bin/env python3
"""Upscale station soil moisture to the whole site (M4).

Turns the point-scale layer into the gridded product the researcher asked for:
"a real time soil moisture map for the entire area" (C1), interpolated in a way
that respects soil and topography rather than distance alone (C2), with an
uncertainty layer and station-free-zone flags because the network is small (C5).

Two tiers, so it degrades instead of breaking:

  **Tier 1 — zone-anchored relative-saturation upscaling (always available).**
  Work in relative saturation S = theta / theta_s instead of raw VWC. A station
  reporting S = 0.4 says "this profile is at 40 % of its own pore space", which
  transfers across a soil boundary; a station reporting 0.14 m3/m3 does not.
  Per zone, average the members' current S and their climatological S for the
  month, then paint each pixel with

      theta(pixel) = (S_clim_zone + dS_zone) * theta_s(pixel)

  clipped to [theta_r, theta_s]. Because theta_s comes from the *pixel*, the map
  carries real within-zone soil texture instead of flat zone blocks, and the
  same relative wetness yields less water on the sandy zone than the loamy one.

  A zone with no station borrows dS and S_clim from its nearest analogue **in
  covariate space** (zone feature centroids from delineate_zones), never its
  nearest neighbour in metres — distance-only substitution is what C2 rules out.

  **Tier 2 — regression + residual interpolation (refinement).** Random forest
  of current station S on covariates, predicted over the grid, plus IDW of the
  station residuals. Gated on `analysis.min_stations_for_regression` distinct
  reporting locations (default 8) — *not* on min_reporting_stations. With the
  ~7 locations the public network provides today, a 49-covariate forest fitted
  to 7 points is decoration, so tier 2 stays off and the JSON says so. It
  engages unchanged when the ARS network arrives.

Uncertainty per pixel combines three terms in quadrature: the spread of the
contributing stations, a distance term that saturates at the between-station
spread over a configured decorrelation length, and a flat penalty inside
station-free zones.

Skill is leave-one-station-out: drop each station, re-estimate, and read the
map at that station's pixel. Reported against two baselines (site mean and
climatology-only) because an RMSE with nothing to compare it to is not a skill
number. Without this the map is a picture, not a research product.
"""

import argparse
import json
import logging
import math
import sys
import time

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import rowcol, xy
from rasterio.warp import transform as warp_transform

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("estimate_soil_moisture")

ZONE_NODATA = -1
DEFAULT_MIN_STATIONS_REGRESSION = 8
DEFAULT_DECORRELATION_M = 2000.0
DEFAULT_STATION_FREE_PENALTY = 0.03
DEFAULT_MIN_SPREAD = 0.01          # m3/m3 uncertainty floor
IDW_POWER = 2.0

# Which POLARIS depth intervals make up "the surface layer" the map describes.
# 0-15 cm, thickness-weighted; 15_30 straddles surface_depth_max_cm=20 and is
# left out rather than silently stretching the layer the map claims to be.
SURFACE_INTERVALS = (("0_5", 5.0), ("5_15", 10.0))


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def load_surface_stations(points, fingerprints, as_of, surface_max_cm,
                          max_age_days):
    """Current + climatological state of every fresh surface station.

    One row per physical station: the depth-nodes at or above
    surface_depth_max_cm are averaged, stale nodes are excluded (a weeks-old
    reading must never be published as current — USCRN Nunn has been dark since
    2026-05-28), and the climatology is the station's own monthly mean for the
    as-of month from its M3 fingerprint.

    Staleness is re-derived here from each node's ``current_date`` against *this*
    job's --as-of rather than trusting the ``stale`` flag in the points layer.
    That flag was computed against the points layer's own as-of; if the two ever
    disagree, trusting it would silently pair (say) July readings with January
    climatology and publish the result as current conditions.
    """
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts.tzinfo is not None:
        as_of_ts = as_of_ts.tz_localize(None)
    as_of_month = int(as_of_ts.month)
    clim_by_node, std_by_node = {}, {}
    for fp in fingerprints:
        for node in fp.get("nodes", []):
            if node.get("insufficient_data"):
                continue
            monthly = node.get("monthly_mean") or {}
            val = monthly.get(str(as_of_month), monthly.get(as_of_month))
            if val is not None:
                clim_by_node[node["node"]] = float(val)
            if node.get("std") is not None:
                std_by_node[node["node"]] = float(node["std"])

    rows, n_stale = [], 0
    for p in points.get("points", []):
        depth = p.get("depth_cm")
        if depth is not None and depth > surface_max_cm:
            continue
        if p.get("current") is None or p.get("lat") is None:
            continue
        if p.get("current_date"):
            age = (as_of_ts - pd.Timestamp(p["current_date"])).days
        else:
            age = p.get("age_days")
        if age is None or age > max_age_days:
            n_stale += 1
            continue
        # period_mean is the fallback climatology: it is the mean over the
        # whole fetch window rather than the month, so it is used only when a
        # node has no fingerprint at all.
        clim = clim_by_node.get(p["node"])
        rows.append({
            "node": p["node"], "station": p["station"], "depth_cm": depth,
            "lat": float(p["lat"]), "lon": float(p["lon"]),
            "current": float(p["current"]),
            "clim": float(clim) if clim is not None else p.get("period_mean"),
            "clim_source": "fingerprint_monthly" if clim is not None
                           else "period_mean",
            "std": std_by_node.get(p["node"]),
            "age_days": age,
        })
    if n_stale:
        logger.info("%d surface node(s) excluded as stale (> %d d before %s)",
                    n_stale, max_age_days, as_of_ts.date())
    nodes = pd.DataFrame(rows)
    if nodes.empty:
        return nodes, nodes

    agg = nodes.groupby("station").agg(
        lat=("lat", "first"), lon=("lon", "first"),
        n_nodes=("node", "count"),
        current=("current", "mean"), clim=("clim", "mean"),
        std=("std", "mean"), age_days=("age_days", "max"),
        clim_source=("clim_source", lambda s: sorted(set(s))[0]),
    ).reset_index()
    return agg, nodes


def surface_theta(stack, names):
    """Thickness-weighted theta_s / theta_r over the surface intervals."""
    out = {}
    for prop in ("theta_s", "theta_r"):
        num = np.zeros(stack.shape[1:], dtype="float64")
        den = 0.0
        for suffix, thick in SURFACE_INTERVALS:
            band = f"{prop}_{suffix}"
            if band in names:
                num += stack[names.index(band)].astype("float64") * thick
                den += thick
        if den == 0:
            raise RuntimeError(f"covariate stack has no {prop} surface bands")
        out[prop] = num / den
    # A pixel where theta_r >= theta_s has no pore space to fill and would make
    # relative saturation meaningless; POLARIS does not produce these but a
    # reprojection edge can.
    bad = ~(out["theta_s"] > out["theta_r"])
    out["theta_s"][bad] = np.nan
    out["theta_r"][bad] = np.nan
    return out["theta_s"], out["theta_r"]


def sample_raster(arr, transform, crs, lats, lons):
    """Sample a 2-D array at lat/lon points; NaN outside the grid."""
    xs, ys = warp_transform("EPSG:4326", crs, list(lons), list(lats))
    h, w = arr.shape
    out = []
    for x, y in zip(xs, ys):
        r, c = rowcol(transform, x, y)
        out.append(float(arr[r, c]) if 0 <= r < h and 0 <= c < w else np.nan)
    return np.array(out, dtype="float64")


def station_xy(crs, lats, lons):
    xs, ys = warp_transform("EPSG:4326", crs, list(lons), list(lats))
    return np.asarray(xs, dtype="float64"), np.asarray(ys, dtype="float64")


def pixel_coords(transform, height, width):
    """Projected coordinates of every pixel centre."""
    cols = np.arange(width)
    rows = np.arange(height)
    x0, y0 = xy(transform, 0, cols)
    _, y = xy(transform, rows, 0)
    return (np.asarray(x0, dtype="float64")[None, :],
            np.asarray(y, dtype="float64")[:, None])


def nearest_station_distance(X, Y, sx, sy):
    """Metres to the nearest contributing station, for every pixel."""
    d2 = None
    for x, y in zip(sx, sy):
        this = (X - x) ** 2 + (Y - y) ** 2
        d2 = this if d2 is None else np.minimum(d2, this)
    return np.sqrt(d2)


# --------------------------------------------------------------------------
# tier 1
# --------------------------------------------------------------------------

def zone_aggregates(stations, zone_stats, valid_zones):
    """Per-zone current and climatological relative saturation.

    Returns {zone: {...}} for zones with members, and the donor mapping used
    for station-free zones (nearest analogue in standardised covariate space).
    """
    agg = {}
    for z in valid_zones:
        members = stations[stations["zone"] == z]
        if members.empty:
            continue
        ds = (members["S_current"] - members["S_clim"]).astype(float)
        agg[z] = {
            "n_stations": int(len(members)),
            "stations": members["station"].tolist(),
            "S_clim": float(members["S_clim"].mean()),
            "dS": float(ds.mean()),
            "dS_spread": float(ds.std(ddof=0)) if len(ds) > 1 else None,
            "source": "own_stations",
        }
    return agg


def analogue_donors(zone_stats, agg, valid_zones):
    """Nearest analogue zone in covariate space for each station-free zone.

    Standardises each covariate across the zone centroids so no single band
    dominates the distance, then takes the closest zone that actually has
    stations. This is the C2-compliant substitution: soil and terrain
    similarity, not proximity in metres.
    """
    centroids = {}
    for z in valid_zones:
        fc = (zone_stats.get("zones", {}).get(str(z)) or {}).get(
            "feature_centroid") or {}
        if fc:
            centroids[z] = fc
    if not centroids:
        return {}
    bands = sorted(set.intersection(*(set(c) for c in centroids.values())))
    M = np.array([[centroids[z][b] for b in bands] for z in centroids],
                 dtype="float64")
    zs = list(centroids)
    std = M.std(0)
    std[std < 1e-12] = np.nan
    keep = np.isfinite(std)
    Mz = (M[:, keep] - M[:, keep].mean(0)) / std[keep]

    donors = {}
    have = [z for z in zs if z in agg]
    for i, z in enumerate(zs):
        if z in agg or not have:
            continue
        best, best_d = None, np.inf
        for d_zone in have:
            j = zs.index(d_zone)
            dist = float(np.linalg.norm(Mz[i] - Mz[j]))
            if dist < best_d:
                best, best_d = d_zone, dist
        donors[z] = {"donor_zone": best,
                     "covariate_distance": round(best_d, 4),
                     "n_bands": int(keep.sum())}
    return donors


def tier1_estimate(zones, theta_s, theta_r, agg, donors, site_dS,
                   site_S_clim, site_spread):
    """Paint relative saturation per zone, convert to VWC per pixel."""
    S = np.full(zones.shape, np.nan, dtype="float64")
    spread = np.full(zones.shape, np.nan, dtype="float64")
    borrowed = np.zeros(zones.shape, dtype=bool)

    for z in np.unique(zones):
        if z == ZONE_NODATA:
            continue
        sel = zones == z
        if z in agg:
            a = agg[z]
            S[sel] = a["S_clim"] + a["dS"]
            spread[sel] = (a["dS_spread"] if a["dS_spread"] is not None
                           else site_spread)
        elif z in donors and donors[z]["donor_zone"] in agg:
            a = agg[donors[z]["donor_zone"]]
            S[sel] = a["S_clim"] + a["dS"]
            spread[sel] = max(a["dS_spread"] or site_spread, site_spread)
            borrowed[sel] = True
        else:
            # Nothing to borrow from: fall back to the site-wide state and let
            # the uncertainty layer say so loudly.
            S[sel] = site_S_clim + site_dS
            spread[sel] = site_spread
            borrowed[sel] = True

    theta = np.clip(S * theta_s, theta_r, theta_s)
    return theta, spread * theta_s, borrowed


# --------------------------------------------------------------------------
# tier 2
# --------------------------------------------------------------------------

def tier2_estimate(stations, stack, names, theta_s, valid, seed=0):
    """Random forest of station relative saturation on covariates.

    Returns (S_grid, per_tree_spread_grid, feature_names). Every covariate band
    is offered and the forest selects; that is only defensible once there are
    enough distinct locations to fit, which is why the caller gates this on
    analysis.min_stations_for_regression.
    """
    from sklearn.ensemble import RandomForestRegressor

    Xs = np.column_stack([stations[f"cov_{n}"].values for n in names])
    y = stations["S_current"].values.astype("float64")
    ok = np.all(np.isfinite(Xs), axis=1) & np.isfinite(y)
    if ok.sum() < 4:
        raise RuntimeError(f"only {int(ok.sum())} stations have a complete "
                           "covariate vector")

    mean, std = Xs[ok].mean(0), Xs[ok].std(0)
    keep = std > 1e-12
    if keep.sum() < 2:
        raise RuntimeError("fewer than 2 covariates vary across the stations")
    used = [n for n, k in zip(names, keep) if k]

    rf = RandomForestRegressor(n_estimators=300, random_state=seed,
                               min_samples_leaf=1)
    rf.fit((Xs[ok][:, keep] - mean[keep]) / std[keep], y[ok])

    grid = np.column_stack([stack[names.index(n)][valid].astype("float64")
                            for n in used])
    Zg = np.nan_to_num((grid - mean[keep]) / std[keep], nan=0.0)

    S = np.full(theta_s.shape, np.nan, dtype="float64")
    S[valid] = rf.predict(Zg)
    per_tree = np.stack([t.predict(Zg) for t in rf.estimators_])
    spread = np.full(theta_s.shape, np.nan, dtype="float64")
    spread[valid] = per_tree.std(0)
    return S, spread, used


def idw_residuals(X, Y, sx, sy, resid, power=IDW_POWER):
    """IDW of station residuals onto the grid, in projected metres."""
    num = np.zeros((Y.shape[0], X.shape[1]), dtype="float64")
    den = np.zeros_like(num)
    for x, y, r in zip(sx, sy, resid):
        d2 = (X - x) ** 2 + (Y - y) ** 2
        d2 = np.maximum(d2, 1.0)
        w = 1.0 / d2 ** (power / 2.0)
        num += w * r
        den += w
    return num / den


# --------------------------------------------------------------------------
# skill
# --------------------------------------------------------------------------

def loso_skill(stations, zone_stats, valid_zones, theta_s_at, theta_r_at,
               site_spread):
    """Leave-one-station-out validation of the tier-1 estimator.

    Re-runs the zone aggregation without each station in turn and reads the
    resulting estimate at that station's own pixel. Compared against two
    baselines that cost nothing, so the reader can see whether the zones buy
    anything: the mean of the remaining stations, and the station's own
    climatology with no anomaly applied.
    """
    if len(stations) < 3:
        return {"skipped": f"only {len(stations)} reporting stations "
                           "(need >= 3 for leave-one-out)"}
    rows = []
    for i, st in stations.reset_index(drop=True).iterrows():
        rest = stations.drop(stations.index[i])
        if rest.empty:
            continue
        agg = zone_aggregates(rest, zone_stats, valid_zones)
        donors = analogue_donors(zone_stats, agg, valid_zones)
        z = st["zone"]
        site_dS = float((rest["S_current"] - rest["S_clim"]).mean())
        site_S_clim = float(rest["S_clim"].mean())

        if z in agg:
            src, a = "own_zone", agg[z]
        elif z in donors and donors[z]["donor_zone"] in agg:
            src, a = "analogue_zone", agg[donors[z]["donor_zone"]]
        else:
            src, a = "site_mean", {"S_clim": site_S_clim, "dS": site_dS}
        pred = np.clip((a["S_clim"] + a["dS"]) * theta_s_at[i],
                       theta_r_at[i], theta_s_at[i])

        base_site = np.clip((site_S_clim + site_dS) * theta_s_at[i],
                            theta_r_at[i], theta_s_at[i])
        base_clim = st["clim"]
        rows.append({
            "station": st["station"], "zone": (int(z) if pd.notna(z) else None),
            "observed": round(float(st["current"]), 4),
            "predicted": round(float(pred), 4),
            "error": round(float(pred - st["current"]), 4),
            "source": src,
            "baseline_site_mean_error": round(float(base_site - st["current"]), 4),
            "baseline_climatology_error": (
                round(float(base_clim - st["current"]), 4)
                if base_clim is not None else None),
        })

    def _agg(key):
        e = np.array([r[key] for r in rows if r[key] is not None],
                     dtype="float64")
        if not len(e):
            return None
        return {"n": int(len(e)), "rmse": round(float(np.sqrt((e ** 2).mean())), 4),
                "bias": round(float(e.mean()), 4),
                "mae": round(float(np.abs(e).mean()), 4)}

    per_zone = {}
    for z in sorted({r["zone"] for r in rows if r["zone"] is not None}):
        e = np.array([r["error"] for r in rows if r["zone"] == z])
        per_zone[str(z)] = {"n": int(len(e)),
                            "rmse": round(float(np.sqrt((e ** 2).mean())), 4),
                            "bias": round(float(e.mean()), 4)}

    zone_rmse = (_agg("error") or {}).get("rmse")
    site_rmse = (_agg("baseline_site_mean_error") or {}).get("rmse")
    verdict = "inconclusive"
    if zone_rmse is not None and site_rmse is not None:
        if zone_rmse < 0.95 * site_rmse:
            verdict = "zone-anchored beats the site-mean baseline"
        elif zone_rmse > 1.05 * site_rmse:
            verdict = ("zone-anchored is WORSE than the site-mean baseline; "
                       "the zones are not adding information at these stations")
        else:
            verdict = "zone-anchored is indistinguishable from the site mean"

    return {
        "method": "leave-one-station-out, tier 1",
        "per_station": rows,
        "overall": _agg("error"),
        "baseline_site_mean": _agg("baseline_site_mean_error"),
        "baseline_climatology": _agg("baseline_climatology_error"),
        "per_zone": per_zone,
        "verdict": verdict,
        "caveat": (f"{len(rows)} stations; every fold re-estimates from at most "
                   f"{len(rows) - 1} of them, so these numbers describe the "
                   "network we have, not the map's accuracy over unsampled "
                   "terrain."),
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def write_raster(path, arr, transform, crs, tags, description):
    """Write a single-band float32 COG (GTiff + overviews if COG is absent)."""
    profile = dict(driver="GTiff", dtype="float32", count=1,
                   height=arr.shape[0], width=arr.shape[1], crs=crs,
                   transform=transform, nodata=np.nan, compress="deflate",
                   predictor=3, tiled=True, blockxsize=512, blockysize=512,
                   bigtiff="if_safer")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr.astype("float32"), 1)
        dst.set_band_description(1, description)
        dst.update_tags(**{k: str(v) for k, v in tags.items()})
        # Overviews are what make this cheap to draw in a GIS or web viewer.
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")


def main():
    ap = argparse.ArgumentParser(
        description="Upscale station soil moisture to the covariate grid (M4)")
    ap.add_argument("--points", required=True, help="soil_moisture_points.json")
    ap.add_argument("--zones", required=True, help="zones.tif")
    ap.add_argument("--zone-stats", required=True, help="zone_stats.json")
    ap.add_argument("--covariates", required=True, help="covariates.tif")
    ap.add_argument("--manifest", help="covariates_manifest.json (provenance)")
    ap.add_argument("--fingerprints", nargs="*", default=[],
                    help="response_*.json for the monthly climatology")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--as-of", help="Reference date (YYYY-MM-DD); defaults to "
                                    "the as_of recorded in --points")
    ap.add_argument("--output-map", required=True, help="soil_moisture_now.tif")
    ap.add_argument("--output-uncertainty", required=True,
                    help="soil_moisture_uncertainty.tif")
    ap.add_argument("--output-json", required=True, help="soil_moisture_now.json")
    ap.add_argument("--output-skill", required=True, help="estimation_skill.json")
    args = ap.parse_args()

    outputs = (args.output_map, args.output_uncertainty, args.output_json,
               args.output_skill)
    try:
        run(args)
    except Exception as exc:
        for path in outputs:
            open(path, "a").close()
        logger.error("estimate_soil_moisture failed: %s", exc)
        sys.exit(1)


def run(args):
    with open(args.config) as fh:
        config = json.load(fh)
    analysis = config.get("analysis", {})
    surface_max = int(analysis.get("surface_depth_max_cm", 20))
    min_stations = int(analysis.get("min_reporting_stations", 3))
    min_reg = int(analysis.get("min_stations_for_regression",
                               DEFAULT_MIN_STATIONS_REGRESSION))
    decorr_m = float(analysis.get("decorrelation_length_m",
                                  DEFAULT_DECORRELATION_M))
    free_penalty = float(analysis.get("station_free_zone_penalty",
                                      DEFAULT_STATION_FREE_PENALTY))
    thresholds = analysis.get("sm_thresholds", {}).get(
        "breakpoints", [0.06, 0.12, 0.20, 0.30])
    labels = analysis.get("sm_thresholds", {}).get(
        "labels", ["very_dry", "dry", "moderate", "moist", "saturated"])

    with open(args.points) as fh:
        points = json.load(fh)
    as_of = args.as_of or points.get("as_of")
    if not as_of:
        raise RuntimeError("no --as-of and none recorded in the points layer")
    as_of_ts = pd.Timestamp(as_of)
    month = int(as_of_ts.month)

    fingerprints = []
    for path in args.fingerprints:
        try:
            with open(path) as fh:
                fingerprints.append(json.load(fh))
        except Exception as exc:
            logger.warning("could not read %s: %s", path, exc)

    max_age = int(analysis.get("max_current_age_days", 5))
    stations, nodes = load_surface_stations(points, fingerprints, as_of,
                                           surface_max, max_age)
    if stations.empty:
        raise RuntimeError(
            f"no fresh surface station as of {as_of}: every surface node is "
            f"either stale (> {max_age} d) or missing a current value. If "
            f"--as-of is far from the points layer's as_of "
            f"({points.get('as_of')}), that is the cause.")
    logger.info("%d fresh surface station(s) from %d node(s), as of %s "
                "(month %d climatology)", len(stations), len(nodes), as_of,
                month)

    with rasterio.open(args.zones) as zsrc:
        zones = zsrc.read(1)
        transform, crs = zsrc.transform, zsrc.crs
    with rasterio.open(args.covariates) as csrc:
        if (csrc.width, csrc.height) != (zones.shape[1], zones.shape[0]):
            raise RuntimeError(
                f"grid mismatch: zones {zones.shape[1]}x{zones.shape[0]} vs "
                f"covariates {csrc.width}x{csrc.height}")
        stack = csrc.read().astype("float32")
        names = [csrc.descriptions[i] or f"band{i + 1}"
                 for i in range(csrc.count)]
    with open(args.zone_stats) as fh:
        zone_stats = json.load(fh)

    theta_s, theta_r = surface_theta(stack, names)
    valid = np.isfinite(theta_s) & np.isfinite(theta_r) & (zones != ZONE_NODATA)
    logger.info("Grid %d x %d, %.1f%% estimable pixels",
                zones.shape[1], zones.shape[0], 100 * valid.mean())

    # --- station-side quantities -----------------------------------------
    ts_at = sample_raster(theta_s, transform, crs, stations["lat"],
                          stations["lon"])
    tr_at = sample_raster(theta_r, transform, crs, stations["lat"],
                          stations["lon"])
    zone_at = sample_raster(zones.astype("float64"), transform, crs,
                            stations["lat"], stations["lon"])
    stations["theta_s"] = ts_at
    stations["theta_r"] = tr_at
    stations["zone"] = [int(z) if np.isfinite(z) and z != ZONE_NODATA else None
                        for z in zone_at]
    for n in names:
        stations[f"cov_{n}"] = sample_raster(
            stack[names.index(n)].astype("float64"), transform, crs,
            stations["lat"], stations["lon"])

    if stations["clim"].isna().any():
        missing = stations.loc[stations["clim"].isna(), "station"].tolist()
        logger.warning("no climatology for %s; dropping from the anomaly "
                       "upscaling", missing)
        stations = stations.dropna(subset=["clim"])
    stations = stations[np.isfinite(stations["theta_s"])].reset_index(drop=True)
    if stations.empty:
        raise RuntimeError("no station has both a climatology and a soil "
                           "profile on the analysis grid")

    # Relative saturation: the quantity that transfers across soil boundaries.
    stations["S_current"] = stations["current"] / stations["theta_s"]
    stations["S_clim"] = stations["clim"] / stations["theta_s"]

    n_report = int(len(stations))
    if n_report < min_stations:
        logger.warning("only %d reporting station(s), below "
                       "analysis.min_reporting_stations=%d; the map is "
                       "published with correspondingly large uncertainty",
                       n_report, min_stations)

    valid_zones = sorted(int(z) for z in np.unique(zones) if z != ZONE_NODATA)
    agg = zone_aggregates(stations, zone_stats, valid_zones)
    donors = analogue_donors(zone_stats, agg, valid_zones)
    site_dS = float((stations["S_current"] - stations["S_clim"]).mean())
    site_S_clim = float(stations["S_clim"].mean())
    site_spread = max(float((stations["S_current"]
                             - stations["S_clim"]).std(ddof=0))
                      if n_report > 1 else 0.0,
                      DEFAULT_MIN_SPREAD)
    logger.info("Zones with stations: %s; station-free zones borrowing an "
                "analogue: %s", sorted(agg), {k: v["donor_zone"]
                                              for k, v in donors.items()})

    theta, model_sd, borrowed = tier1_estimate(
        zones, theta_s, theta_r, agg, donors, site_dS, site_S_clim,
        site_spread)
    tier_used, tier2_note = 1, None

    # --- tier 2 -----------------------------------------------------------
    n_loc = int(stations[["lat", "lon"]].round(4).drop_duplicates().shape[0])
    if n_loc >= min_reg:
        try:
            S2, sd2, tier2_cols = tier2_estimate(stations, stack, names,
                                                 theta_s, valid)
            sx, sy = station_xy(crs, stations["lat"], stations["lon"])
            X, Y = pixel_coords(transform, zones.shape[0], zones.shape[1])
            pred_at = sample_raster(S2, transform, crs, stations["lat"],
                                    stations["lon"])
            resid = stations["S_current"].values - pred_at
            S2 = S2 + idw_residuals(X, Y, sx, sy, np.nan_to_num(resid))
            theta = np.clip(S2 * theta_s, theta_r, theta_s)
            model_sd = sd2 * theta_s
            tier_used = 2
            tier2_note = (f"tier 2 engaged: {n_loc} distinct locations >= "
                          f"{min_reg}; {len(tier2_cols)} covariates offered "
                          "to the forest, residuals interpolated by IDW")
            logger.info(tier2_note)
        except Exception as exc:
            tier2_note = f"tier 2 attempted and failed: {exc}"
            logger.warning(tier2_note)
    else:
        tier2_note = (f"tier 2 not attempted: {n_loc} distinct reporting "
                      f"location(s) < analysis.min_stations_for_regression="
                      f"{min_reg}. A {len(names)}-covariate forest fitted to "
                      f"{n_loc} points would not be evidence.")
        logger.info(tier2_note)

    # --- uncertainty ------------------------------------------------------
    sx, sy = station_xy(crs, stations["lat"], stations["lon"])
    X, Y = pixel_coords(transform, zones.shape[0], zones.shape[1])
    dist = nearest_station_distance(X, Y, sx, sy)
    # Between-station spread of the actual current values is the natural scale
    # for "how wrong can distance make you": the distance term saturates there
    # rather than at an invented constant.
    between = max(float(stations["current"].std(ddof=0)) if n_report > 1
                  else 0.0, DEFAULT_MIN_SPREAD)
    u_dist = between * (1.0 - np.exp(-dist / decorr_m))

    station_free = set(zone_stats.get("station_free_zones") or [])
    u_free = np.where(np.isin(zones, list(station_free)) | borrowed,
                      free_penalty, 0.0)

    unc = np.sqrt(np.nan_to_num(model_sd, nan=site_spread) ** 2
                  + u_dist ** 2 + u_free ** 2)
    unc = np.maximum(unc, DEFAULT_MIN_SPREAD)
    theta[~valid] = np.nan
    unc[~valid] = np.nan

    # --- skill ------------------------------------------------------------
    skill = loso_skill(stations, zone_stats, valid_zones,
                       stations["theta_s"].values, stations["theta_r"].values,
                       site_spread)

    # --- write ------------------------------------------------------------
    manifest_built = None
    if args.manifest:
        try:
            with open(args.manifest) as fh:
                manifest_built = json.load(fh).get("built_utc")
        except Exception as exc:
            logger.warning("could not read manifest: %s", exc)

    provenance = {
        "as_of": as_of,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "covariate_manifest_built_utc": manifest_built,
        "zones_built_utc": zone_stats.get("built_utc"),
        "zone_k": (zone_stats.get("clustering") or {}).get("k"),
        "tier_used": tier_used,
        "surface_layer_cm": [0, int(sum(t for _, t in SURFACE_INTERVALS))],
    }
    tags = dict(provenance, units="m3/m3", layer="soil_moisture_now",
                n_reporting_stations=n_report)
    write_raster(args.output_map, theta, transform, crs, tags,
                 "soil_moisture_now")
    write_raster(args.output_uncertainty, unc, transform, crs,
                 dict(tags, layer="soil_moisture_uncertainty"),
                 "soil_moisture_uncertainty_1sigma")

    finite = theta[np.isfinite(theta)]
    px_ha = (abs(transform.a) ** 2) / 10_000.0
    class_area = {}
    for i, lab in enumerate(labels):
        lo = -np.inf if i == 0 else thresholds[i - 1]
        hi = thresholds[i] if i < len(thresholds) else np.inf
        class_area[lab] = round(float(((finite > lo) & (finite <= hi)).sum()
                                      * px_ha), 1)

    zone_table = []
    for z in valid_zones:
        zs = (zone_stats.get("zones", {}) or {}).get(str(z), {})
        sel = (zones == z) & valid
        a = agg.get(z)
        zone_table.append({
            "zone": z,
            "area_ha": zs.get("area_ha"),
            "n_stations": (a or {}).get("n_stations", 0),
            "stations": (a or {}).get("stations", []),
            "station_free": bool(zs.get("station_free", z not in agg)),
            "analogue_donor_zone": (donors.get(z) or {}).get("donor_zone"),
            "analogue_covariate_distance": (donors.get(z) or {}).get(
                "covariate_distance"),
            "S_clim": round((a or {}).get("S_clim", site_S_clim), 4),
            "dS": round((a or {}).get("dS", site_dS), 4),
            "mean_estimate": (round(float(np.nanmean(theta[sel])), 4)
                              if sel.any() else None),
            "mean_uncertainty": (round(float(np.nanmean(unc[sel])), 4)
                                 if sel.any() else None),
        })

    result = {
        "layer": "soil_moisture_now",
        "units": "m3/m3 (volumetric fraction)",
        "site": config.get("site"),
        "provenance": provenance,
        "method": {
            "tier": tier_used,
            "tier1": "zone-anchored relative-saturation anomaly upscaling; "
                     "theta = (S_clim_zone + dS_zone) * theta_s(pixel), "
                     "clipped to [theta_r, theta_s]",
            "tier2_note": tier2_note,
            "uncertainty": "sqrt(model_spread^2 + distance^2 + "
                           "station_free_penalty^2), 1 sigma, m3/m3",
            "decorrelation_length_m": decorr_m,
            "station_free_zone_penalty": free_penalty,
        },
        "n_reporting_stations": n_report,
        "n_distinct_locations": n_loc,
        "min_reporting_stations": min_stations,
        "below_min_reporting_stations": n_report < min_stations,
        "grid": {"crs": str(crs), "resolution_m": abs(transform.a),
                 "width": int(zones.shape[1]), "height": int(zones.shape[0]),
                 "estimable_fraction": round(float(valid.mean()), 4)},
        "summary": {
            "mean": round(float(np.nanmean(finite)), 4) if finite.size else None,
            "min": round(float(np.nanmin(finite)), 4) if finite.size else None,
            "max": round(float(np.nanmax(finite)), 4) if finite.size else None,
            "mean_uncertainty": round(float(np.nanmean(unc[valid])), 4),
            "area_ha_by_class": class_area,
            "area_ha_station_free": round(
                float((np.isin(zones, list(station_free)) & valid).sum()
                      * px_ha), 1),
        },
        "classes": labels,
        "thresholds": thresholds,
        "zones": zone_table,
        "stations": [
            {"station": r["station"], "lat": r["lat"], "lon": r["lon"],
             "zone": r["zone"], "n_surface_nodes": int(r["n_nodes"]),
             "current": round(float(r["current"]), 4),
             "climatology_month": round(float(r["clim"]), 4),
             "climatology_source": r["clim_source"],
             "anomaly": round(float(r["current"] - r["clim"]), 4),
             "theta_s": round(float(r["theta_s"]), 4),
             "S_current": round(float(r["S_current"]), 4),
             "S_clim": round(float(r["S_clim"]), 4),
             "age_days": r["age_days"]}
            for _, r in stations.iterrows()],
    }
    with open(args.output_json, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    with open(args.output_skill, "w") as fh:
        json.dump({"layer": "estimation_skill", "as_of": as_of,
                   "tier_used": tier_used, **skill}, fh, indent=2, default=str)

    logger.info("Wrote %s (mean %.4f m3/m3, mean 1-sigma %.4f); tier %d; "
                "LOSO RMSE %s vs site-mean %s -> %s",
                args.output_map, float(np.nanmean(finite)),
                float(np.nanmean(unc[valid])), tier_used,
                (skill.get("overall") or {}).get("rmse", "n/a"),
                (skill.get("baseline_site_mean") or {}).get("rmse", "n/a"),
                skill.get("verdict", skill.get("skipped")))


if __name__ == "__main__":
    main()
