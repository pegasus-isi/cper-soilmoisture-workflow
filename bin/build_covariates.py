#!/usr/bin/env python3
"""Build the CPER static covariate stack (M2).

Consumes the raw fetch products (DEM crop, POLARIS multiband, SDA JSON),
defines the analysis grid (UTM 13N, 10 m, bbox snapped outward — SPEC.md
section 8 M2), reprojects everything onto it, derives the terrain covariates
in numpy (slope, northness/eastness, plan/profile curvature, TPI, D8 flow
accumulation -> TWI, McCune-Keon heat-load index), and writes:

  * covariates.tif           multiband float32 stack on the analysis grid
  * covariates_manifest.json every band's source, units and provenance,
                             plus the grid definition (C9 provenance)
  * station_covariates.csv   the covariate vector at each station (from
                             site_config) and, when --observations is given,
                             at each observation node (per-depth coordinates)

Terrain derivatives live here rather than in fetch_terrain so the math runs
once, in metric space, on the final grid (and the fetchers stay pure
fetchers). All derivatives are plain numpy — no scipy/whitebox (SPEC.md
"Container": keep terrain derivatives in numpy where practical).
"""

import argparse
import json
import logging
import sys
import time

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import Affine, rowcol
from rasterio.warp import Resampling, reproject, transform as warp_transform
from rasterio.warp import transform_bounds

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("build_covariates")

TPI_WINDOW_M = 310  # focal window for TPI; odd multiple of the grid step


def define_grid(config):
    """Analysis grid: bbox -> target CRS, snapped outward to the resolution."""
    grid_cfg = config["static_layers"]["analysis_grid"]
    crs = grid_cfg["crs"]
    res = float(grid_cfg["resolution_m"])
    bbox = config["bbox"]
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", crs,
        bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["max_lat"])
    left, bottom = np.floor(left / res) * res, np.floor(bottom / res) * res
    right, top = np.ceil(right / res) * res, np.ceil(top / res) * res
    width, height = int((right - left) / res), int((top - bottom) / res)
    transform = Affine(res, 0, left, 0, -res, top)
    return crs, transform, width, height, res


def regrid(src_path, crs, transform, width, height, band=None):
    """Reproject one raster (or one band of it) onto the analysis grid."""
    with rasterio.open(src_path) as src:
        indexes = [band] if band else list(src.indexes)
        names = [src.descriptions[i - 1] or f"band{i}" for i in indexes]
        tags = src.tags()
        out = np.full((len(indexes), height, width), np.nan, dtype="float32")
        for k, i in enumerate(indexes):
            reproject(
                source=rasterio.band(src, i), destination=out[k],
                dst_transform=transform, dst_crs=crs,
                resampling=Resampling.bilinear,
                src_nodata=src.nodata, dst_nodata=np.nan)
    return out, names, tags


def _shift(a, dr, dc):
    """Neighbour view of a 2-D array, NaN-padded at the borders."""
    out = np.full_like(a, np.nan)
    rows = slice(max(dr, 0), a.shape[0] + min(dr, 0))
    cols = slice(max(dc, 0), a.shape[1] + min(dc, 0))
    src_rows = slice(max(-dr, 0), a.shape[0] + min(-dr, 0))
    src_cols = slice(max(-dc, 0), a.shape[1] + min(-dc, 0))
    out[rows, cols] = a[src_rows, src_cols]
    return out


def box_mean(a, w):
    """NaN-aware square focal mean via integral images (no scipy)."""
    filled = np.nan_to_num(a, nan=0.0)
    valid = np.isfinite(a).astype("float64")
    pad = w // 2

    def boxsum(x):
        x = np.pad(x, pad + 1, mode="edge")
        c = x.cumsum(0).cumsum(1)
        return (c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w])[
            : a.shape[0], : a.shape[1]]

    counts = boxsum(valid)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, boxsum(filled) / counts, np.nan)


def d8_flow_accumulation(dem, res):
    """D8 accumulation (contributing cells) on the raw DEM, numpy only.

    Pits simply stop accumulating (no fill step) — noisier than a hydrologic
    conditioning pass but adequate for a covariate; noted in the manifest.
    """
    h, w = dem.shape
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
                  (0, 1), (1, -1), (1, 0), (1, 1)]
    best_drop = np.full(dem.shape, 0.0)
    receiver = np.full(dem.shape, -1, dtype="int64")
    idx = np.arange(dem.size).reshape(dem.shape)
    for dr, dc in neighbours:
        dist = res * (np.sqrt(2) if dr and dc else 1.0)
        drop = (dem - _shift(dem, -dr, -dc)) / dist  # positive = downhill
        take = np.isfinite(drop) & (drop > best_drop)
        best_drop = np.where(take, drop, best_drop)
        receiver = np.where(take, _shift(idx.astype("float64"), -dr, -dc),
                            receiver).astype("int64")

    flat_dem = dem.ravel()
    flat_recv = receiver.ravel()
    acc = np.ones(dem.size)
    acc[~np.isfinite(flat_dem)] = 0
    order = np.argsort(-np.nan_to_num(flat_dem, nan=-np.inf))
    for i in order:
        r = flat_recv[i]
        if r >= 0:
            acc[r] += acc[i]
    acc = acc.reshape(dem.shape)
    acc[~np.isfinite(dem)] = np.nan
    return acc


def terrain_covariates(dem, res, mean_lat_deg):
    """All terrain bands from the projected DEM. Returns {name: array}."""
    gy, gx = np.gradient(dem, res)  # gy: along rows (southward), gx: eastward
    gn = -gy  # northward gradient
    slope_rad = np.arctan(np.hypot(gx, gn))
    mag = np.hypot(gx, gn)
    with np.errstate(invalid="ignore", divide="ignore"):
        # Unit vector of the downslope direction (aspect); 0 on flats.
        northness = np.where(mag > 0, -gn / mag, 0.0)
        eastness = np.where(mag > 0, -gx / mag, 0.0)

    # Zevenbergen & Thorne (1987) curvatures from the 3x3 neighbourhood.
    z = dem
    zx = (_shift(z, 0, 1) - _shift(z, 0, -1)) / (2 * res)
    zy = (_shift(z, -1, 0) - _shift(z, 1, 0)) / (2 * res)
    zxx = (_shift(z, 0, 1) - 2 * z + _shift(z, 0, -1)) / res**2
    zyy = (_shift(z, -1, 0) - 2 * z + _shift(z, 1, 0)) / res**2
    zxy = (_shift(z, -1, 1) - _shift(z, -1, -1)
           - _shift(z, 1, 1) + _shift(z, 1, -1)) / (4 * res**2)
    p = zx**2 + zy**2
    with np.errstate(invalid="ignore", divide="ignore"):
        curv_profile = np.where(
            p > 1e-12,
            -(zxx * zx**2 + 2 * zxy * zx * zy + zyy * zy**2)
            / (p * np.power(1 + p, 1.5)), 0.0)
        curv_plan = np.where(
            p > 1e-12,
            -(zxx * zy**2 - 2 * zxy * zx * zy + zyy * zx**2)
            / np.power(p, 1.5), 0.0)

    tpi = dem - box_mean(dem, int(TPI_WINDOW_M / res) // 2 * 2 + 1)

    acc = d8_flow_accumulation(dem, res)
    with np.errstate(invalid="ignore", divide="ignore"):
        twi = np.log((acc * res) / np.maximum(np.tan(slope_rad), 1e-3))

    # McCune & Keon (2002) heat-load index; folded aspect about SW.
    aspect = np.degrees(np.arctan2(eastness, northness)) % 360.0
    folded = np.radians(np.abs(180.0 - np.abs(aspect - 225.0)))
    lat = np.radians(mean_lat_deg)
    hli = (0.339 + 0.808 * np.cos(lat) * np.cos(slope_rad)
           - 0.196 * np.sin(lat) * np.sin(slope_rad)
           - 0.482 * np.cos(folded) * np.sin(slope_rad))

    return {
        "elevation": dem,
        "slope_deg": np.degrees(slope_rad),
        "northness": northness,
        "eastness": eastness,
        "curv_plan": curv_plan,
        "curv_profile": curv_profile,
        f"tpi_{TPI_WINDOW_M}m": tpi,
        "twi": twi,
        "heat_load": hli,
    }


TERRAIN_UNITS = {
    "elevation": "m", "slope_deg": "deg", "northness": "-", "eastness": "-",
    "curv_plan": "1/m", "curv_profile": "1/m", f"tpi_{TPI_WINDOW_M}m": "m",
    "twi": "ln(m)", "heat_load": "-",
}


def station_points(config, observations_path):
    """(label, lat, lon, kind) for config stations + optional obs nodes."""
    pts = []
    for st in config.get("stations", []):
        if st.get("lat") is not None and st.get("lon") is not None:
            pts.append((st["id"], float(st["lat"]), float(st["lon"]), "station"))
    if observations_path:
        obs = pd.read_csv(observations_path)
        if not obs.empty:
            nodes = (obs[["node", "lat", "lon"]].dropna()
                     .drop_duplicates(subset=["node"]))
            existing = {p[0] for p in pts}
            pts += [(r.node, float(r.lat), float(r.lon), "node")
                    for r in nodes.itertuples() if r.node not in existing]
    return pts


def main():
    ap = argparse.ArgumentParser(description="Build the CPER covariate stack")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--dem", required=True, help="DEM crop from fetch_terrain")
    ap.add_argument("--polaris", required=True,
                    help="POLARIS multiband tif from fetch_soil_properties")
    ap.add_argument("--sda", required=True,
                    help="SDA JSON from fetch_soil_properties")
    ap.add_argument("--observations", help="Optional observations.csv whose "
                    "node coordinates are added to station_covariates")
    ap.add_argument("--output-stack", required=True, help="covariates.tif")
    ap.add_argument("--output-manifest", required=True,
                    help="covariates_manifest.json")
    ap.add_argument("--output-stations", required=True,
                    help="station_covariates.csv")
    args = ap.parse_args()

    try:
        run(args)
    except Exception as exc:
        # Write every declared output before the non-zero exit (SPEC.md
        # section 4) so stage-out succeeds and the DAG fails cleanly.
        for path in (args.output_stack, args.output_manifest,
                     args.output_stations):
            open(path, "a").close()
        logger.error("build_covariates failed: %s", exc)
        sys.exit(1)


def run(args):
    with open(args.config) as fh:
        config = json.load(fh)

    crs, transform, width, height, res = define_grid(config)
    logger.info("Analysis grid: %s, %.0f m, %d x %d", crs, res, width, height)

    dem_grid, _, dem_tags = regrid(args.dem, crs, transform, width, height,
                                   band=1)
    bbox = config["bbox"]
    terrain = terrain_covariates(
        dem_grid[0].astype("float64"), res,
        (bbox["min_lat"] + bbox["max_lat"]) / 2)

    soil_grid, soil_names, soil_tags = regrid(
        args.polaris, crs, transform, width, height)
    soil_units = json.loads(soil_tags.get("units", "{}"))

    names = list(terrain) + soil_names
    stack = np.stack([terrain[n] for n in terrain]
                     + [soil_grid[k] for k in range(len(soil_names))]
                     ).astype("float32")

    with rasterio.open(args.output_stack, "w", driver="GTiff",
                       dtype="float32", count=len(names), height=height,
                       width=width, crs=crs, transform=transform,
                       nodata=np.nan, compress="deflate", predictor=3,
                       tiled=True, bigtiff="if_safer") as dst:
        dst.write(stack)
        for i, name in enumerate(names, start=1):
            dst.set_band_description(i, name)

    bands = []
    for i, name in enumerate(names, start=1):
        is_terrain = name in TERRAIN_UNITS
        bands.append({
            "band": i, "name": name,
            "source": (dem_tags.get("source_product", "3DEP DEM")
                       if is_terrain
                       else soil_tags.get("source_product", "POLARIS v1.0")),
            "source_url": (dem_tags.get("source_url") if is_terrain
                           else soil_tags.get("source_url")),
            "fetched_utc": (dem_tags.get("fetched_utc") if is_terrain
                            else soil_tags.get("fetched_utc")),
            "units": (TERRAIN_UNITS.get(name)
                      or soil_units.get(name.rsplit("_", 1)[0], "")),
        })
    manifest = {
        "layer": "covariates",
        "built_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "grid": {"crs": str(crs), "resolution_m": res,
                 "width": width, "height": height,
                 "transform": list(transform)[:6]},
        "bands": bands,
        "notes": [
            "terrain derivatives computed in numpy on the analysis grid",
            "TWI uses D8 accumulation on the unconditioned DEM (no pit "
            "fill); adequate as a covariate, not for hydrologic routing",
            "log10-transformed POLARIS variables (ksat, alpha, om) were "
            "converted back to linear units at fetch time",
        ],
        "sda": {"file": "sda_soil.json"},
    }
    with open(args.output_manifest, "w") as fh:
        json.dump(manifest, fh, indent=2)

    pts = station_points(config, args.observations)
    rows = []
    for label, lat, lon, kind in pts:
        xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
        r, c = rowcol(transform, xs[0], ys[0])
        row = {"id": label, "kind": kind, "lat": lat, "lon": lon}
        if 0 <= r < height and 0 <= c < width:
            row.update({n: (round(float(v), 6) if np.isfinite(v) else None)
                        for n, v in zip(names, stack[:, r, c])})
        else:
            logger.warning("%s (%.4f, %.4f) is outside the analysis grid",
                           label, lat, lon)
        rows.append(row)
    pd.DataFrame(rows).to_csv(args.output_stations, index=False)

    logger.info("Wrote %d bands -> %s; %d station/node vectors -> %s",
                len(names), args.output_stack, len(rows),
                args.output_stations)


if __name__ == "__main__":
    main()
