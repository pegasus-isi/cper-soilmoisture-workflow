#!/usr/bin/env python3
"""Figures and a self-contained web page for the dynamic map (M4).

"Being able to visualize this would be great" (the researcher's email) - this is that
deliverable for the live map, as opposed to visualize_response.py which covers
the historical characterisation.

Two products:

  **soil_moisture_map.png** — six panels: the current estimate, its uncertainty,
  the response zones with station-free areas hatched, per-zone recent time
  series, station current-vs-climatology, and a summary block carrying the
  leave-one-station-out skill numbers and the tier that was actually used.

  **soil_moisture_map.html** — one file, no network access, openable from disk
  or droppable on any web host. Zone polygons are inlined as SVG paths, station
  markers carry click-through popups with their M3 fingerprint, and the PNG
  panels are embedded as base64. A field researcher gets something that looks
  like it "updates" without a server, a tile provider, or a JS bundle.

Every product prints the as-of date, the tier, and the covariate/zone build
stamps it was made from, so a map on someone's screen can always be traced back
to the static baseline it came from (C9).
"""

import argparse
import base64
import json
import logging
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import rasterio  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, ListedColormap  # noqa: E402
from rasterio.warp import transform_bounds  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("visualize_soil_moisture")

ZONE_PALETTE = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
                "#937860", "#DA8BC3", "#8C8C8C"]
# Dry -> wet, colour-blind safe and printable in greyscale.
SM_COLORS = ["#8C510A", "#D8B365", "#F6E8C3", "#C7EAE5", "#5AB4AC", "#01665E"]


def read_band(path):
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True).filled(np.nan)
        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        return arr, bounds, dict(src.tags())


def _extent(bounds):
    """(left, right, bottom, top) in lon/lat for imshow."""
    return (bounds[0], bounds[2], bounds[1], bounds[3])


def panel_map(ax, arr, bounds, stations, thresholds, labels):
    """Current estimate on a continuous scale, with class breaks as ticks.

    A hard class-binned image would be one flat colour whenever the whole site
    sits inside one dryness class — which is exactly what a dry July looks like
    here. The continuous scale keeps the soil-driven texture visible; the class
    breakpoints stay legible as colourbar ticks, so the decision-support
    reading is not lost.
    """
    cmap = LinearSegmentedColormap.from_list("sm", SM_COLORS)
    lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
    if not np.isfinite(lo) or hi - lo < 1e-9:
        lo, hi = lo - 0.01, lo + 0.01
    im = ax.imshow(arr, extent=_extent(bounds), origin="upper", cmap=cmap,
                   vmin=lo, vmax=hi, interpolation="nearest")
    if stations:
        ax.scatter([s["lon"] for s in stations], [s["lat"] for s in stations],
                   c="black", s=26, marker="o", edgecolor="white", lw=1.0,
                   zorder=5, label="reporting station")
        ax.legend(fontsize=6, frameon=False, loc="lower left")
    cb = plt.colorbar(im, ax=ax, fraction=0.046,
                      label="$\\theta$ (m$^3$/m$^3$)")
    inside = [t for t in thresholds if lo < t < hi]
    for t in inside:
        cb.ax.axhline(t, color="black", lw=1.0)
    band = next((lab for lab, t in zip(labels, list(thresholds) + [np.inf])
                 if hi <= t), labels[-1] if labels else "?")
    ax.set_title("Current soil moisture (0-15 cm)"
                 + ("" if inside else f"\nentire site in class '{band}'"))
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")


def panel_uncertainty(ax, arr, bounds, stations):
    im = ax.imshow(arr, extent=_extent(bounds), origin="upper",
                   cmap="magma_r", interpolation="nearest")
    if stations:
        ax.scatter([s["lon"] for s in stations], [s["lat"] for s in stations],
                   c="cyan", s=22, edgecolor="black", lw=0.6, zorder=5)
    plt.colorbar(im, ax=ax, fraction=0.046,
                 label="1$\\sigma$ (m$^3$/m$^3$)")
    ax.set_title("Uncertainty\n(spread + distance + station-free penalty)")
    ax.set_xlabel("longitude")


def panel_zones(ax, zones, bounds, stations, station_free):
    k = int(np.nanmax(zones)) + 1 if np.isfinite(np.nanmax(zones)) else 0
    cmap = ListedColormap(ZONE_PALETTE[:max(k, 1)])
    ax.imshow(np.where(zones < 0, np.nan, zones), extent=_extent(bounds),
              origin="upper", cmap=cmap, vmin=-0.5, vmax=max(k - 0.5, 0.5),
              interpolation="nearest")
    # Station-free zones are pure extrapolation and must not look like the
    # rest of the map (C5).
    if station_free:
        mask = np.isin(zones, list(station_free)).astype(float)
        mask[mask == 0] = np.nan
        ax.contourf(mask, levels=[0.5, 1.5], colors="none", hatches=["///"],
                    extent=_extent(bounds), origin="upper")
    if stations:
        ax.scatter([s["lon"] for s in stations], [s["lat"] for s in stations],
                   c="black", s=26, edgecolor="white", lw=1.0, zorder=5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=ZONE_PALETTE[g % len(ZONE_PALETTE)])
               for g in range(k)]
    ax.legend(handles, [f"zone {g}" + (" (no station)" if g in station_free
                                       else "") for g in range(k)],
              fontsize=6, frameon=False, loc="lower left")
    ax.set_title("Response zones (hatched = no station)")
    ax.set_xlabel("longitude")


def panel_zone_series(ax, points, now, days=120):
    """Recent daily series averaged over each zone's member stations."""
    series = points.get("daily_series") or {}
    surface_max = points.get("surface_depth_max_cm", 20)
    station_zone = {s["station"]: s["zone"] for s in now.get("stations", [])}
    depth_of = {p["node"]: p.get("depth_cm") for p in points.get("points", [])}
    station_of = {p["node"]: p.get("station") for p in points.get("points", [])}

    by_zone = {}
    for node, obs in series.items():
        d = depth_of.get(node)
        if d is not None and d > surface_max:
            continue
        z = station_zone.get(station_of.get(node))
        if z is None:
            continue
        s = pd.Series({pd.Timestamp(k): v for k, v in obs.items()})
        by_zone.setdefault(z, []).append(s)
    if not by_zone:
        ax.text(0.5, 0.5, "no zone time series available", ha="center",
                va="center", fontsize=7.5, color="#666")
        ax.set_axis_off()
        return
    as_of = pd.Timestamp(now.get("provenance", {}).get("as_of")
                         or points.get("as_of"))
    lo = as_of - pd.Timedelta(days=days)
    for z in sorted(by_zone):
        m = pd.concat(by_zone[z], axis=1, sort=True).mean(axis=1).sort_index()
        m = m[(m.index >= lo) & (m.index <= as_of)]
        if m.empty:
            continue
        ax.plot(m.index, m.values, lw=1.6, label=f"zone {z} (n={len(by_zone[z])})",
                color=ZONE_PALETTE[int(z) % len(ZONE_PALETTE)])
    ax.set_title(f"Surface soil moisture by zone, last {days} d")
    ax.set_ylabel("$\\theta$ (m$^3$/m$^3$)")
    ax.legend(fontsize=6, frameon=False)
    ax.tick_params(axis="x", labelrotation=30, labelsize=6)
    ax.grid(alpha=0.3)


def panel_station_anomaly(ax, now):
    """Current vs the month's climatology, per station."""
    st = sorted(now.get("stations", []), key=lambda s: s["current"])
    if not st:
        ax.set_axis_off()
        return
    y = np.arange(len(st))
    for i, s in enumerate(st):
        ax.plot([s["climatology_month"], s["current"]], [i, i], color="#bbb",
                lw=2, zorder=1)
    ax.scatter([s["climatology_month"] for s in st], y, color="#8C8C8C", s=28,
               label="climatology (this month)", zorder=2)
    ax.scatter([s["current"] for s in st], y,
               color=["#C44E52" if s["anomaly"] < 0 else "#4C72B0" for s in st],
               s=34, label="current", zorder=3)
    ax.set_yticks(y)
    ax.set_yticklabels([s["station"] for s in st], fontsize=6)
    ax.set_xlabel("$\\theta$ (m$^3$/m$^3$)")
    ax.set_title("Current vs climatology")
    ax.legend(fontsize=6, frameon=False, loc="lower right")
    ax.grid(alpha=0.3, axis="x")


def summary_lines(now, skill):
    prov = now.get("provenance", {})
    summ = now.get("summary", {})
    overall = skill.get("overall") or {}
    base = skill.get("baseline_site_mean") or {}
    clim = skill.get("baseline_climatology") or {}
    lines = [
        f"as of: {prov.get('as_of')}",
        f"tier used: {prov.get('tier_used')}   "
        f"zones (k): {prov.get('zone_k')}",
        f"reporting stations: {now.get('n_reporting_stations')} "
        f"({now.get('n_distinct_locations')} locations)",
        f"site mean: {summ.get('mean')} m3/m3   "
        f"mean 1-sigma: {summ.get('mean_uncertainty')}",
        f"station-free area: {summ.get('area_ha_station_free')} ha",
        "",
        "leave-one-station-out:",
        f"  zone-anchored RMSE   {overall.get('rmse')}",
        f"  site-mean baseline   {base.get('rmse')}",
        f"  climatology baseline {clim.get('rmse')}",
        f"  -> {skill.get('verdict', skill.get('skipped', 'n/a'))}",
        "",
        f"covariates built {prov.get('covariate_manifest_built_utc')}",
        f"zones built {prov.get('zones_built_utc')}",
    ]
    return lines


# --------------------------------------------------------------------------
# self-contained HTML
# --------------------------------------------------------------------------

def _svg_paths(geojson, bounds, w, h, min_area_ha):
    """Zone polygons as SVG paths in a lon/lat-linear viewBox."""
    lon0, lat0, lon1, lat1 = bounds[0], bounds[1], bounds[2], bounds[3]
    sx = w / max(lon1 - lon0, 1e-9)
    sy = h / max(lat1 - lat0, 1e-9)

    def project(lon, lat):
        return (lon - lon0) * sx, (lat1 - lat) * sy

    paths = []
    for f in geojson.get("features", []):
        props = f.get("properties", {})
        # Only the map-usable polygons: the sliver layer would add megabytes of
        # single-pixel outlines that no one can see anyway.
        if props.get("below_min_area") or props.get("area_ha", 0) < min_area_ha:
            continue
        geom = f.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        d = []
        for ring in geom["coordinates"]:
            pts = [project(x, y) for x, y in ring]
            d.append("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z")
        paths.append((int(props.get("zone", 0)), " ".join(d),
                      props.get("area_ha")))
    return paths, project


def build_html(now, skill, zone_stats, geojson, fingerprints, png_paths,
               title="CPER dynamic soil-moisture map"):
    """One file, no external requests. SVG zones + station popups + PNGs."""
    bounds = (now["grid"].get("lon_min"), now["grid"].get("lat_min"),
              now["grid"].get("lon_max"), now["grid"].get("lat_max"))
    if None in bounds:
        lons = [s["lon"] for s in now.get("stations", [])] or [-104.8, -104.68]
        lats = [s["lat"] for s in now.get("stations", [])] or [40.75, 40.88]
        pad = 0.02
        bounds = (min(lons) - pad, min(lats) - pad,
                  max(lons) + pad, max(lats) + pad)

    W, H = 720, 780
    min_area = (zone_stats.get("polygons", {}) or {}).get("min_polygon_ha", 1.0)
    paths, project = _svg_paths(geojson, bounds, W, H, min_area)
    station_free = set(zone_stats.get("station_free_zones") or [])

    fp_by_station = {}
    for fp in fingerprints:
        for node in fp.get("nodes", []):
            st = str(node.get("node", "")).split("@")[0]
            if node.get("depth_cm") is not None and node["depth_cm"] <= 20:
                fp_by_station.setdefault(st, node)

    svg = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="auto" '
           'role="img" aria-label="response zones with stations">']
    for zone, d, area in paths:
        color = ZONE_PALETTE[zone % len(ZONE_PALETTE)]
        hatch = ' class="freezone"' if zone in station_free else ""
        svg.append(f'<path d="{d}" fill="{color}" fill-opacity="0.55" '
                   f'stroke="{color}" stroke-width="0.6"{hatch}>'
                   f'<title>zone {zone} — {area} ha'
                   f'{" (no station)" if zone in station_free else ""}</title>'
                   '</path>')
    for s in now.get("stations", []):
        x, y = project(s["lon"], s["lat"])
        fp = fp_by_station.get(s["station"], {})
        payload = {
            "station": s["station"], "zone": s["zone"],
            "current": s["current"], "climatology": s["climatology_month"],
            "anomaly": s["anomaly"], "theta_s": s["theta_s"],
            "surface_nodes": s["n_surface_nodes"],
            "age_days": s["age_days"],
            "drydown_tau_days": fp.get("drydown_tau_days"),
            "memory_efolding_days": fp.get("memory_efolding_days"),
            "plant_available_range": fp.get("plant_available_range"),
            "event_response_fraction": fp.get("event_response_fraction"),
        }
        svg.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" class="stn" '
            f'data-info=\'{json.dumps(payload)}\' tabindex="0">'
            f'<title>{s["station"]}</title></circle>')
    svg.append("</svg>")

    imgs = []
    for label, path in png_paths:
        try:
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            imgs.append(f'<figure><img alt="{label}" '
                        f'src="data:image/png;base64,{b64}">'
                        f'<figcaption>{label}</figcaption></figure>')
        except Exception as exc:
            logger.warning("could not embed %s: %s", path, exc)

    prov = now.get("provenance", {})
    zone_rows = "".join(
        f"<tr><td>{z['zone']}</td><td>{z['area_ha']}</td>"
        f"<td>{z['n_stations']}</td>"
        f"<td>{'yes' if z['station_free'] else 'no'}</td>"
        f"<td>{z['mean_estimate']}</td><td>{z['mean_uncertainty']}</td>"
        f"<td>{z.get('analogue_donor_zone') if z['station_free'] else ''}</td>"
        "</tr>" for z in now.get("zones", []))
    stat_rows = "".join(
        f"<tr><td>{s['station']}</td><td>{s['zone']}</td>"
        f"<td>{s['current']}</td><td>{s['climatology_month']}</td>"
        f"<td>{s['anomaly']}</td><td>{s['age_days']}</td></tr>"
        for s in now.get("stations", []))
    skill_rows = "".join(
        f"<tr><td>{r['station']}</td><td>{r['observed']}</td>"
        f"<td>{r['predicted']}</td><td>{r['error']}</td>"
        f"<td>{r['source']}</td></tr>"
        for r in (skill.get("per_station") or []))

    overall = skill.get("overall") or {}
    base = skill.get("baseline_site_mean") or {}
    climb = skill.get("baseline_climatology") or {}

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {prov.get('as_of')}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 system-ui, -apple-system, sans-serif; margin: 0;
        padding: 1.5rem; max-width: 1100px; margin-inline: auto; }}
 h1 {{ font-size: 1.4rem; margin-bottom: .2rem; }}
 .sub {{ color: #777; margin-top: 0; }}
 .grid {{ display: grid; gap: 1.5rem; grid-template-columns: minmax(0,1fr); }}
 @media (min-width: 900px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px;
         display: block; overflow-x: auto; }}
 th, td {{ border-bottom: 1px solid #8884; padding: .3rem .5rem;
          text-align: right; white-space: nowrap; }}
 th:first-child, td:first-child {{ text-align: left; }}
 figure {{ margin: 0; }} img {{ max-width: 100%; height: auto; }}
 figcaption {{ font-size: 12px; color: #777; }}
 .stn {{ fill: #111; stroke: #fff; stroke-width: 2; cursor: pointer; }}
 .stn:hover, .stn:focus {{ fill: #C44E52; outline: none; }}
 .freezone {{ fill-opacity: .2; stroke-dasharray: 4 3; }}
 #popup {{ position: sticky; bottom: 0; background: Canvas;
          border: 1px solid #8886; border-radius: 6px; padding: .6rem .8rem;
          font-size: 13px; margin-top: .5rem; }}
 .warn {{ background: #DD84521f; border-left: 3px solid #DD8452;
         padding: .6rem .8rem; border-radius: 4px; }}
 code {{ font-size: 12px; }}
</style></head><body>
<h1>{title}</h1>
<p class="sub">as of <strong>{prov.get('as_of')}</strong> ·
 tier {prov.get('tier_used')} · {now.get('n_reporting_stations')} reporting
 stations at {now.get('n_distinct_locations')} locations ·
 page built {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}</p>

<p class="warn"><strong>Read the uncertainty layer.</strong>
 {now.get('summary', {}).get('area_ha_station_free')} ha of the site sits in a
 zone with no station and is estimated from its nearest analogue in covariate
 space, not from a measurement. Leave-one-station-out RMSE is
 {overall.get('rmse')} m³/m³ against a site-mean baseline of
 {base.get('rmse')} and a climatology-only baseline of {climb.get('rmse')} —
 {skill.get('verdict', skill.get('skipped', 'not assessed'))}.</p>

<div class="grid">
 <section><h2>Zones and stations</h2>{''.join(svg)}
  <div id="popup">Click or tab to a station marker for its response
   fingerprint.</div></section>
 <section><h2>Panels</h2>{''.join(imgs)}</section>
</div>

<h2>Zones</h2>
<table><thead><tr><th>zone</th><th>area (ha)</th><th>stations</th>
 <th>station-free</th><th>mean θ</th><th>mean 1σ</th><th>analogue donor</th>
 </tr></thead><tbody>{zone_rows}</tbody></table>

<h2>Stations</h2>
<table><thead><tr><th>station</th><th>zone</th><th>current θ</th>
 <th>climatology</th><th>anomaly</th><th>age (d)</th></tr></thead>
 <tbody>{stat_rows}</tbody></table>

<h2>Leave-one-station-out</h2>
<table><thead><tr><th>held out</th><th>observed</th><th>predicted</th>
 <th>error</th><th>estimated from</th></tr></thead>
 <tbody>{skill_rows}</tbody></table>
<p class="sub">{skill.get('caveat', '')}</p>

<h2>Provenance</h2>
<p><code>covariates built {prov.get('covariate_manifest_built_utc')} ·
 zones built {prov.get('zones_built_utc')} (k={prov.get('zone_k')}) ·
 map built {prov.get('built_utc')}</code></p>

<script>
const popup = document.getElementById('popup');
const fmt = v => (v === null || v === undefined) ? '—' : v;
for (const el of document.querySelectorAll('.stn')) {{
  const show = () => {{
    const d = JSON.parse(el.dataset.info);
    popup.innerHTML = `<strong>${{d.station}}</strong> — zone ${{fmt(d.zone)}}
      · ${{fmt(d.surface_nodes)}} surface node(s), ${{fmt(d.age_days)}} d old<br>
      current <strong>${{fmt(d.current)}}</strong> vs climatology
      ${{fmt(d.climatology)}} (anomaly ${{fmt(d.anomaly)}}) ·
      θ<sub>s</sub> ${{fmt(d.theta_s)}}<br>
      dry-down τ ${{fmt(d.drydown_tau_days)}} d · memory
      ${{fmt(d.memory_efolding_days)}} d · plant-available range
      ${{fmt(d.plant_available_range)}} · event response
      ${{fmt(d.event_response_fraction)}}`;
  }};
  el.addEventListener('click', show);
  el.addEventListener('focus', show);
}}
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(
        description="Figures + self-contained HTML for the M4 dynamic map")
    ap.add_argument("--map", required=True, help="soil_moisture_now.tif")
    ap.add_argument("--uncertainty", required=True,
                    help="soil_moisture_uncertainty.tif")
    ap.add_argument("--zones", required=True, help="zones.tif")
    ap.add_argument("--geojson", required=True,
                    help="soil_moisture_zones.geojson")
    ap.add_argument("--zone-stats", required=True, help="zone_stats.json")
    ap.add_argument("--now-json", required=True, help="soil_moisture_now.json")
    ap.add_argument("--skill", required=True, help="estimation_skill.json")
    ap.add_argument("--points", help="soil_moisture_points.json (time series)")
    ap.add_argument("--fingerprints", nargs="*", default=[],
                    help="response_*.json for the station popups")
    ap.add_argument("--output-figure", required=True, help="Output PNG")
    ap.add_argument("--output-html", required=True, help="Output HTML page")
    args = ap.parse_args()

    try:
        run(args)
    except Exception as exc:
        logger.error("visualize_soil_moisture failed: %s", exc)
        try:
            fig = plt.figure(figsize=(6, 2))
            fig.text(0.5, 0.5, f"map visualisation failed:\n{exc}",
                     ha="center", va="center", fontsize=8)
            fig.savefig(args.output_figure, dpi=100)
            plt.close(fig)
        except Exception:
            open(args.output_figure, "a").close()
        with open(args.output_html, "w") as fh:
            fh.write("<!DOCTYPE html><html><body><h1>Map visualisation "
                     f"failed</h1><pre>{exc}</pre></body></html>")


def run(args):
    theta, bounds, _ = read_band(args.map)
    unc, _, _ = read_band(args.uncertainty)
    with rasterio.open(args.zones) as zsrc:
        zones = zsrc.read(1)

    with open(args.now_json) as fh:
        now = json.load(fh)
    with open(args.skill) as fh:
        skill = json.load(fh)
    with open(args.zone_stats) as fh:
        zone_stats = json.load(fh)
    with open(args.geojson) as fh:
        geojson = json.load(fh)
    points = {}
    if args.points:
        try:
            with open(args.points) as fh:
                points = json.load(fh)
        except Exception as exc:
            logger.warning("no points layer for time series: %s", exc)
    fingerprints = []
    for path in args.fingerprints:
        try:
            with open(path) as fh:
                fingerprints.append(json.load(fh))
        except Exception as exc:
            logger.warning("could not read %s: %s", path, exc)

    # Record the geographic bounds so the HTML can place the SVG without
    # re-opening the rasters.
    now.setdefault("grid", {}).update(
        {"lon_min": bounds[0], "lat_min": bounds[1],
         "lon_max": bounds[2], "lat_max": bounds[3]})

    stations = now.get("stations", [])
    station_free = set(zone_stats.get("station_free_zones") or [])

    fig, axes = plt.subplots(2, 3, figsize=(16, 10))
    fig.suptitle(f"CPER dynamic soil-moisture map (M4) — "
                 f"as of {now.get('provenance', {}).get('as_of')}", fontsize=13)
    panel_map(axes[0, 0], theta, bounds, stations, now.get("thresholds", []),
              now.get("classes", []))
    panel_uncertainty(axes[0, 1], unc, bounds, stations)
    panel_zones(axes[0, 2], zones, bounds, stations, station_free)
    panel_zone_series(axes[1, 0], points, now)
    panel_station_anomaly(axes[1, 1], now)
    ax = axes[1, 2]
    ax.set_axis_off()
    ax.text(0.02, 0.98, "\n".join(summary_lines(now, skill)), va="top",
            fontsize=7.5, family="monospace")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(args.output_figure, dpi=150)
    plt.close(fig)
    logger.info("Wrote %s", args.output_figure)

    html = build_html(now, skill, zone_stats, geojson, fingerprints,
                      [("current estimate, uncertainty, zones and skill",
                        args.output_figure)])
    with open(args.output_html, "w") as fh:
        fh.write(html)
    logger.info("Wrote %s (%.1f KB, self-contained)", args.output_html,
                len(html) / 1024.0)


if __name__ == "__main__":
    main()
