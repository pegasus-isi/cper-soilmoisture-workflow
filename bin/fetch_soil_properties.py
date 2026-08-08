#!/usr/bin/env python3
"""Fetch static soil-property inputs for CPER (M2 static branch).

Two complementary products (SPEC.md section 8, M2):

  * POLARIS (~30 m gridded, anonymous, HTTP-only host): continuous surfaces
    including the van Genuchten parameters (theta_s, theta_r, alpha, n), so no
    pedotransfer step is needed. One 1x1-degree tile covers all of CPER; only
    the bbox window is read via /vsicurl/. Output: multiband GeoTIFF in the
    source CRS, one band per variable x depth interval, log10-transformed
    variables (ksat, alpha, om) converted back to linear units.

  * Soil Data Access (SDA, anonymous, POST-only): authoritative SSURGO map
    units, components and horizon properties over the bbox, plus the dominant
    soil component at each configured station point. Output: JSON.

Failure policy: POLARIS is REQUIRED (empty output + exit non-zero on
persistent failure). SDA is best-effort within the job — POLARIS carries the
gridded covariates; SDA adds tabular provenance/cross-check, so its failure
logs an ERROR and leaves an empty JSON without failing the job.
"""

import argparse
import json
import logging
import sys
import time

import numpy as np
import rasterio
import requests
from rasterio.windows import from_bounds

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("fetch_soil")

RETRIES = 4
BACKOFF_S = 5

# SPEC.md section 10 gotcha: these POLARIS variables ship log10-transformed.
LOG10_VARIABLES = {"ksat", "alpha", "om"}

POLARIS_UNITS = {
    "theta_s": "m3/m3", "theta_r": "m3/m3", "alpha": "1/kPa", "n": "-",
    "ksat": "cm/hr", "clay": "percent", "sand": "percent", "silt": "percent",
    "bd": "g/cm3", "om": "percent", "ph": "-",
}

SDA_TIMEOUT_S = 120


def polaris_tile_name(bbox):
    """POLARIS v1.0 tile naming, e.g. lat4041_lon-105-104.tif."""
    la = int(np.floor(bbox["min_lat"]))
    lo = int(np.floor(bbox["min_lon"]))
    return f"lat{la}{la + 1}_lon{lo}{lo + 1}.tif"


def fetch_polaris(config, output):
    """Windowed reads of every variable x depth band; write one multiband tif."""
    soil_cfg = config["static_layers"]["soil"]
    base = soil_cfg["polaris_base"]
    variables = soil_cfg["polaris_variables"]
    depths = soil_cfg["polaris_depths"]
    stat = soil_cfg.get("polaris_stat", "mean")
    bbox = config["bbox"]
    tile = polaris_tile_name(bbox)
    bounds = (bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["max_lat"])

    bands, names, profile = [], [], None
    for var in variables:
        for depth in depths:
            url = f"{base}/{var}/{stat}/{depth}/{tile}"
            last = None
            for attempt in range(1, RETRIES + 1):
                try:
                    with rasterio.open(f"/vsicurl/{url}") as src:
                        window = from_bounds(*bounds, transform=src.transform)
                        data = src.read(1, window=window, boundless=True,
                                        fill_value=np.nan).astype("float32")
                        nodata = src.nodata
                        if profile is None:
                            profile = {
                                "crs": src.crs,
                                "transform": src.window_transform(window),
                                "height": data.shape[0],
                                "width": data.shape[1],
                            }
                    if nodata is not None:
                        data[data == nodata] = np.nan
                    if var in LOG10_VARIABLES:
                        data = np.power(10.0, data).astype("float32")
                    bands.append(data)
                    names.append(f"{var}_{depth}")
                    logger.info("POLARIS %s/%s/%s ok", var, stat, depth)
                    break
                except Exception as exc:
                    last = exc
                    logger.warning("POLARIS %s attempt %d/%d failed: %s",
                                   f"{var}/{depth}", attempt, RETRIES, exc)
                    if attempt < RETRIES:
                        time.sleep(BACKOFF_S * attempt)
            else:
                raise RuntimeError(
                    f"POLARIS {var}/{depth} failed after {RETRIES} attempts: {last}")

    stack = np.stack(bands)
    with rasterio.open(output, "w", driver="GTiff", dtype="float32",
                       count=len(bands), height=profile["height"],
                       width=profile["width"], crs=profile["crs"],
                       transform=profile["transform"], nodata=np.nan,
                       compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(stack)
        for i, name in enumerate(names, start=1):
            dst.set_band_description(i, name)
        dst.update_tags(
            source_url=base, source_product=f"POLARIS v1.0 {stat}",
            fetched_utc=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            log10_untransformed=json.dumps(sorted(LOG10_VARIABLES)),
            units=json.dumps(POLARIS_UNITS),
        )
    return len(bands)


def sda_query(endpoint, query):
    """POST one tabular query to Soil Data Access; return rows as dicts."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(
                endpoint, json={"format": "JSON+COLUMNNAME", "query": query},
                timeout=SDA_TIMEOUT_S)
            resp.raise_for_status()
            table = resp.json().get("Table", [])
            if not table:
                return []
            header, *rows = table
            return [dict(zip(header, r)) for r in rows]
        except requests.RequestException as exc:
            last = exc
            logger.warning("SDA attempt %d/%d failed: %s", attempt, RETRIES, exc)
            if attempt < RETRIES:
                time.sleep(BACKOFF_S * attempt)
    raise RuntimeError(f"SDA query failed after {RETRIES} attempts: {last}")


def fetch_sda(config):
    """Map units, components + horizons over the bbox; dominant soil per station."""
    endpoint = config["static_layers"]["soil"]["sda_endpoint"]
    bbox = config["bbox"]
    wkt = (f"POLYGON(({bbox['min_lon']} {bbox['min_lat']},"
           f"{bbox['max_lon']} {bbox['min_lat']},"
           f"{bbox['max_lon']} {bbox['max_lat']},"
           f"{bbox['min_lon']} {bbox['max_lat']},"
           f"{bbox['min_lon']} {bbox['min_lat']}))")

    mukeys = sda_query(endpoint, (
        "SELECT DISTINCT mukey "
        f"FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt}')"))
    keys = ",".join(str(r["mukey"]) for r in mukeys)
    if not keys:
        raise RuntimeError("SDA returned no map units for the bbox")

    horizons = sda_query(endpoint, (
        "SELECT mu.mukey, mu.muname, c.cokey, c.compname, c.comppct_r, "
        "c.taxclname, ch.hzname, ch.hzdept_r, ch.hzdepb_r, "
        "ch.sandtotal_r, ch.silttotal_r, ch.claytotal_r, ch.awc_r, "
        "ch.wthirdbar_r, ch.wfifteenbar_r, ch.dbthirdbar_r, ch.ksat_r, ch.om_r "
        "FROM mapunit mu "
        "JOIN component c ON c.mukey = mu.mukey "
        "LEFT JOIN chorizon ch ON ch.cokey = c.cokey "
        f"WHERE mu.mukey IN ({keys}) AND c.majcompflag = 'Yes' "
        "ORDER BY mu.mukey, c.comppct_r DESC, ch.hzdept_r"))

    stations = {}
    for st in config.get("stations", []):
        if st.get("lat") is None or st.get("lon") is None:
            continue
        rows = sda_query(endpoint, (
            "SELECT DISTINCT mu.mukey, mu.muname, c.compname, c.comppct_r "
            "FROM SDA_Get_Mukey_from_intersection_with_WktWgs84("
            f"'POINT({st['lon']} {st['lat']})') k "
            "JOIN mapunit mu ON mu.mukey = k.mukey "
            "JOIN component c ON c.mukey = mu.mukey "
            "ORDER BY c.comppct_r DESC"))
        stations[st["id"]] = rows[0] if rows else None

    return {
        "provenance": {
            "source": "USDA Soil Data Access (SSURGO)",
            "endpoint": endpoint,
            "fetched_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "bbox_wkt": wkt,
        },
        "n_mapunits": len(mukeys),
        "horizons": horizons,
        "stations": stations,
    }


def main():
    ap = argparse.ArgumentParser(description="Fetch POLARIS + SDA soil data")
    ap.add_argument("--config", required=True, help="site_config.json")
    ap.add_argument("--output-polaris", required=True,
                    help="Output multiband POLARIS GeoTIFF")
    ap.add_argument("--output-sda", required=True, help="Output SDA JSON")
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)

    # SDA (best-effort) first so a POLARIS hard-fail still leaves both
    # declared outputs on disk before the non-zero exit.
    try:
        sda = fetch_sda(config)
        logger.info("SDA: %d map units, %d horizon rows, %d station lookups",
                    sda["n_mapunits"], len(sda["horizons"]), len(sda["stations"]))
    except Exception as exc:
        sda = {"error": str(exc)}
        logger.error("SDA unavailable; continuing with POLARIS only "
                     "(SDA is the tabular cross-check, not the gridded "
                     "source): %s", exc)
    with open(args.output_sda, "w") as fh:
        json.dump(sda, fh, indent=2)

    try:
        n = fetch_polaris(config, args.output_polaris)
        logger.info("Wrote %d POLARIS bands -> %s", n, args.output_polaris)
    except Exception as exc:
        # REQUIRED source: write the declared output, then fail loudly.
        open(args.output_polaris, "wb").close()
        logger.error("POLARIS unavailable (%s); wrote empty %s and failing "
                     "the static branch.", exc, args.output_polaris)
        sys.exit(1)


if __name__ == "__main__":
    main()
