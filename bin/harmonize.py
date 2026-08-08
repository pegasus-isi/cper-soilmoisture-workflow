#!/usr/bin/env python3
"""Harmonize observations from every source into one analysis-ready dataset.

Merges the long-format CSVs produced by the SAGE, GLEES (AmeriFlux) and SNOTEL
fetchers (and any future adapter) onto the common schema, reconciles units,
drops exact duplicates, and emits:

  * a single harmonized observations CSV consumed by all layer jobs, and
  * a harmonization report (provenance + coverage per source/variable).

Sources stay as distinct nodes (e.g. GLEES tower vs co-located SNOTEL #367) so
complementary instruments are preserved rather than collapsed; the report flags
which variables each source contributes.
"""

import argparse
import json
import logging
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import drought_common as dc  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("harmonize")


def harmonize(paths):
    """Concatenate, validate units, and dedupe observations from all sources."""
    df = dc.load_observations(paths)
    if df.empty:
        return df, {"n_observations": 0, "duplicates_dropped": 0,
                    "sources": {}, "variables": {},
                    "warnings": ["no observations from any source"]}

    warnings = []
    # Unit-consistency check: each variable should carry one canonical unit.
    canonical = {}
    for var, grp in df.groupby("variable"):
        units = grp["unit"].dropna().unique().tolist()
        canonical[var] = units[0] if units else ""
        if len(units) > 1:
            warnings.append(f"{var} reported in mixed units {units}; "
                            f"fetchers should normalise before harmonize")
        if var not in dc.VARIABLES:
            warnings.append(f"{var} not in the canonical vocabulary")

    # Drop exact duplicate observations (same source/node/var/timestamp).
    before = len(df)
    df = df.drop_duplicates(
        subset=["source", "node", "variable", "timestamp"]
    ).sort_values(["variable", "node", "timestamp"]).reset_index(drop=True)
    dropped = before - len(df)

    # Provenance report.
    per_source = {}
    for src, grp in df.groupby("source"):
        per_source[src] = {
            "n_observations": int(len(grp)),
            "nodes": sorted(grp["node"].unique().tolist()),
            "variables": sorted(grp["variable"].unique().tolist()),
            "start": str(grp["timestamp"].min()),
            "end": str(grp["timestamp"].max()),
        }
    per_var = {}
    for var, grp in df.groupby("variable"):
        per_var[var] = {
            "unit": canonical.get(var, ""),
            "n_observations": int(len(grp)),
            "sources": sorted(grp["source"].unique().tolist()),
        }

    report = {
        "n_observations": int(len(df)),
        "duplicates_dropped": int(dropped),
        "sources": per_source,
        "variables": per_var,
        "warnings": warnings,
    }
    return df, report


def main():
    ap = argparse.ArgumentParser(description="Harmonize multi-source observations")
    ap.add_argument("--inputs", nargs="+", required=True,
                    help="Observation CSVs to merge (sage, glees, snotel, ...)")
    ap.add_argument("--output", required=True, help="Harmonized observations CSV")
    ap.add_argument("--report", help="Optional harmonization report JSON")
    args = ap.parse_args()

    df, report = harmonize(args.inputs)
    df.to_csv(args.output, index=False)
    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)

    logger.info("Harmonized %d observations from %d sources (%d dups dropped) -> %s",
                report["n_observations"], len(report["sources"]),
                report["duplicates_dropped"], args.output)
    for w in report["warnings"]:
        logger.warning(w)

    # Individual fetchers degrade to empty on failure so one flaky source can't
    # block the run. But if EVERY source came back empty the result is
    # meaningless — fail loudly here. The (empty) output was already written, so
    # the job fails cleanly rather than being held on a missing stage-out file.
    if report["n_observations"] == 0:
        logger.error("No observations from ANY source; failing the workflow. "
                     "Check the fetch job logs for the per-source errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
