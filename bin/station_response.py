#!/usr/bin/env python3
"""Per-station soil-moisture response fingerprints (M3).

One job per station (the fan-out unit); every depth-node belonging to that
station gets its own fingerprint, so a NEON soil plot with sensors at six
depths yields six fingerprints from one job. This is the direct answer to the
researcher's first ask: characterise how each monitoring location responds,
in numbers that can then be related to soil, terrain and climate.

Per node (SPEC.md section 8, M3):

  * coverage/QC       record span, completeness, gaps, flatlines, out-of-range
  * climatology       monthly means, percentiles, seasonal amplitude and phase
  * reference points  dry-end proxy, field-capacity proxy, plant-available range
  * event response    dVWC per mm of precip, lag to peak, response fraction
  * dry-down          recession time constant tau from post-event dry-downs
  * memory            autocorrelation e-folding time

Deliberately numpy/pandas only — no scipy. The exponential recession fit is a
log-linear regression with a small search over the asymptote, which is stable
on short, noisy recessions and keeps the container lean (SPEC.md "Container").

Failure policy: this is a per-station analysis job, not a fetcher. It always
writes its declared output; a station with too little usable data produces a
fingerprint marked insufficient rather than killing the DAG, so one dead
logger cannot sink a characterisation run.
"""

import argparse
import json
import logging
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import drought_common as dc  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("station_response")

MIN_DAYS = 90            # below this a fingerprint is not meaningful
VWC_MIN, VWC_MAX = 0.0, 0.75   # physical plausibility for these soils
FLATLINE_TOL = 1e-4      # identical-to-this counts as a stuck sensor
EVENT_PRECIP_MM = 5.0    # daily precip that counts as a wetting event
EVENT_WINDOW_D = 5       # days after an event to look for the peak
RECESSION_MIN_D = 5      # shortest usable dry-down
RECESSION_MAX_D = 30     # longest dry-down segment considered


def node_depth_cm(node):
    """Depth in cm from a `station@Ncm` node id; None for depth-less nodes."""
    if "@" in node and node.endswith("cm"):
        try:
            return float(node.rsplit("@", 1)[1][:-2])
        except ValueError:
            return None
    return None


def daily_series(df, node, variable="soil_moisture"):
    """Daily-mean series for one node/variable, gaps left as missing dates."""
    sub = df[(df["node"] == node) & (df["variable"] == variable)]
    if sub.empty:
        return pd.Series(dtype=float)
    s = sub.set_index("timestamp")["value"].resample("1D").mean().dropna()
    return s


def quality(series):
    """Coverage and sensor-health summary for one node."""
    if series.empty:
        return {"n_days": 0, "completeness": 0.0}
    span = (series.index.max() - series.index.min()).days + 1
    diffs = series.diff().abs()
    flat = (diffs < FLATLINE_TOL)
    # longest run of consecutive flat days
    longest, run = 0, 0
    for f in flat.fillna(False).values:
        run = run + 1 if f else 0
        longest = max(longest, run)
    out_of_range = ((series < VWC_MIN) | (series > VWC_MAX)).sum()
    return {
        "start": str(series.index.min().date()),
        "end": str(series.index.max().date()),
        "n_days": int(len(series)),
        "span_days": int(span),
        "completeness": round(float(len(series) / span), 4) if span else 0.0,
        "longest_gap_days": int(
            series.index.to_series().diff().dt.days.fillna(1).max() - 1),
        "longest_flatline_days": int(longest),
        "out_of_range_days": int(out_of_range),
    }


def climatology(series):
    """Monthly climatology, seasonal amplitude and phase."""
    monthly = series.groupby(series.index.month).mean()
    if monthly.empty:
        return {}
    return {
        "monthly_mean": {int(m): round(float(v), 4) for m, v in monthly.items()},
        "seasonal_amplitude": round(float(monthly.max() - monthly.min()), 4),
        "month_wettest": int(monthly.idxmax()),
        "month_driest": int(monthly.idxmin()),
    }


def reference_points(series):
    """Dry-end / field-capacity proxies and the plant-available range.

    Percentile-based rather than event-based: with daily data and multi-year
    records the 5th/95th percentiles are a stabler pair of reference points
    than picking individual post-drainage plateaus, and they are directly
    comparable between stations.
    """
    q = series.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    dry, fc = float(q.loc[0.05]), float(q.loc[0.95])
    return {
        "p05_dry_end": round(dry, 4),
        "p25": round(float(q.loc[0.25]), 4),
        "median": round(float(q.loc[0.5]), 4),
        "p75": round(float(q.loc[0.75]), 4),
        "p95_field_capacity_proxy": round(fc, 4),
        "plant_available_range": round(fc - dry, 4),
        "mean": round(float(series.mean()), 4),
        "std": round(float(series.std()), 4),
        "min": round(float(series.min()), 4),
        "max": round(float(series.max()), 4),
    }


def memory_efolding(series):
    """Autocorrelation e-folding time (days) on the anomaly series."""
    s = series.asfreq("1D")
    anom = s - s.groupby(s.index.dayofyear).transform("mean")
    anom = anom.dropna()
    if len(anom) < 30:
        return {}
    v = anom.values
    v = v - v.mean()
    denom = float(np.dot(v, v))
    if denom <= 0:
        return {}
    max_lag = min(365, len(v) - 1)
    acf, efold = [], None
    for lag in range(1, max_lag):
        r = float(np.dot(v[:-lag], v[lag:]) / denom)
        acf.append(r)
        if efold is None and r < 1 / np.e:
            efold = lag
            break
    return {
        "lag1_autocorr": round(acf[0], 4) if acf else None,
        "memory_efolding_days": efold,
        # Distinguish "no memory estimate" from "memory longer than we looked":
        # a null efolding with censored=True means the anomaly ACF never fell
        # below 1/e within max_lag, i.e. very persistent, not missing.
        "memory_efolding_censored": efold is None,
        "memory_max_lag_days": int(max_lag),
    }


def _loglinear_tau(seg):
    """Fit VWC(t) = a*exp(-t/tau) + c; return (tau, r2) or (None, None).

    No scipy: search the asymptote c over a grid below the segment minimum and
    keep the c whose log-linear fit explains the most variance. Robust on the
    short (5-30 day) recessions these records actually contain.
    """
    y = seg.values.astype(float)
    t = np.arange(len(y), dtype=float)
    if len(y) < RECESSION_MIN_D or not np.all(np.isfinite(y)):
        return None, None
    best = (None, None)
    ymin, ymax = y.min(), y.max()
    if ymax - ymin < 0.005:            # no real drying signal
        return None, None
    for frac in (0.0, 0.25, 0.5, 0.75, 0.9):
        c = ymin - (ymax - ymin) * 0.05 - frac * (ymin * 0.5)
        z = y - c
        if np.any(z <= 0):
            continue
        lz = np.log(z)
        slope, intercept = np.polyfit(t, lz, 1)
        if slope >= 0:                  # not drying
            continue
        pred = intercept + slope * t
        ss_res = float(np.sum((lz - pred) ** 2))
        ss_tot = float(np.sum((lz - lz.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        if best[1] is None or r2 > best[1]:
            best = (-1.0 / slope, r2)
    return best


def dry_downs(series, precip):
    """Recession time constants from post-event dry-downs.

    A recession is a run of non-increasing days with no meaningful precip.
    Reports the median tau across all usable recessions — the classic
    single-number summary of how fast a location loses water.
    """
    s = series.asfreq("1D")
    taus, r2s, lengths = [], [], []
    wet_days = set()
    if precip is not None and not precip.empty:
        p = precip.reindex(s.index).fillna(0.0)
        wet_days = set(p.index[p > 1.0])

    i, n = 0, len(s)
    idx = s.index
    while i < n - RECESSION_MIN_D:
        if not np.isfinite(s.iloc[i]):
            i += 1
            continue
        j = i + 1
        while (j < n and j - i <= RECESSION_MAX_D
               and np.isfinite(s.iloc[j])
               and s.iloc[j] <= s.iloc[j - 1] + 0.002
               and idx[j] not in wet_days):
            j += 1
        if j - i >= RECESSION_MIN_D:
            tau, r2 = _loglinear_tau(s.iloc[i:j])
            if tau is not None and 0.5 < tau < 365 and r2 is not None and r2 > 0.7:
                taus.append(tau)
                r2s.append(r2)
                lengths.append(j - i)
            i = j
        else:
            i += 1

    if not taus:
        return {"n_recessions": 0}
    return {
        "n_recessions": len(taus),
        "drydown_tau_days": round(float(np.median(taus)), 3),
        "drydown_tau_iqr": round(float(np.percentile(taus, 75)
                                       - np.percentile(taus, 25)), 3),
        "drydown_fit_r2_median": round(float(np.median(r2s)), 3),
        "drydown_median_length_days": int(np.median(lengths)),
    }


def event_response(series, precip):
    """Wetting response to precip events: dVWC, dVWC per mm, lag to peak."""
    if precip is None or precip.empty:
        return {"n_events": 0, "note": "no precip series available"}
    s = series.asfreq("1D")
    p = precip.reindex(s.index).fillna(0.0)
    events = p.index[p >= EVENT_PRECIP_MM]

    deltas, per_mm, lags, responded = [], [], [], 0
    for ts in events:
        try:
            i = s.index.get_loc(ts)
        except KeyError:
            continue
        if i == 0 or i + EVENT_WINDOW_D >= len(s):
            continue
        pre = s.iloc[i - 1]
        window = s.iloc[i:i + EVENT_WINDOW_D + 1]
        if not np.isfinite(pre) or window.isna().all():
            continue
        peak = float(window.max())
        d = peak - float(pre)
        amount = float(p.loc[ts])
        deltas.append(d)
        per_mm.append(d / amount if amount > 0 else np.nan)
        lags.append(int(window.values.argmax()))
        if d > 0.01:
            responded += 1

    if not deltas:
        return {"n_events": 0}
    return {
        "n_events": len(deltas),
        "event_delta_vwc_median": round(float(np.nanmedian(deltas)), 4),
        "event_delta_per_mm_median": round(float(np.nanmedian(per_mm)), 6),
        "event_lag_to_peak_days_median": float(np.median(lags)),
        "event_response_fraction": round(responded / len(deltas), 3),
    }


def fingerprint(df, node, precip):
    """Full response fingerprint for one depth-node."""
    s = daily_series(df, node)
    fp = {
        "node": node,
        "depth_cm": node_depth_cm(node),
        "quality": quality(s),
    }
    meta = df[df["node"] == node].iloc[0] if not df[df["node"] == node].empty else None
    if meta is not None:
        fp["lat"] = float(meta["lat"]) if pd.notna(meta["lat"]) else None
        fp["lon"] = float(meta["lon"]) if pd.notna(meta["lon"]) else None
        fp["source"] = meta["source"]

    if len(s) < MIN_DAYS:
        fp["insufficient_data"] = True
        fp["reason"] = f"{len(s)} usable days < {MIN_DAYS}"
        return fp

    fp["insufficient_data"] = False
    fp.update(reference_points(s))
    fp.update(climatology(s))
    fp.update(memory_efolding(s))
    fp.update(dry_downs(s, precip))
    fp.update(event_response(s, precip))
    return fp


def site_precip(df):
    """Best available daily precip series for the site (any reporting node).

    NEON soil plots carry no precipitation, so event response for NEON nodes
    is driven by the site's other networks — which is legitimate here: CPER's
    reporting stations are within a few km of each other on the same steppe.
    """
    p = df[df["variable"] == "precip"]
    if p.empty:
        return None
    return (p.set_index("timestamp")["value"]
            .groupby(level=0).max()
            .resample("1D").max().dropna())


def main():
    ap = argparse.ArgumentParser(
        description="Per-station soil-moisture response fingerprints")
    ap.add_argument("--observations", required=True,
                    help="Harmonized observations CSV")
    ap.add_argument("--station", required=True,
                    help="Station id prefix, e.g. NEON:CPER or SCAN:2197")
    ap.add_argument("--config", help="site_config.json (optional metadata)")
    ap.add_argument("--output", required=True, help="Output fingerprint JSON")
    args = ap.parse_args()

    result = {"station": args.station, "nodes": []}
    try:
        df = dc.load_observations([args.observations])
        precip = site_precip(df)
        sm = df[df["variable"] == "soil_moisture"]
        nodes = sorted(n for n in sm["node"].unique()
                       if str(n).startswith(args.station))
        logger.info("Station %s: %d soil-moisture nodes", args.station, len(nodes))
        for node in nodes:
            result["nodes"].append(fingerprint(df, node, precip))
        result["n_nodes"] = len(result["nodes"])
        result["n_characterized"] = sum(
            1 for f in result["nodes"] if not f.get("insufficient_data"))
        result["precip_available"] = precip is not None and not precip.empty
        logger.info("Station %s: %d/%d nodes characterized",
                    args.station, result["n_characterized"], result["n_nodes"])
    except Exception as exc:
        # Always write the declared output: one bad station must not hang the
        # characterisation fan-out.
        result["error"] = str(exc)
        logger.error("station_response failed for %s: %s", args.station, exc)

    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=2)


if __name__ == "__main__":
    main()
