#!/usr/bin/env python3
"""Fetch the USGS 3DEP 10 m DEM cropped to the CPER bbox (M2 static branch).

Reads a window straight out of the single Cloud-Optimized GeoTIFF that covers
the whole site (SPEC.md section 10: USGS_13_n41w105.tif on the anonymous
prd-tnm S3 bucket) via GDAL's /vsicurl/, so only the ~7 MB covering CPER moves
over the network, not the 1-degree tile. The crop is written in the source CRS
and resolution; reprojection onto the analysis grid and all terrain
derivatives happen in build_covariates.py.

Failure policy (SPEC.md section 4): terrain is a REQUIRED static source — on
persistent failure write the (empty) declared output so stage-out succeeds,
then exit non-zero so the static branch stops loudly.
"""

import argparse
import json
import logging
import sys
import time

import rasterio
from rasterio.windows import from_bounds

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_terrain")

# Buffer around the bbox (degrees, ~1 km) so focal terrain derivatives
# (TPI, TWI, curvature) have real neighbourhoods at the site boundary.
BBOX_BUFFER_DEG = 0.01

RETRIES = 4
BACKOFF_S = 5


def fetch_dem_crop(url, bounds, output):
    """Windowed read of the DEM COG over HTTP; write the crop as GTiff."""
    vsiurl = f"/vsicurl/{url}" if "://" in url else url
    with rasterio.open(vsiurl) as src:
        window = from_bounds(*bounds, transform=src.transform)
        data = src.read(1, window=window, boundless=True,
                        fill_value=src.nodata if src.nodata is not None else 0)
        transform = src.window_transform(window)
        profile = {
            "driver": "GTiff", "dtype": data.dtype, "count": 1,
            "height": data.shape[0], "width": data.shape[1],
            "crs": src.crs, "transform": transform, "nodata": src.nodata,
            "compress": "deflate", "predictor": 3, "tiled": True,
        }
        with rasterio.open(output, "w", **profile) as dst:
            dst.write(data, 1)
            dst.set_band_description(1, "elevation")
            dst.update_tags(
                source_url=url,
                source_product="USGS 3DEP 1/3 arc-second DEM",
                fetched_utc=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                bbox=json.dumps(list(bounds)),
                units="m",
            )
    return data.shape


def main():
    ap = argparse.ArgumentParser(description="Fetch 3DEP DEM crop for CPER")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--output", required=True, help="Output DEM GeoTIFF")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    bbox = config["bbox"]
    url = config["static_layers"]["terrain"]["dem_10m_cog"]
    bounds = (bbox["min_lon"] - BBOX_BUFFER_DEG, bbox["min_lat"] - BBOX_BUFFER_DEG,
              bbox["max_lon"] + BBOX_BUFFER_DEG, bbox["max_lat"] + BBOX_BUFFER_DEG)

    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            shape = fetch_dem_crop(url, bounds, args.output)
            logger.info("Wrote DEM crop %sx%s -> %s", shape[0], shape[1],
                        args.output)
            return
        except Exception as exc:  # rasterio raises non-requests exceptions
            last = exc
            logger.warning("DEM fetch attempt %d/%d failed: %s",
                           attempt, RETRIES, exc)
            if attempt < RETRIES:
                time.sleep(BACKOFF_S * attempt)

    # REQUIRED source: declared output must exist before the non-zero exit,
    # or HTCondor holds the job on stage-out and hangs the DAG.
    open(args.output, "wb").close()
    logger.error("DEM unavailable after %d attempts (%s); wrote empty %s and "
                 "failing the static branch.", RETRIES, last, args.output)
    sys.exit(1)


if __name__ == "__main__":
    main()
