#!/usr/bin/env bash
#
# Smallest end-to-end smoke test: fetch a short window and produce the gridded
# soil-moisture map, with no Pegasus and no HTCondor.
#
# This is deliberately narrow — a recent window, SCAN only unless you have a
# NEON token — so it finishes in a couple of minutes and proves the toolchain
# works. For the real thing see README "Running it":
#
#     ./fetch_data.sh
#     python3 workflow_generator.py --reuse-dir output -o workflow.yml
#     pegasus-plan --submit -s condorpool -o local workflow.yml
#
set -euo pipefail
cd "$(dirname "$0")"

OUT=output/smoke
CFG=site_config.json
END=$(date +%F)
START=$(date -v-400d +%F 2>/dev/null || date -d '400 days ago' +%F)

# 1. Python environment.
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip -q install -r requirements.txt

# 2. Optional: a free NEON token adds five soil plots (data.neonscience.org).
#    Without it you get SCAN only, which is enough for a smoke test.
# export NEON_TOKEN=...

# 3. Download the inputs. Idempotent — re-running skips what is already there.
#    A ~400-day window is the minimum that yields usable response fingerprints.
./fetch_data.sh --output-dir "$OUT" --start-date "$START" --end-date "$END"

# 4. Run the pipeline. Each of these is exactly the command the DAG issues.
python3 bin/harmonize.py --inputs "$OUT"/*_observations*.csv \
    --output "$OUT/observations.csv" --report "$OUT/harmonization_report.json"

python3 bin/soil_moisture_map.py --observations "$OUT/observations.csv" \
    --config $CFG --as-of "$END" --output "$OUT/soil_moisture_points.json"

python3 bin/build_covariates.py --config $CFG --dem "$OUT/dem.tif" \
    --polaris "$OUT/polaris_soil.tif" --sda "$OUT/sda_soil.json" \
    --observations "$OUT/observations.csv" \
    --output-stack "$OUT/covariates.tif" \
    --output-manifest "$OUT/covariates_manifest.json" \
    --output-stations "$OUT/node_covariates.csv"

for s in USCRN:94074 SCAN:2197 SCAN:2017 NEON:CPER; do
    python3 bin/station_response.py --observations "$OUT/observations.csv" \
        --station "$s" --config $CFG \
        --output "$OUT/response_${s//:/_}.json" || true
done

python3 bin/station_similarity.py --stage all \
    --fingerprints "$OUT"/response_*.json \
    --covariates "$OUT/node_covariates.csv" --config $CFG \
    --output "$OUT/station_similarity.json" \
    --output-groups "$OUT/station_groups.csv"

python3 bin/delineate_zones.py --covariates "$OUT/covariates.tif" \
    --manifest "$OUT/covariates_manifest.json" --config $CFG \
    --groups "$OUT/station_groups.csv" \
    --output-zones "$OUT/zones.tif" \
    --output-geojson "$OUT/soil_moisture_zones.geojson" \
    --output-stats "$OUT/zone_stats.json" \
    --output-membership "$OUT/station_zones.csv"

python3 bin/estimate_soil_moisture.py --points "$OUT/soil_moisture_points.json" \
    --zones "$OUT/zones.tif" --zone-stats "$OUT/zone_stats.json" \
    --covariates "$OUT/covariates.tif" \
    --manifest "$OUT/covariates_manifest.json" \
    --fingerprints "$OUT"/response_*.json --config $CFG --as-of "$END" \
    --output-map "$OUT/soil_moisture_now.tif" \
    --output-uncertainty "$OUT/soil_moisture_uncertainty.tif" \
    --output-json "$OUT/soil_moisture_now.json" \
    --output-skill "$OUT/estimation_skill.json"

python3 bin/visualize_soil_moisture.py --map "$OUT/soil_moisture_now.tif" \
    --uncertainty "$OUT/soil_moisture_uncertainty.tif" \
    --zones "$OUT/zones.tif" --geojson "$OUT/soil_moisture_zones.geojson" \
    --zone-stats "$OUT/zone_stats.json" \
    --now-json "$OUT/soil_moisture_now.json" \
    --skill "$OUT/estimation_skill.json" \
    --points "$OUT/soil_moisture_points.json" \
    --fingerprints "$OUT"/response_*.json \
    --output-figure "$OUT/soil_moisture_map.png" \
    --output-html "$OUT/soil_moisture_map.html"

echo
echo "Done. Open:"
echo "    $OUT/soil_moisture_map.html    (self-contained, no network needed)"
echo "    $OUT/soil_moisture_map.png"
echo
echo "Read $OUT/estimation_skill.json before trusting the map — a short window"
echo "and few stations make it a toolchain check, not a research product."
