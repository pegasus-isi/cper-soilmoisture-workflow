"""Shared helpers for the drought workflow layer scripts.

Staged into each job's working directory by Pegasus and imported by the layer
scripts. Keeps the long-format observation schema and the small bits of
meteorology (VPD, evaporative fraction, IDW gridding) in one place.

Observation schema (produced by both fetchers):
    timestamp, source, node, lat, lon, variable, value, unit
"""

import os
import sys

import numpy as np
import pandas as pd

# Normalised variable vocabulary shared across the workflow.
VARIABLES = [
    "soil_moisture",  # m3/m3 (fraction)
    "soil_temp",      # degC
    "air_temp",       # degC
    "rel_humidity",   # percent
    "pressure",       # Pa
    "wind_speed",     # m/s
    "precip",         # mm
    "le",             # W/m2 latent heat flux
    "h",              # W/m2 sensible heat flux
    "netrad",         # W/m2 net radiation
    "snow_depth",     # m
    "swe",            # mm snow water equivalent
    "vpd",            # kPa
    "et",             # mm evapotranspiration
    "sw_in",          # W/m2 incoming shortwave
    "sw_out",         # W/m2 outgoing shortwave
    "lw_in",          # W/m2 incoming longwave
    "lw_out",         # W/m2 outgoing longwave
    "surface_temp",   # degC (thermal)
]


def load_observations(paths):
    """Load and concatenate one or more long-format observation CSVs."""
    frames = []
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame(
            columns=[
                "timestamp", "source", "node", "lat", "lon",
                "variable", "value", "unit",
            ]
        )
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce", utc=True)
    out = out.dropna(subset=["timestamp"])
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out.dropna(subset=["value"])


def hourly_series(df, variable, node=None):
    """Return an hourly-mean pandas Series (indexed by timestamp) for a variable."""
    sub = df[df["variable"] == variable]
    if node is not None:
        sub = sub[sub["node"] == node]
    if sub.empty:
        return pd.Series(dtype=float)
    return (
        sub.set_index("timestamp")["value"]
        .resample("1h")
        .mean()
        .dropna()
    )


def daily_series(df, variable, node=None, how="mean"):
    """Return a daily-aggregated Series for a variable."""
    s = hourly_series(df, variable, node)
    if s.empty:
        return s
    return getattr(s.resample("1D"), how)().dropna()


def saturation_vapor_pressure(temp_c):
    """Saturation vapour pressure (kPa) via Tetens."""
    return 0.6108 * np.exp(17.27 * temp_c / (temp_c + 237.3))


def vpd_kpa(temp_c, rh_percent):
    """Vapour pressure deficit (kPa) from air temperature and relative humidity."""
    es = saturation_vapor_pressure(temp_c)
    return np.clip(es * (1.0 - np.clip(rh_percent, 0, 100) / 100.0), 0, None)


def evaporative_fraction(le, h):
    """EF = LE / (LE + H); robust to tiny/negative denominators."""
    denom = le + h
    ef = np.where(np.abs(denom) > 1.0, le / denom, np.nan)
    return np.clip(ef, 0, 1)


def normalize01(series):
    """Min-max normalise a series to [0,1]; flat series -> 0.5."""
    s = pd.Series(series, dtype=float)
    lo, hi = s.min(), s.max()
    if not np.isfinite(lo) or not np.isfinite(hi) or hi - lo < 1e-9:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def node_locations(df):
    """Return {node: (lat, lon)} for nodes with known coordinates."""
    locs = {}
    known = df.dropna(subset=["lat", "lon"])
    for node, grp in known.groupby("node"):
        locs[node] = (float(grp["lat"].iloc[0]), float(grp["lon"].iloc[0]))
    return locs


def idw_grid(points, bbox, n=40, power=2.0):
    """Inverse-distance-weighted grid over bbox from [(lat,lon,value), ...].

    Returns (lats, lons, grid) or None when fewer than 3 located points exist.
    """
    pts = [(la, lo, v) for la, lo, v in points if None not in (la, lo, v)]
    if len(pts) < 3:
        return None
    lats = np.linspace(bbox["min_lat"], bbox["max_lat"], n)
    lons = np.linspace(bbox["min_lon"], bbox["max_lon"], n)
    grid = np.zeros((n, n))
    plat = np.array([p[0] for p in pts])
    plon = np.array([p[1] for p in pts])
    pval = np.array([p[2] for p in pts])
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            d2 = (plat - la) ** 2 + (plon - lo) ** 2
            if (d2 < 1e-12).any():
                grid[i, j] = pval[d2 < 1e-12][0]
            else:
                w = 1.0 / d2 ** (power / 2.0)
                grid[i, j] = np.sum(w * pval) / np.sum(w)
    return lats.tolist(), lons.tolist(), grid.tolist()


def classify(value, thresholds, labels):
    """Map a scalar to a label given ascending threshold breakpoints."""
    for t, lab in zip(thresholds, labels):
        if value <= t:
            return lab
    return labels[-1]
