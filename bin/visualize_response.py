#!/usr/bin/env python3
"""Figures for the M3 station characterisation.

"Being able to visualize this would be great" (the researcher's email) - this is that
deliverable for ask 1. Five panels, written as one PNG plus a small index
JSON so the figure set is self-describing:

  1. Depth profile of response  — tau and event response vs depth, one line
     per station/plot. The clearest single view of "how does this location
     behave", and it reads correctly even with few stations.
  2. Station map by group       — node locations coloured by behavioural
     group, sized by depth.
  3. Metric vs covariate        — the strongest attributed relationship,
     scatter with Spearman rho annotated.
  4. Driver importances         — top covariate per response metric, taken
     from the *depth-band* attribution rather than the pooled fit. Pooled,
     depth outranks every soil and terrain covariate by construction, so the
     pooled panel says nothing; the banded one answers "at comparable depth,
     what separates these locations".
  5. Group metric profiles      — standardised metric means per behavioural
     group, so "what makes group 2 different" is legible.

Matplotlib Agg only (no seaborn/cartopy): the container stays lean and the
job needs no display or basemap download.
"""

import argparse
import json
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("visualize_response")

PALETTE = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
           "#937860", "#DA8BC3", "#8C8C8C"]


def _station_of(node):
    return str(node).split("@")[0]


def panel_depth_profile(ax, groups):
    """tau vs depth, one line per station/plot — the core 'behaviour' view."""
    df = groups.dropna(subset=["depth_cm"])
    if df.empty or "drydown_tau_days" not in df:
        ax.set_visible(False)
        return
    for i, (st, sub) in enumerate(df.groupby(df["node"].map(_station_of))):
        sub = sub.sort_values("depth_cm")
        ax.plot(sub["drydown_tau_days"], sub["depth_cm"], "o-",
                color=PALETTE[i % len(PALETTE)], label=st, lw=1.8, ms=5)
    ax.invert_yaxis()
    ax.set_xlabel("dry-down $\\tau$ (days)")
    ax.set_ylabel("depth (cm)")
    ax.set_title("Dry-down response by depth")
    ax.legend(fontsize=6, frameon=False)
    ax.grid(alpha=0.3)


def panel_map(ax, groups):
    """Node locations coloured by behavioural group."""
    df = groups.dropna(subset=["lat", "lon"])
    if df.empty:
        ax.set_visible(False)
        return
    for g, sub in df.groupby("group"):
        ax.scatter(sub["lon"], sub["lat"],
                   s=20 + 0.6 * sub["depth_cm"].fillna(10),
                   color=PALETTE[int(g) % len(PALETTE)] if g >= 0 else "#999",
                   label=f"group {g}" if g >= 0 else "ungrouped",
                   alpha=0.8, edgecolor="white", lw=0.5)
    for st, sub in df.groupby(df["node"].map(_station_of)):
        ax.annotate(st, (sub["lon"].iloc[0], sub["lat"].iloc[0]),
                    fontsize=5, xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title("Stations by behavioural group\n(marker size = depth)")
    ax.legend(fontsize=6, frameon=False)
    ax.grid(alpha=0.3)


def _best_relationship(similarity):
    """Pick the metric/covariate pair with the strongest |rho| worth plotting."""
    best = None
    for scope in ("attribution_by_depth_band", "attribution"):
        block = similarity.get(scope) or {}
        candidates = ({"pooled": block} if scope == "attribution"
                      else block)
        for band, res in candidates.items():
            for metric, info in (res or {}).get("metrics", {}).items():
                for d in info.get("top_drivers", []):
                    rho = d.get("spearman")
                    if rho is None or d["covariate"] == "depth_cm":
                        continue
                    if best is None or abs(rho) > abs(best["spearman"]):
                        best = {"metric": metric, "covariate": d["covariate"],
                                "spearman": rho, "band": band}
    return best


def panel_scatter(ax, groups, covariates, similarity):
    """Strongest attributed metric-covariate relationship."""
    rel = _best_relationship(similarity)
    # A covariate that takes only 2 distinct values across the network will
    # correlate with almost anything. Refuse to draw a "driver" scatter that
    # cannot be evidence, rather than publishing a persuasive coincidence.
    n_loc = similarity.get("n_distinct_locations") or 0
    if rel is None or covariates is None or covariates.empty or n_loc < 3:
        ax.text(0.5, 0.5,
                "driver scatter withheld\n"
                f"only {n_loc} distinct station location(s)\n"
                "(needs >= 3 to be evidence)",
                ha="center", va="center", fontsize=7.5, color="#666")
        ax.set_axis_off()
        return
    cov = covariates.set_index("id")
    keys = sorted(cov.index.astype(str), key=len, reverse=True)
    xs, ys, cs = [], [], []
    for _, row in groups.iterrows():
        k = next((k for k in keys if str(row["node"]).startswith(k)), None)
        if k is None or rel["covariate"] not in cov.columns:
            continue
        x, y = cov.loc[k, rel["covariate"]], row.get(rel["metric"])
        if pd.notna(x) and pd.notna(y):
            xs.append(float(x))
            ys.append(float(y))
            cs.append(PALETTE[int(row["group"]) % len(PALETTE)]
                      if row["group"] >= 0 else "#999")
    if not xs:
        ax.set_visible(False)
        return
    ax.scatter(xs, ys, c=cs, s=28, alpha=0.85, edgecolor="white", lw=0.5)
    ax.set_xlabel(rel["covariate"])
    ax.set_ylabel(rel["metric"])
    ax.set_title(f"Strongest driver ({rel['band']})\n"
                 f"Spearman $\\rho$ = {rel['spearman']:.2f}")
    ax.grid(alpha=0.3)


def _band_importances(similarity):
    """Top non-depth driver per (depth band, response metric).

    The pooled attribution is *not* what to plot: within a single soil profile
    depth dominates every response metric (verified 0.78-0.91 importance on the
    real CPER network), so a pooled panel reads "<- depth_cm" for every metric
    and hides the question the researcher asked. The depth-band fits answer it:
    at comparable depth, which soil and terrain properties separate one
    location from another.
    """
    rows = []
    for band, res in (similarity.get("attribution_by_depth_band") or {}).items():
        if not isinstance(res, dict) or res.get("skipped"):
            continue
        for metric, info in (res.get("metrics") or {}).items():
            top = next((d for d in (info.get("top_drivers") or [])
                        if d.get("covariate") != "depth_cm"), None)
            if top:
                rows.append((metric, top["covariate"], top["importance"],
                             band, info.get("n")))
    return rows


def _pooled_importances(similarity):
    """Fallback when no depth band had enough nodes to fit."""
    attr = (similarity.get("attribution") or {}).get("metrics", {})
    return [(m, d["covariate"], d["importance"], "pooled", info.get("n"))
            for m, info in attr.items()
            for d in (info.get("top_drivers") or [])[:1]]


def panel_importances(ax, similarity):
    """Dominant driver per response metric, computed within depth bands."""
    rows, pooled = _band_importances(similarity), False
    if not rows:
        rows, pooled = _pooled_importances(similarity), True
    if not rows:
        ax.set_visible(False)
        return

    bands = sorted({r[3] for r in rows})
    colors = {b: PALETTE[i % len(PALETTE)] for i, b in enumerate(bands)}
    rows = sorted(rows, key=lambda r: -r[2])[:10]
    y = np.arange(len(rows))
    ax.barh(y, [r[2] for r in rows],
            color=[colors[r[3]] for r in rows], alpha=0.85)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{r[0]}\n← {r[1]}" for r in rows], fontsize=5)
    ax.invert_yaxis()
    ax.set_xlabel("random-forest importance")
    if pooled:
        ax.set_title("Dominant driver per response metric\n"
                     "(pooled — depth bands had too few nodes)")
    else:
        ax.set_title("Dominant driver per response metric\n"
                     "(within depth band, depth excluded)")
    handles = [plt.Rectangle((0, 0), 1, 1, color=colors[b], alpha=0.85)
               for b in bands]
    n_by_band = {r[3]: r[4] for r in rows}
    ax.legend(handles, [f"{b} (n={n_by_band.get(b, '?')})" for b in bands],
              fontsize=6, frameon=False, loc="lower right")
    ax.grid(alpha=0.3, axis="x")


def panel_group_profiles(ax, groups, metrics):
    """Standardised metric means per behavioural group."""
    df = groups[groups["group"] >= 0]
    use = [m for m in metrics if m in df.columns
           and pd.to_numeric(df[m], errors="coerce").notna().sum() >= 3]
    if df.empty or not use:
        ax.set_visible(False)
        return
    z = df[use].apply(pd.to_numeric, errors="coerce")
    z = (z - z.mean()) / z.std(ddof=0).replace(0, np.nan)
    prof = z.groupby(df["group"]).mean()
    x = np.arange(len(use))
    for g, row in prof.iterrows():
        ax.plot(x, row.values, "o-", color=PALETTE[int(g) % len(PALETTE)],
                label=f"group {g}", lw=1.6, ms=4)
    ax.axhline(0, color="#999", lw=0.8, ls="--")
    ax.set_xticks(x)
    ax.set_xticklabels(use, rotation=40, ha="right", fontsize=5)
    ax.set_ylabel("z-score vs all nodes")
    ax.set_title("What separates each behavioural group")
    ax.legend(fontsize=6, frameon=False)
    ax.grid(alpha=0.3)


def main():
    ap = argparse.ArgumentParser(description="Figures for M3 characterisation")
    ap.add_argument("--groups", required=True, help="station_groups.csv")
    ap.add_argument("--similarity", required=True,
                    help="station_similarity.json")
    ap.add_argument("--covariates", help="station/node covariates CSV")
    ap.add_argument("--output", required=True, help="Output PNG")
    ap.add_argument("--output-index", help="Optional figure index JSON")
    args = ap.parse_args()

    index = {"figure": args.output, "panels": []}
    try:
        groups = pd.read_csv(args.groups)
        with open(args.similarity) as fh:
            similarity = json.load(fh)
        covariates = None
        if args.covariates:
            try:
                covariates = pd.read_csv(args.covariates)
            except Exception as exc:
                logger.warning("no covariates for plotting: %s", exc)

        if "group" not in groups.columns:
            groups["group"] = -1
        metrics = similarity.get("response_metrics_used", [])

        fig, axes = plt.subplots(2, 3, figsize=(15, 9))
        fig.suptitle("CPER soil-moisture station characterisation (M3)",
                     fontsize=13)
        panel_depth_profile(axes[0, 0], groups)
        panel_map(axes[0, 1], groups)
        panel_scatter(axes[0, 2], groups, covariates, similarity)
        panel_importances(axes[1, 0], similarity)
        panel_group_profiles(axes[1, 1], groups, metrics)

        ax = axes[1, 2]
        ax.set_axis_off()
        clus = similarity.get("clustering", {})
        lines = [
            f"nodes characterised: {similarity.get('n_nodes', 'n/a')}",
            f"distinct locations: {similarity.get('n_distinct_locations', 'n/a')}",
            f"groups (k): {clus.get('k', 'n/a')}  "
            f"silhouette: {clus.get('silhouette', 'n/a')}",
            f"depth range: {similarity.get('depth_range_cm', 'n/a')} cm",
            "",
            "Attribution is descriptive, not inferential:",
            "with few distinct locations, importances",
            "rank candidate drivers rather than prove them.",
        ]
        ax.text(0.02, 0.95, "\n".join(lines), va="top", fontsize=7.5,
                family="monospace")

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(args.output, dpi=150)
        plt.close(fig)
        index["panels"] = ["depth_profile", "station_map", "driver_scatter",
                           "importances", "group_profiles", "summary"]
        logger.info("Wrote %s", args.output)
    except Exception as exc:
        logger.error("visualize_response failed: %s", exc)
        index["error"] = str(exc)
        # Always leave the declared output in place.
        try:
            fig = plt.figure(figsize=(6, 2))
            fig.text(0.5, 0.5, f"visualisation failed:\n{exc}",
                     ha="center", va="center", fontsize=8)
            fig.savefig(args.output, dpi=100)
            plt.close(fig)
        except Exception:
            open(args.output, "a").close()

    if args.output_index:
        with open(args.output_index, "w") as fh:
            json.dump(index, fh, indent=2)


if __name__ == "__main__":
    main()
