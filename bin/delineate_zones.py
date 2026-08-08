#!/usr/bin/env python3
"""Delineate soil-moisture response zones from the covariate grid (M3).

This is the stage that makes the whole approach soil/terrain-aware rather than
distance-only (SPEC.md C2). It clusters the *covariate grid* — every pixel of
the M2 stack — into k response zones, then **validates those zones against the
M3 behavioural groups** derived independently from the observations. Agreement
is the evidence that the zones mean something; disagreement localises where the
covariates are missing something.

    covariates.tif (49 bands)              station_groups.csv
            │                                      │
            ├─ z-score, PCA to 95 % variance       │ behavioural groups
            ├─ KMeans, k by silhouette             │ (from response metrics)
            ├─ modal filter for map-usable zones   │
            v                                      v
        zones.tif  ─────────> adjusted Rand index, cross-tab
        soil_moisture_zones.geojson
        zone_stats.json (+ per-zone feature centroids)
        station_zones.csv

Two design points that matter downstream:

* **PCA before clustering.** The stack carries the same soil property at four
  depth intervals, so the raw bands are heavily collinear and KMeans on them
  would silently weight whichever property has the most bands. PCA on the
  standardised stack removes that arbitrariness.
* **Per-zone feature centroids are an output.** M4 needs them: a zone with no
  station gets its climatology from its nearest analogue *in covariate space*,
  not its nearest neighbour in metres. Distance-only substitution is exactly
  what C2 rules out.

No geopandas/shapely: polygons come from `rasterio.features.shapes` and ring
areas from the shoelace formula, keeping the container on python:3.11-slim.
"""

import argparse
import json
import logging
import sys
import time

import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.transform import rowcol
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("delineate_zones")

NODATA = -1
FIT_SAMPLE = 120_000      # pixels used to fit KMeans / PCA
SIL_SAMPLE = 5_000        # pixels used for silhouette (it is O(n^2))
PCA_VARIANCE = 0.95
PCA_MAX_COMPONENTS = 12
MODAL_WINDOW = 5          # pixels; 5 x 10 m = 50 m smoothing of zone edges
DEFAULT_MIN_POLYGON_HA = 1.0


def read_stack(path):
    """Read the covariate stack as (bands, names, transform, crs, nodata mask)."""
    with rasterio.open(path) as src:
        data = src.read().astype("float32")
        names = [src.descriptions[i] or f"band{i + 1}"
                 for i in range(src.count)]
        return data, names, src.transform, src.crs


def box_sum(a, w):
    """Square focal sum via an integral image (no scipy)."""
    pad = w // 2
    x = np.pad(a, pad + 1, mode="edge")
    c = x.cumsum(0).cumsum(1)
    return (c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w])[
        : a.shape[0], : a.shape[1]]


def modal_filter(labels, valid, k, w=MODAL_WINDOW):
    """Majority filter over a square window.

    Raw per-pixel KMeans output is salt-and-pepper: single pixels of zone 3
    inside zone 1 are not a management zone, they are noise in the covariates.
    Smoothing here rather than in the estimator keeps zones.tif and the map
    consistent with each other.
    """
    counts = np.stack([box_sum(((labels == g) & valid).astype("float32"), w)
                       for g in range(k)])
    out = np.argmax(counts, axis=0).astype("int16")
    out[~valid] = NODATA
    return out


def cluster_grid(data, valid, k_candidates, seed=0):
    """Standardise -> PCA -> KMeans; k chosen by silhouette score."""
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.metrics import silhouette_score

    n_bands = data.shape[0]
    X = data.reshape(n_bands, -1).T[valid.ravel()]
    logger.info("Clustering %d valid pixels x %d bands", len(X), n_bands)

    mean, std = X.mean(0), X.std(0)
    std[std < 1e-12] = np.nan
    keep = np.isfinite(std)
    if keep.sum() < 2:
        raise RuntimeError("fewer than 2 covariate bands vary across the grid")
    Xz = (X[:, keep] - mean[keep]) / std[keep]

    rng = np.random.default_rng(seed)
    fit_idx = rng.choice(len(Xz), size=min(FIT_SAMPLE, len(Xz)), replace=False)
    pca = PCA(n_components=min(PCA_MAX_COMPONENTS, int(keep.sum())),
              random_state=seed).fit(Xz[fit_idx])
    n_comp = int(np.searchsorted(np.cumsum(pca.explained_variance_ratio_),
                                PCA_VARIANCE) + 1)
    n_comp = max(2, min(n_comp, pca.n_components_))
    logger.info("PCA: %d components retain %.1f%% of variance", n_comp,
                100 * float(np.sum(pca.explained_variance_ratio_[:n_comp])))
    Z = pca.transform(Xz)[:, :n_comp]
    Zfit = Z[fit_idx]

    sil_idx = rng.choice(len(Z), size=min(SIL_SAMPLE, len(Z)), replace=False)
    scores, best = {}, (None, -1.0)
    for k in k_candidates:
        if k < 2 or k >= len(Zfit):
            continue
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(Zfit)
        s = float(silhouette_score(Z[sil_idx], km.predict(Z[sil_idx])))
        scores[int(k)] = round(s, 4)
        logger.info("  k=%d silhouette %.4f", k, s)
        if s > best[1]:
            best = (km, s)
    if best[0] is None:
        raise RuntimeError(f"no valid k in {k_candidates}")
    km = best[0]

    labels_flat = km.predict(Z)
    labels = np.full(valid.shape, NODATA, dtype="int16")
    labels[valid] = labels_flat

    # Centroids back in original covariate units — this is what M4 uses to
    # find a station-free zone's nearest analogue in covariate space.
    centroids_z = pca.inverse_transform(
        np.pad(km.cluster_centers_,
               ((0, 0), (0, pca.n_components_ - n_comp))))
    band_names_kept = np.where(keep)[0]
    centroids = centroids_z * std[keep] + mean[keep]

    info = {
        "k": int(km.n_clusters),
        "silhouette": round(best[1], 4),
        "silhouette_by_k": scores,
        "pca_components": n_comp,
        "pca_variance_retained": round(
            float(np.sum(pca.explained_variance_ratio_[:n_comp])), 4),
        "n_pixels_clustered": int(len(Z)),
        "n_pixels_fit": int(len(fit_idx)),
    }
    return labels, info, centroids, band_names_kept


def _ring_area_m2(ring):
    """Shoelace area of a projected linear ring (metres -> m^2)."""
    a = np.asarray(ring, dtype="float64")
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def polygonize(labels, transform, crs, min_polygon_ha):
    """Zone polygons as GeoJSON (EPSG:4326).

    Sub-threshold slivers are *tagged*, not dropped. Dropping them would punch
    holes in the polygon layer that read as missing data, and would make the
    geojson disagree with zones.tif about how much of the site is covered. A
    consumer that wants clean cartography filters on `below_min_area`.
    """
    feats, small, small_ha = [], 0, 0.0
    for geom, value in features.shapes(labels, mask=(labels != NODATA),
                                       transform=transform):
        rings = geom["coordinates"]
        area_m2 = _ring_area_m2(rings[0]) - sum(
            _ring_area_m2(r) for r in rings[1:])
        area_ha = area_m2 / 10_000.0
        below = area_ha < min_polygon_ha
        if below:
            small += 1
            small_ha += area_ha
        feats.append({
            "type": "Feature",
            "geometry": transform_geom(crs, "EPSG:4326", geom, precision=6),
            "properties": {"zone": int(value), "area_ha": round(area_ha, 3),
                           "below_min_area": below},
        })
    if small:
        logger.info("%d of %d polygons are below %.2f ha (%.1f ha total); "
                    "tagged below_min_area, not dropped",
                    small, len(feats), min_polygon_ha, small_ha)
    feats.sort(key=lambda f: -f["properties"]["area_ha"])
    return feats, small, small_ha


def zone_statistics(labels, data, names, k, res_m):
    """Mean/std of every covariate band per zone, plus area."""
    px_ha = (res_m * res_m) / 10_000.0
    stats = {}
    flat = labels.ravel()
    for g in range(k):
        sel = flat == g
        n = int(sel.sum())
        if not n:
            stats[str(g)] = {"n_pixels": 0, "area_ha": 0.0}
            continue
        block = data.reshape(data.shape[0], -1)[:, sel]
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(block, axis=1)
            std = np.nanstd(block, axis=1)
        stats[str(g)] = {
            "n_pixels": n,
            "area_ha": round(n * px_ha, 2),
            "covariate_mean": {nm: (round(float(v), 5) if np.isfinite(v)
                                    else None) for nm, v in zip(names, mean)},
            "covariate_std": {nm: (round(float(v), 5) if np.isfinite(v)
                                   else None) for nm, v in zip(names, std)},
        }
    return stats


def assign_stations(groups, labels, transform, crs):
    """Sample the zone raster at every observation node."""
    if groups is None or groups.empty:
        return pd.DataFrame(columns=["node", "lat", "lon", "zone"])
    df = groups.dropna(subset=["lat", "lon"]).copy()
    if df.empty:
        return pd.DataFrame(columns=["node", "lat", "lon", "zone"])
    xs, ys = warp_transform("EPSG:4326", crs,
                            df["lon"].astype(float).tolist(),
                            df["lat"].astype(float).tolist())
    zones, inside = [], []
    h, w = labels.shape
    for x, y in zip(xs, ys):
        r, c = rowcol(transform, x, y)
        if 0 <= r < h and 0 <= c < w and labels[r, c] != NODATA:
            zones.append(int(labels[r, c]))
            inside.append(True)
        else:
            zones.append(None)
            inside.append(False)
    df["zone"] = zones
    df["inside_grid"] = inside
    n_out = int((~df["inside_grid"]).sum())
    if n_out:
        logger.warning("%d node(s) fall outside the zone grid", n_out)
    return df


def validate(station_zones):
    """Do covariate zones agree with independently derived behaviour?

    Adjusted Rand index between the two labelings at the nodes, plus the raw
    cross-tab. With a handful of distinct locations this is a weak test and is
    reported as such — but a *negative* ARI is still informative: it says the
    covariates are organising the site differently from how it behaves.
    """
    from sklearn.metrics import adjusted_rand_score

    df = station_zones.dropna(subset=["zone"])
    if "group" not in df.columns:
        return {"skipped": "no behavioural groups supplied"}
    df = df[pd.to_numeric(df["group"], errors="coerce") >= 0]
    if len(df) < 4:
        return {"skipped": f"only {len(df)} nodes with both labels"}

    zone = df["zone"].astype(int).values
    group = df["group"].astype(int).values
    n_loc = int(df["lat"].round(4).astype(str).nunique())
    ct = pd.crosstab(pd.Series(group, name="behavioural_group"),
                     pd.Series(zone, name="covariate_zone"))
    # A node's zone comes from its coordinates, so all depth-nodes at one plot
    # share a zone while their behavioural groups differ by depth. The
    # surface-only view is the comparable one; both are reported.
    out = {
        "adjusted_rand_index": round(float(adjusted_rand_score(group, zone)), 4),
        "n_nodes": int(len(df)),
        "n_distinct_locations": n_loc,
        "crosstab": {str(g): {str(z): int(v) for z, v in row.items()}
                     for g, row in ct.iterrows()},
        "interpretation": (
            "Nodes share a zone per location but differ in behavioural group "
            "by depth, so a low ARI over all depths is expected; compare "
            "surface_only, and treat both as weak evidence at "
            f"{n_loc} distinct locations."),
    }
    surf = df[pd.to_numeric(df.get("depth_cm"), errors="coerce") <= 20]
    if len(surf) >= 4:
        out["surface_only"] = {
            "adjusted_rand_index": round(float(adjusted_rand_score(
                surf["group"].astype(int).values,
                surf["zone"].astype(int).values)), 4),
            "n_nodes": int(len(surf)),
        }
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Cluster the covariate grid into soil-moisture response zones")
    ap.add_argument("--covariates", required=True, help="covariates.tif from M2")
    ap.add_argument("--manifest", help="covariates_manifest.json (provenance)")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--groups", help="station_groups.csv from station_similarity")
    ap.add_argument("--output-zones", required=True, help="zones.tif")
    ap.add_argument("--output-geojson", required=True,
                    help="soil_moisture_zones.geojson")
    ap.add_argument("--output-stats", required=True, help="zone_stats.json")
    ap.add_argument("--output-membership", required=True,
                    help="station_zones.csv (node -> zone)")
    args = ap.parse_args()

    outputs = (args.output_zones, args.output_geojson, args.output_stats,
               args.output_membership)
    try:
        run(args)
    except Exception as exc:
        # Declared outputs must exist before a non-zero exit or HTCondor holds
        # the job on stage-out and the DAG hangs (SPEC.md section 4).
        for path in outputs:
            open(path, "a").close()
        logger.error("delineate_zones failed: %s", exc)
        sys.exit(1)


def run(args):
    with open(args.config) as fh:
        config = json.load(fh)
    analysis = config.get("analysis", {})
    k_candidates = analysis.get("cluster_k_candidates", [3, 4, 5, 6, 7, 8])
    min_poly_ha = float(analysis.get("min_zone_polygon_ha",
                                     DEFAULT_MIN_POLYGON_HA))

    data, names, transform, crs = read_stack(args.covariates)
    res_m = abs(float(transform.a))
    valid = np.all(np.isfinite(data), axis=0)
    logger.info("Stack %s: %d bands, %d x %d, %.1f%% valid pixels",
                args.covariates, len(names), data.shape[2], data.shape[1],
                100 * valid.mean())
    if valid.sum() < 100:
        raise RuntimeError(f"only {int(valid.sum())} pixels have all bands finite")

    labels, info, centroids, kept_bands = cluster_grid(data, valid, k_candidates)
    k = info["k"]
    labels = modal_filter(labels, valid, k)
    info["modal_filter_px"] = MODAL_WINDOW

    with rasterio.open(args.output_zones, "w", driver="GTiff", dtype="int16",
                       count=1, height=labels.shape[0], width=labels.shape[1],
                       crs=crs, transform=transform, nodata=NODATA,
                       compress="deflate", tiled=True) as dst:
        dst.write(labels, 1)
        dst.set_band_description(1, "response_zone")
        dst.update_tags(k=k, silhouette=info["silhouette"],
                        source="delineate_zones.py",
                        built_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime()))

    feats, n_small, small_ha = polygonize(labels, transform, crs, min_poly_ha)
    with open(args.output_geojson, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "crs": {"type": "name",
                           "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                   "features": feats}, fh)

    groups = None
    if args.groups:
        try:
            groups = pd.read_csv(args.groups)
        except Exception as exc:
            logger.warning("could not read %s (%s); zones will not be "
                           "validated against behaviour", args.groups, exc)
    station_zones = assign_stations(groups, labels, transform, crs)
    station_zones.to_csv(args.output_membership, index=False)

    stats = zone_statistics(labels, data, names, k, res_m)
    occupied = set(int(z) for z in station_zones.get("zone", pd.Series(dtype=float))
                   .dropna().tolist())
    for g in range(k):
        members = (station_zones[station_zones["zone"] == g]
                   if "zone" in station_zones else pd.DataFrame())
        stats[str(g)]["n_nodes"] = int(len(members))
        stats[str(g)]["nodes"] = (members["node"].tolist()
                                 if "node" in members else [])
        # Station-free zones are pure extrapolation on the dynamic map (C5).
        # M4 reads this flag to penalise their uncertainty and the figure
        # hatches them.
        stats[str(g)]["station_free"] = g not in occupied
        stats[str(g)]["feature_centroid"] = {
            names[b]: round(float(v), 5)
            for b, v in zip(kept_bands, centroids[g])}

    manifest_ref = {}
    if args.manifest:
        try:
            with open(args.manifest) as fh:
                m = json.load(fh)
            manifest_ref = {"covariate_manifest_built_utc": m.get("built_utc"),
                            "grid": m.get("grid"),
                            "n_covariate_bands": len(m.get("bands", []))}
        except Exception as exc:
            logger.warning("could not read manifest: %s", exc)

    station_free = [g for g in range(k) if stats[str(g)]["station_free"]]
    result = {
        "layer": "soil_moisture_zones",
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "site": config.get("site"),
        "clustering": info,
        "covariates_used": [names[b] for b in kept_bands],
        "grid": {"crs": str(crs), "resolution_m": res_m,
                 "width": int(labels.shape[1]), "height": int(labels.shape[0]),
                 "transform": list(transform)[:6]},
        "provenance": manifest_ref,
        "polygons": {"n_features": len(feats),
                     "min_polygon_ha": min_poly_ha,
                     "n_below_min_area": n_small,
                     "below_min_area_ha": round(small_ha, 2),
                     "note": "sub-threshold polygons are tagged "
                             "below_min_area, not dropped"},
        "zones": stats,
        "station_free_zones": station_free,
        "validation": validate(station_zones),
    }
    with open(args.output_stats, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    val = result["validation"]
    logger.info("k=%d zones, silhouette %.3f; %d polygon(s); "
                "station-free zones: %s; ARI vs behaviour: %s",
                k, info["silhouette"], len(feats),
                station_free or "none", val.get("adjusted_rand_index",
                                               val.get("skipped")))


if __name__ == "__main__":
    main()
