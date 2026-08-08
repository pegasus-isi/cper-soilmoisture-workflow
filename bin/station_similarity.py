#!/usr/bin/env python3
"""Behavioural grouping and covariate attribution of response fingerprints (M3).

Merges the per-station fingerprints produced by station_response.py and does
the two things the researcher's first ask actually needs:

  1. **Behavioural groups** — cluster depth-nodes on their *response* metrics
     (dry-down tau, event response, memory, seasonal amplitude, plant-available
     range), k chosen by silhouette score rather than by fiat. "These nodes
     dry down the same way."

  2. **Attribution** — relate each response metric to the M2 static covariates
     (soil texture, van Genuchten parameters, terrain) with a Spearman
     correlation matrix plus random-forest importances, yielding statements
     like "tau is driven mostly by clay % and TWI".

Depth is carried as a covariate in its own right: within a single soil profile
depth is the dominant control on response, and pretending otherwise would let
depth masquerade as a soil-texture effect.

Small-n honesty (SPEC.md C5, section 12): with only a handful of stations the
random forest is descriptive, not inferential. Importances are reported with
an out-of-bag-style holdout score and an explicit `n_samples`; when a metric
has too few usable nodes it is skipped rather than fitted to noise.
"""

import argparse
import json
import logging
import sys

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("station_similarity")

# Response metrics that define "behaviour" for clustering/attribution.
RESPONSE_METRICS = [
    "drydown_tau_days",
    "event_delta_per_mm_median",
    "event_lag_to_peak_days_median",
    "event_response_fraction",
    "memory_efolding_days",
    "seasonal_amplitude",
    "plant_available_range",
    "median",
    "std",
]

# Covariate columns to exclude from attribution (identifiers, not predictors).
NON_COVARIATES = {"id", "kind", "lat", "lon"}

MIN_SAMPLES_FIT = 6      # below this, do not fit a forest
MIN_SAMPLES_CLUSTER = 4
MIN_GROUPS_CV = 3        # distinct locations needed for grouped cross-validation


def load_fingerprints(paths):
    """Flatten every node fingerprint from every station file into a frame."""
    rows = []
    for path in paths:
        with open(path) as fh:
            d = json.load(fh)
        if d.get("error"):
            logger.warning("%s reported an error: %s", path, d["error"])
        for node in d.get("nodes", []):
            if node.get("insufficient_data"):
                logger.info("skipping %s: %s", node.get("node"),
                            node.get("reason"))
                continue
            row = {"node": node["node"], "station": d["station"],
                   "depth_cm": node.get("depth_cm"),
                   "source": node.get("source"),
                   "lat": node.get("lat"), "lon": node.get("lon"),
                   "n_recessions": node.get("n_recessions"),
                   "n_events": node.get("n_events"),
                   "n_days": (node.get("quality") or {}).get("n_days")}
            for m in RESPONSE_METRICS:
                row[m] = node.get(m)
            rows.append(row)
    # Sort by node id so results never depend on the order --fingerprints was
    # given in. KMeans++ seeds its initial centres by sampling rows, so a
    # different argument order could land on a different local optimum and
    # move a borderline node between behavioural groups - which is exactly
    # what happened between a shell glob (alphabetical) and the generator's
    # config order.
    return (pd.DataFrame(rows).sort_values("node").reset_index(drop=True)
            if rows else pd.DataFrame(rows))


def match_covariates(fp, cov):
    """Join station covariates onto node fingerprints.

    station_covariates.csv keys on station/node id. A NEON depth-node
    (NEON:CPER:SP4@26cm) shares the covariate vector of its plot or station,
    so match on the longest id prefix present in the covariate table.
    """
    if cov is None or cov.empty:
        return pd.DataFrame(index=fp.index)
    cov = cov.set_index("id")
    keys = sorted(cov.index.astype(str), key=len, reverse=True)
    out = []
    for node in fp["node"]:
        match = next((k for k in keys if str(node).startswith(k)), None)
        out.append(cov.loc[match] if match else pd.Series(dtype=float))
    joined = pd.DataFrame(out).reset_index(drop=True)
    return joined.drop(columns=[c for c in joined.columns if c in NON_COVARIATES],
                       errors="ignore")


def zscore(frame):
    """Standardise, dropping all-NaN / zero-variance columns."""
    f = frame.astype(float)
    f = f.loc[:, f.notna().sum() >= max(3, int(0.6 * len(f)))]
    f = f.fillna(f.median())
    std = f.std(ddof=0).replace(0, np.nan)
    f = f.loc[:, std.notna()]
    return (f - f.mean()) / f.std(ddof=0), list(f.columns)


def cluster_nodes(fp, k_candidates):
    """KMeans on standardised response metrics; k by silhouette score."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    X, used = zscore(fp[RESPONSE_METRICS])
    if len(X) < MIN_SAMPLES_CLUSTER or X.shape[1] < 2:
        return None, {"reason": f"only {len(X)} usable nodes / "
                                f"{X.shape[1]} metrics"}, used

    scores = {}
    best = (None, -1)
    for k in k_candidates:
        if k >= len(X):
            continue
        km = KMeans(n_clusters=k, n_init=10, random_state=0).fit(X.values)
        s = float(silhouette_score(X.values, km.labels_))
        scores[k] = round(s, 4)
        if s > best[1]:
            best = (km, s)
    if best[0] is None:
        return None, {"reason": "no valid k"}, used
    km = best[0]
    return km.labels_, {"k": int(km.n_clusters),
                        "silhouette": round(best[1], 4),
                        "silhouette_by_k": scores}, used


def location_key(fp):
    """A grouping label per node: nodes at the same coordinates share one."""
    return (fp[["lat", "lon"]].round(4).astype(str)
            .agg(",".join, axis=1).values)


def attribute(fp, cov, metrics):
    """Correlate + random-forest each response metric onto the covariates.

    Cross-validation folds are grouped by **location**, not by node. Sibling
    depth-nodes at one soil plot share a coordinate, so inside a depth band
    (where depth_cm is dropped) their covariate vectors are *identical* — 16 of
    the 24 surface rows on the real network are exact duplicates of another row.
    Leave-one-node-out would therefore train on a row identical to the held-out
    one and report a leaked score: on the real data it turned an honest
    plant_available_range R^2 of -0.57 into +0.61. Grouping by location removes
    that, and costs 8 folds instead of 55 while doing so.
    """
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import (LeaveOneGroupOut, LeaveOneOut,
                                         cross_val_predict)

    X_all = cov.copy()
    X_all["depth_cm"] = fp["depth_cm"].values   # depth is a real control
    X_all, cols = zscore(X_all)
    if X_all.empty or X_all.shape[1] == 0:
        return {"error": "no usable covariates"}

    loc_all = location_key(fp)
    out = {"covariates_used": cols, "n_samples": int(len(X_all)), "metrics": {}}
    for m in metrics:
        y = pd.to_numeric(fp[m], errors="coerce")
        ok = y.notna().values
        if ok.sum() < MIN_SAMPLES_FIT:
            out["metrics"][m] = {"skipped": f"only {int(ok.sum())} usable nodes"}
            continue
        Xm, ym, groups = X_all[ok], y[ok], loc_all[ok]

        spearman = {c: round(float(pd.Series(Xm[c].values).corr(
            pd.Series(ym.values), method="spearman")), 4) for c in Xm.columns}
        rf = RandomForestRegressor(n_estimators=400, random_state=0,
                                   min_samples_leaf=1)
        rf.fit(Xm.values, ym.values)
        imp = sorted(zip(Xm.columns, rf.feature_importances_),
                     key=lambda t: -t[1])

        # Generalisation estimate. Grouped by location where there are enough
        # distinct locations to leave one out; otherwise fall back to
        # leave-one-node-out and say so, because a leaked number that is
        # labelled as leaked is still better than no number.
        n_groups = len(set(groups))
        try:
            if n_groups >= MIN_GROUPS_CV:
                cv, scheme = LeaveOneGroupOut(), "leave-one-location-out"
                pred = cross_val_predict(rf, Xm.values, ym.values, cv=cv,
                                         groups=groups)
                n_folds = n_groups
            else:
                cv, scheme = LeaveOneOut(), "leave-one-node-out (LEAKY)"
                pred = cross_val_predict(rf, Xm.values, ym.values, cv=cv)
                n_folds = int(ok.sum())
            ss_res = float(np.sum((ym.values - pred) ** 2))
            ss_tot = float(np.sum((ym.values - ym.values.mean()) ** 2))
            cv_r2 = round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None
        except Exception as exc:
            logger.warning("cross-validation failed for %s: %s", m, exc)
            cv_r2, scheme, n_folds = None, "failed", 0

        out["metrics"][m] = {
            "n": int(ok.sum()),
            "n_locations": n_groups,
            "top_drivers": [{"covariate": c, "importance": round(float(v), 4),
                             "spearman": spearman.get(c)}
                            for c, v in imp[:5]],
            "cv_r2": cv_r2,
            "cv_scheme": scheme,
            "cv_folds": n_folds,
            # Kept under the old key as well so existing readers do not break.
            "loo_r2": cv_r2,
            "spearman": spearman,
        }
    return out


def attribute_by_depth_band(fp, cov, metrics, bands):
    """Attribution *within* depth bands, i.e. controlling for depth.

    Depth is the dominant control on soil-moisture response inside a single
    profile, so a pooled fit mostly rediscovers that. The question the
    researcher actually needs answered is the depth-controlled one: at a
    comparable depth, which soil and terrain properties separate one location
    from another? Requires genuinely distinct locations to be meaningful —
    with per-plot NEON coordinates this has 5 sites, with station-level
    coordinates only 2-4.
    """
    out = {}
    depth = pd.to_numeric(fp["depth_cm"], errors="coerce")
    for label, (lo, hi) in bands.items():
        sel = ((depth >= lo) & (depth < hi)).values
        n_sites = fp.loc[sel, "lat"].round(4).astype(str).nunique() if sel.any() else 0
        if sel.sum() < MIN_SAMPLES_FIT or n_sites < 3:
            out[label] = {"skipped": f"{int(sel.sum())} nodes / {n_sites} "
                                     f"distinct locations in {lo}-{hi} cm "
                                     f"(need >={MIN_SAMPLES_FIT} nodes and "
                                     f">=3 locations)"}
            continue
        sub_fp = fp[sel].reset_index(drop=True)
        sub_cov = cov[sel].reset_index(drop=True)
        # Drop depth from the predictors: within a band it is nearly constant
        # and would otherwise soak up importance again.
        res = attribute(sub_fp.assign(depth_cm=np.nan), sub_cov, metrics)
        res["depth_band_cm"] = [lo, hi]
        res["n_distinct_locations"] = int(n_sites)
        out[label] = res
    return out


def _config_value(path, key, default):
    if not path:
        return default
    try:
        with open(path) as fh:
            return json.load(fh).get("analysis", {}).get(key, default)
    except Exception:
        return default


def do_cluster(args, fp):
    """Stage `cluster`: behavioural groups only. Cheap (~1 s)."""
    k_candidates = _config_value(args.config, "cluster_k_candidates",
                                 [3, 4, 5, 6])
    labels, cluster_info, metrics_used = cluster_nodes(fp, k_candidates)
    result = {"layer": "station_clusters", "clustering": cluster_info,
              "response_metrics_used": metrics_used}
    if labels is not None:
        fp["group"] = labels
        result["groups"] = {
            str(g): {
                "n_nodes": int((fp["group"] == g).sum()),
                "nodes": fp.loc[fp["group"] == g, "node"].tolist(),
                "mean_metrics": {
                    m: (round(float(v), 4) if pd.notna(v) else None)
                    for m, v in fp.loc[fp["group"] == g, metrics_used]
                    .mean(numeric_only=True).items()},
            } for g in sorted(set(labels))}
    else:
        fp["group"] = -1
        logger.warning("clustering skipped: %s", cluster_info.get("reason"))

    result["n_nodes"] = int(len(fp))
    result["n_distinct_locations"] = int(len(set(location_key(fp))))
    result["depth_range_cm"] = [
        (float(fp["depth_cm"].min()) if fp["depth_cm"].notna().any() else None),
        (float(fp["depth_cm"].max()) if fp["depth_cm"].notna().any() else None)]
    return result, fp


def do_attribute(args, fp):
    """Stage `attribute`: covariate attribution for a subset of metrics.

    Split out so it can fan out one job per response metric and, more
    importantly, so it stops sitting on the critical path: nothing downstream
    of here reads station_similarity.json except visualize_response. The zone
    delineation and the whole M4 chain need only station_groups.csv, which the
    cluster stage produces in about a second.
    """
    cov = None
    if args.covariates:
        cov = pd.read_csv(args.covariates)
    cov_matched = match_covariates(fp, cov)
    if cov_matched.empty:
        return {"layer": "station_attribution",
                "skipped": "no station covariates matched"}

    # Recomputed rather than passed in, so this stage does not have to wait on
    # the cluster stage. zscore() is deterministic and cheap - no fitting.
    _, usable = zscore(fp[RESPONSE_METRICS])
    metrics = [m for m in (args.metrics or RESPONSE_METRICS) if m in usable]
    dropped = [m for m in (args.metrics or RESPONSE_METRICS) if m not in usable]
    if dropped:
        logger.info("not attributable (no variance / too sparse): %s",
                    ", ".join(dropped))
    if not metrics:
        return {"layer": "station_attribution", "metrics_requested":
                list(args.metrics or RESPONSE_METRICS),
                "skipped": "none of the requested metrics are usable"}
    logger.info("attributing %d metric(s): %s", len(metrics), ", ".join(metrics))

    surface_max = _config_value(args.config, "surface_depth_max_cm", 20)
    out = {"layer": "station_attribution",
           "metrics_requested": metrics,
           "pooled": attribute(fp, cov_matched, metrics),
           "by_depth_band": attribute_by_depth_band(
               fp, cov_matched, metrics,
               {"surface": (0, surface_max + 1),
                "deep": (surface_max + 1, 1e6)}),
           "n_distinct_locations": int(len(set(location_key(fp))))}
    for m, info in (out["pooled"].get("metrics") or {}).items():
        if info.get("top_drivers"):
            top = info["top_drivers"][0]
            logger.info("%s <- %s (importance %.3f, rho %s, %s R2 %s over %s folds)",
                        m, top["covariate"], top["importance"], top["spearman"],
                        info.get("cv_scheme"), info.get("cv_r2"),
                        info.get("cv_folds"))
    return out


def do_merge(cluster_path, attribution_paths):
    """Stage `merge`: reassemble station_similarity.json from the partials."""
    with open(cluster_path) as fh:
        cluster = json.load(fh)

    result = {"layer": "station_similarity",
              "clustering": cluster.get("clustering"),
              "response_metrics_used": cluster.get("response_metrics_used"),
              "groups": cluster.get("groups"),
              "n_nodes": cluster.get("n_nodes"),
              "n_distinct_locations": cluster.get("n_distinct_locations"),
              "depth_range_cm": cluster.get("depth_range_cm")}

    pooled = {"metrics": {}}
    bands, skipped = {}, []
    for path in attribution_paths:
        with open(path) as fh:
            part = json.load(fh)
        if part.get("skipped"):
            skipped.append({"file": str(path), "reason": part["skipped"]})
            continue
        p = part.get("pooled") or {}
        for key in ("covariates_used", "n_samples"):
            if key in p:
                pooled.setdefault(key, p[key])
        pooled["metrics"].update(p.get("metrics") or {})
        for band, res in (part.get("by_depth_band") or {}).items():
            if not isinstance(res, dict):
                continue
            if res.get("skipped"):
                bands.setdefault(band, res)
                continue
            slot = bands.get(band)
            if not slot or slot.get("skipped"):
                slot = {k: v for k, v in res.items() if k != "metrics"}
                slot["metrics"] = {}
                bands[band] = slot
            slot["metrics"].update(res.get("metrics") or {})

    result["attribution"] = pooled if pooled["metrics"] else {
        "skipped": "no attribution partials contained usable metrics"}
    result["attribution_by_depth_band"] = bands
    if skipped:
        result["attribution_partials_skipped"] = skipped
    logger.info("merged %d attribution partial(s): %d pooled metric(s), "
                "bands %s", len(attribution_paths), len(pooled["metrics"]),
                sorted(bands))
    return result


def main():
    ap = argparse.ArgumentParser(
        description="Cluster response fingerprints and attribute them to covariates")
    ap.add_argument("--stage", default="all",
                    choices=["all", "cluster", "attribute", "merge"],
                    help="`all` (default) does everything in one process, which "
                         "is what the notebooks and a laptop run want. The DAG "
                         "splits it: `cluster` is ~1 s and unblocks the zone/M4 "
                         "chain, `attribute` fans out per metric, `merge` "
                         "reassembles station_similarity.json.")
    ap.add_argument("--fingerprints", nargs="+",
                    help="response_*.json files from station_response")
    ap.add_argument("--covariates", help="station_covariates.csv from M2")
    ap.add_argument("--config", help="site_config.json (cluster_k_candidates)")
    ap.add_argument("--metrics", nargs="+",
                    help="stage=attribute: response metrics to attribute "
                         "(default: all of them)")
    ap.add_argument("--clusters", help="stage=merge: the cluster stage's JSON")
    ap.add_argument("--attributions", nargs="*", default=[],
                    help="stage=merge: the attribute stages' JSONs")
    ap.add_argument("--output", required=True,
                    help="station_similarity.json, or the stage's partial JSON")
    ap.add_argument("--output-groups",
                    help="station_groups.csv (stages all/cluster)")
    args = ap.parse_args()

    needs_groups = args.stage in ("all", "cluster")
    if needs_groups and not args.output_groups:
        ap.error("--output-groups is required for stage %s" % args.stage)
    if args.stage != "merge" and not args.fingerprints:
        ap.error("--fingerprints is required for stage %s" % args.stage)
    if args.stage == "merge" and not args.clusters:
        ap.error("--clusters is required for stage merge")

    result, fp = {"layer": "station_similarity"}, pd.DataFrame()
    try:
        if args.stage == "merge":
            result = do_merge(args.clusters, args.attributions)
        else:
            fp = load_fingerprints(args.fingerprints)
            if fp.empty:
                raise RuntimeError("no usable fingerprints (all nodes were "
                                   "insufficient or errored)")
            logger.info("Loaded %d node fingerprints from %d station files",
                        len(fp), len(args.fingerprints))

            if args.stage == "cluster":
                result, fp = do_cluster(args, fp)
            elif args.stage == "attribute":
                result = do_attribute(args, fp)
            else:                                    # all
                cluster_result, fp = do_cluster(args, fp)
                attr = do_attribute(args, fp)
                result = dict(cluster_result, layer="station_similarity")
                if attr.get("skipped"):
                    result["attribution"] = {"skipped": attr["skipped"]}
                    result["attribution_by_depth_band"] = {}
                else:
                    result["attribution"] = attr["pooled"]
                    result["attribution_by_depth_band"] = attr["by_depth_band"]
    except Exception as exc:
        result["error"] = str(exc)
        logger.error("station_similarity (stage=%s) failed: %s", args.stage, exc)

    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    if needs_groups:
        (fp if not fp.empty else pd.DataFrame(columns=["node", "group"])
         ).to_csv(args.output_groups, index=False)

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
