# CPER Soil-Moisture Workflow

A Pegasus WMS workflow that produces a **dynamic soil-moisture map** for the
USDA-ARS **Central Plains Experimental Range** — ~15,000 acres of shortgrass
steppe in Weld County, CO (NEON site `CPER`, domain D10, 40.8155 / −104.7456).

It answers three questions, in one run:

1. **How does each monitoring station respond?** Per-depth response
   fingerprints — dry-down time constant, event response, memory, seasonal
   amplitude — and what soil and terrain properties drive them.
2. **Which areas behave alike?** Response zones delineated by clustering the
   soil/terrain covariate grid, validated against the observed behaviour.
3. **What is the soil moisture right now, everywhere?** A gridded estimate with
   an uncertainty layer and cross-validated skill, not just values at the
   sensors.

The pipeline is complete and verified end to end on a real HTCondor pool:
**186 executable jobs, 9 min 56 s, zero failures** on 4 workers × 4 CPUs.

`SPEC.md` is the design reference — frozen observation contract, node-id
convention, source roles and failure policy, evaluation criteria, verified
data-source facts, open questions and risks.

## The pipeline

```
fetch_neon --month 2016-07 ┐
    ... one job per month ...─> harmonize ─┬─> soil_moisture_map ────────────┐
fetch_neon --month 2026-07 ┤               │      (point scale)              │
fetch_uscrn, fetch_awdb ───┘               ├─> station_response × N ──┐      │
                                           │                          │      │
fetch_soil_properties ─┐                   │            similarity_cluster   │
fetch_terrain ─────────┴─> build_covariates│                 │   │           │
                              │            │    attribute ×9 ┘   │           │
                              │            │        │            │           │
                              │            │   similarity_merge  │           │
                              v            v                     v           │
                          delineate_zones <─────────────────────-┘           │
                                  │                                          │
                          zones.tif ──────┐                                  │
                                          v                                  v
                                      estimate_soil_moisture <───────────────┘
                                              │
                                              v
                                      visualize_soil_moisture
```

| Stage | What it does |
|---|---|
| `fetch_*` | Pull observations (USCRN, SCAN, NEON) and static rasters (DEM, POLARIS soil, SSURGO) |
| `harmonize` | Merge and deduplicate into one long-format table |
| `soil_moisture_map` | Point-scale current values per depth-node, with staleness flags |
| `build_covariates` | 49-band soil + terrain stack on a UTM 13N 10 m grid |
| `station_response` | Response fingerprint per depth-node (one job per station) |
| `similarity_cluster` | Behavioural groups, k by silhouette |
| `attribute` × 9 | Which covariates drive each response metric (one job per metric) |
| `similarity_merge` | Reassemble the attribution report |
| `delineate_zones` | Cluster the covariate grid into response zones; validate against behaviour |
| `estimate_soil_moisture` | Upscale station values to the whole grid + uncertainty + skill |
| `visualize_soil_moisture` | Six-panel figure and a self-contained HTML page |

---

# Running it

## 1. Set up

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Get a free NEON API token (data.neonscience.org → My Account → API Tokens) and
export it **before generating anything**:

```sh
export NEON_TOKEN=<your-token>
```

The generator captures the token and injects it into the job environment with
`add_env`. HTCondor runs jobs in a clean environment, so exporting it later has
no effect — this is the single most common reason a NEON job comes back empty.
Without a token the NEON source is dropped with a warning and you get SCAN +
USCRN only.

## 2. Build the container

Every workflow job runs inside `pegasus/cper-soilmoisture`, pulled from Docker
Hub and converted to Singularity/Apptainer.

```sh
docker build -t pegasus/cper-soilmoisture:m3 Docker/
docker push pegasus/cper-soilmoisture:m3           # docker login first
```

On Apple Silicon targeting an amd64 pool, build multi-arch:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
    -t pegasus/cper-soilmoisture:m3 --push Docker/
```

The image is lean (`python:3.11-slim` + pandas/numpy/requests/rasterio/
scikit-learn/matplotlib). The workflow scripts are staged in by Pegasus rather
than baked in, so it only needs rebuilding when job *dependencies* change — and
each dependency change gets a **new tag** rather than mutating an existing one.

⚠ **Verify the imports in the container, not just your venv.** rasterio's
manylinux wheel needs the system `libexpat1`, which `python:3.11-slim` omits — a
macOS venv uses a different wheel and will never reproduce the failure. An
import-time crash happens before the scripts can write their declared outputs,
so HTCondor *holds* the jobs on stage-out instead of failing them:

```sh
docker run --rm --platform linux/amd64 pegasus/cper-soilmoisture:m3 \
    python3 -c "import rasterio, sklearn, matplotlib, pandas"
```

## 3. Fetch the data

```sh
./fetch_data.sh
```

Downloads every input once into `inputs/` — 121 NEON site-months plus USCRN,
SCAN, the DEM, POLARIS and SSURGO. Measured: **126 files, 58 MB, 4 min 23 s**
with the default 6 workers. It is idempotent: re-running skips whatever is
already there, so an interrupted fetch resumes where it stopped.

Downloads land in `inputs/` and results in `output/` on purpose. Feeding the
run `--reuse-dir inputs` reuses only the downloads, so the analysis recomputes
in full every time you re-run it — which is what you want while iterating.

The script does not hardcode the work. It asks `workflow_generator.py` for the
download-only DAG and executes exactly the jobs that DAG contains, so adding a
source or changing the NEON month list needs no edit here.

It needs pandas/rasterio to run the fetchers directly. If the current
interpreter lacks them it **automatically uses the container you built in step
2** — which is what happens on a submit host, where only `pegasus-wms.api` is
installed. Force either way with `--container` / `--no-container`.

```sh
./fetch_data.sh --jobs 12                              # more concurrency
./fetch_data.sh --start-date 2024-01-01 --end-date 2024-06-30
./fetch_data.sh --sources awdb                         # skip NEON/USCRN
./fetch_data.sh --output-dir /data/cper                # somewhere else
```

There is also a `--mode fetch` DAG that does the same downloads through
Pegasus, if you want the fetch itself tracked with provenance and retries:

```sh
python3 workflow_generator.py --mode fetch -o fetch.yml
pegasus-plan --submit -s condorpool -o local fetch.yml
```

It is **not faster** — measured 7 m 18 s against the script's 4 m 23 s, because
167 jobs each pay scheduling and stage-out overhead that dwarfs a ~2 s
download. Use the script unless you specifically want the DAG's provenance
record; both leave `inputs/` in the same state.

## 4. Run the workflow

```sh
python3 workflow_generator.py --reuse-dir inputs -o workflow.yml
pegasus-plan --submit -s condorpool -o local workflow.yml
pegasus-status <submit-dir>
```

`--reuse-dir` registers everything already in that directory as a replica, and
Pegasus prunes the job that would have produced it — so the run starts at
`harmonize` instead of re-downloading. Results stage into `output/`.

### From a notebook, or any Python

`workflow_generator.py` is importable. `build_workflow()` takes the same
options as the command line — long names, dashes as underscores — and returns a
workflow object you can drive in place, which is what
`Access-CPER-SoilMoisture-Workflow.ipynb` does:

```python
from workflow_generator import build_workflow

fetch = build_workflow(mode="fetch", output_dir="inputs")
fetch.plan_submit()          # pegasus-plan --submit -s <site> -o local
fetch.wait()                 # blocks until the DAG finishes

run = build_workflow(mode="all", reuse_dir="inputs")   # build AFTER the fetch
run.plan_submit()
run.wait()
run.statistics()             # per-stage cost; .analyze() if it failed
```

Build the second workflow *after* the first finishes: reuse is resolved at
build time, and the catalogs use fixed filenames, so the last workflow built
owns them.

**Which directory you point it at decides what gets recomputed**, and this is
the whole trick:

| `--reuse-dir` | Reuses | Use it when |
|---|---|---|
| `inputs` | downloads only | re-running the analysis, however many times — every stage after `harmonize` recomputes |
| `output` (products from a previous run) | downloads *and* computed products | a nowcast, where reusing the covariates, zones and fingerprints is the point |

⚠ A full run against a directory of computed products prunes almost everything
and finishes in a few minutes having recomputed nothing. That looks like a fast
success and proves nothing — if you meant to re-run the analysis, point at
`inputs`.

Measured end to end on the Chameleon pool; every row was submitted and ran to
completion. These runs predate the switch to container universe, so the job
counts hold but the wall clocks may shift a little with how the image is
delivered:

| | executable jobs | wall clock |
|---|---|---|
| No prefetch, everything from scratch | 186 | 9 m 56 s |
| `./fetch_data.sh` (the prefetch) | — | 4 m 23 s |
| `--mode fetch` DAG instead of the script | 167 | 7 m 18 s |
| Run after prefetching (`--reuse-dir inputs`) | **45** | **6 m 1 s** |
| Re-run against a directory of computed products | **16** | 4 m 2 s |

The prefetched run produces **bit-identical** products: `zones.tif` and
`soil_moisture_now.tif` match pixel for pixel (0 of 1,471,599 differing), and
the JSON layers differ only in their `built_utc` provenance stamps.

⚠ **Prefetching barely helps a single run.** 4 m 23 s + 6 m 1 s ≈ 10 min, about
the same as the 9 m 56 s cold run, because the staging you save on the fetch you
pay again bringing 126 files back in. It pays from the *second* run onward — 6
min instead of 10 each time. Prefetch when you plan to iterate, want a pinned
data snapshot, or work where the source APIs are slow or blocked; skip it for a
genuine one-shot.

### Keeping the map current

Re-run with a short window, this time reusing a **previous run's products** —
that is what makes it cheap. The static products (covariates, zones) and the
fingerprints are reused, so only the recent observations and the estimate are
recomputed:

```sh
python3 workflow_generator.py --mode nowcast --reuse-dir output -o nowcast.yml
pegasus-plan --submit -s condorpool -o local nowcast.yml
```

⚠ `--mode nowcast` **without** `--reuse-dir` is a trap: it recomputes the
response fingerprints from the 30-day window, so the "climatology" each anomaly
is measured against becomes that same window and the map collapses toward
climatology. The generator warns if you do it.

---

# Running it manually, without Pegasus

Every stage is a plain Python script; the DAG only chains them. This is the
fastest way to test a change, and it needs no HTCondor.

```sh
source .venv/bin/activate
./fetch_data.sh --output-dir output/local --start-date 2024-01-01 --end-date 2026-07-01
```

Then run the stages in order. Zone delineation takes ~11 s and the estimator
~3 s on the real 1.5 M-pixel grid, so iterating here is cheap.

```sh
OUT=output/local
CFG=site_config.json
ASOF=2026-07-01

# Merge the fetched sources into the frozen observation contract
python3 bin/harmonize.py --inputs $OUT/uscrn_observations.csv \
    $OUT/awdb_observations.csv $OUT/neon_observations_*.csv \
    --output $OUT/observations.csv --report $OUT/harmonization_report.json

# Point-scale current values
python3 bin/soil_moisture_map.py --observations $OUT/observations.csv \
    --config $CFG --as-of $ASOF --output $OUT/soil_moisture_points.json

# Covariate stack (feeding it the observations gives each NEON plot its own
# coordinates instead of all nodes inheriting the tower's)
python3 bin/build_covariates.py --config $CFG --dem $OUT/dem.tif \
    --polaris $OUT/polaris_soil.tif --sda $OUT/sda_soil.json \
    --observations $OUT/observations.csv \
    --output-stack $OUT/covariates.tif \
    --output-manifest $OUT/covariates_manifest.json \
    --output-stations $OUT/node_covariates.csv

# Response fingerprint per station
for s in USCRN:94074 SCAN:2197 SCAN:2017 NEON:CPER; do
    python3 bin/station_response.py --observations $OUT/observations.csv \
        --station $s --config $CFG \
        --output $OUT/response_${s//:/_}.json
done

# Behavioural groups + covariate attribution (one process; the DAG splits this
# into cluster / 9 attribute jobs / merge, which is equivalent but parallel)
python3 bin/station_similarity.py --stage all \
    --fingerprints $OUT/response_*.json --covariates $OUT/node_covariates.csv \
    --config $CFG --output $OUT/station_similarity.json \
    --output-groups $OUT/station_groups.csv

python3 bin/visualize_response.py --groups $OUT/station_groups.csv \
    --similarity $OUT/station_similarity.json \
    --covariates $OUT/node_covariates.csv \
    --output $OUT/station_characterization.png \
    --output-index $OUT/figure_index.json

# Response zones
python3 bin/delineate_zones.py --covariates $OUT/covariates.tif \
    --manifest $OUT/covariates_manifest.json --config $CFG \
    --groups $OUT/station_groups.csv \
    --output-zones $OUT/zones.tif \
    --output-geojson $OUT/soil_moisture_zones.geojson \
    --output-stats $OUT/zone_stats.json \
    --output-membership $OUT/station_zones.csv

# The gridded estimate
python3 bin/estimate_soil_moisture.py --points $OUT/soil_moisture_points.json \
    --zones $OUT/zones.tif --zone-stats $OUT/zone_stats.json \
    --covariates $OUT/covariates.tif --manifest $OUT/covariates_manifest.json \
    --fingerprints $OUT/response_*.json --config $CFG --as-of $ASOF \
    --output-map $OUT/soil_moisture_now.tif \
    --output-uncertainty $OUT/soil_moisture_uncertainty.tif \
    --output-json $OUT/soil_moisture_now.json \
    --output-skill $OUT/estimation_skill.json

# Figure + self-contained HTML page
python3 bin/visualize_soil_moisture.py --map $OUT/soil_moisture_now.tif \
    --uncertainty $OUT/soil_moisture_uncertainty.tif --zones $OUT/zones.tif \
    --geojson $OUT/soil_moisture_zones.geojson \
    --zone-stats $OUT/zone_stats.json --now-json $OUT/soil_moisture_now.json \
    --skill $OUT/estimation_skill.json --points $OUT/soil_moisture_points.json \
    --fingerprints $OUT/response_*.json \
    --output-figure $OUT/soil_moisture_map.png \
    --output-html $OUT/soil_moisture_map.html
```

Or use the notebooks, which run these same scripts as subprocesses so they
cannot drift from what the workflow runs:

| Notebook | Needs | What it does |
|---|---|---|
| [`Run-CPER-SoilMoisture-Locally.ipynb`](Run-CPER-SoilMoisture-Locally.ipynb) | a laptop | The whole pipeline with **no Pegasus or HTCondor**, ending in the gridded map, uncertainty layer, skill table and HTML page, with the intermediate tables inline. **Start here to see results.** |
| [`Access-CPER-SoilMoisture-Workflow.ipynb`](Access-CPER-SoilMoisture-Workflow.ipynb) | Pegasus + a pool | Generates the DAG, shows the job breakdown, renders the graph, plans/submits, monitors, and inspects the staged products. Covers the prefetch/reuse flow and troubleshooting. |

---

# Scaling back

`--mode` defaults to **`all`** — the complete pipeline ending in the map.
The other modes exist to do *less*:

| Mode | Builds | Use it when |
|---|---|---|
| `all` | everything | **Default.** |
| `nowcast` | everything, on the last 30 days | Recurring updates. Pair with `--reuse-dir`. |
| `characterize` | fingerprints, zones, attribution | You want the characterization without the gridded map. |
| `static` | the covariate stack | Build and register it once. |
| `observe` | fetch → harmonize → point-scale | Just the observations. |
| `fetch` | downloads only | Prefetch, as an alternative to `fetch_data.sh`. |

```
--sources {uscrn,awdb,neon,sage} ...   default: uscrn awdb neon
--start-date / --end-date              override the fetch window (YYYY-MM-DD)
--reuse-dir DIR                        reuse already-fetched/computed products
--config PATH                          default: site_config.json
-e / --execution-site-name NAME        default: condorpool
--container-image IMAGE                default: pegasus/cper-soilmoisture:m3
                                       a bare name means Docker Hub; a full
                                       URL (https:// or file:// to a SIF) is
                                       used as-is
--no-container                         run in the site's native environment
--exec-universe {container,vanilla}    default: container
--no-sites-catalog                     plan against the submit host's catalog
--inherit-pegasusrc                    layer our properties onto ~/.pegasusrc
-o / --output FILE                     default: workflow.yml
```

### Which HTCondor universe

Jobs run in **container universe** by default: HTCondor creates the container
and PegasusLite runs inside it, with the image staged in as a data dependency.
Pegasus recommends this for every HTCondor pool, and it is the only thing that
works where the execution point is *itself* an unprivileged container — OSG /
OSPool / PATh, which is what **ACCESS Pegasus** provisions. There, the older
arrangement fails before the task starts:

```
Using /usr/bin/apptainer to run the container
ERROR  : Failed to set mount propagation: Permission denied
```

That is a nested unprivileged apptainer, not a workflow bug — and note that a
Pegasus example running fine on the same pool (the ACCESS Quickstart, say)
proves nothing, because it declares no container at all.

Use `--exec-universe vanilla` to go back to PegasusLite launching apptainer
itself, if a pool's HTCondor predates container universe.

On a managed submit host like ACCESS Pegasus, two more flags matter:
`--no-sites-catalog` plans against the platform's own site catalog
(`pegasus.catalog.site.repo.file` in `~/.pegasusrc`) instead of the one we
write — pass its site name to `-e` — and `--inherit-pegasusrc` layers our
properties onto `~/.pegasusrc`, without which the generated
`pegasus.properties` shadows the platform's settings entirely, since
`pegasus-plan` reads one file or the other and never both.

Explicit `--start-date/--end-date` win over the config windows. Each fetch job
carries a DAGMan retry of 2 for transient API failures.

⚠ The generated catalogs (`replicas.yml`, `sites.yml`, `transformations.yml`,
`pegasus.properties`) use **fixed filenames** regardless of `-o`. Generating two
variants back-to-back and then planning both will plan the second one's
catalogs twice — always pair generate → plan.

---

# What comes out

| File | Contents |
|---|---|
| **Observations** | |
| `<source>_observations.csv` | Per-source long format (`timestamp, source, node, lat, lon, variable, value, unit`) |
| `observations.csv` | Harmonized, deduplicated merge |
| `harmonization_report.json` | Per-source counts, node inventory, variable coverage, date spans |
| `soil_moisture_points.json` | Per depth-node current value + class, period stats, daily series, surface aggregation |
| **Static covariates** | |
| `covariates.tif` | 49-band stack on the UTM 13N 10 m grid, registered for reuse |
| `covariates_manifest.json` | Per-band source/units/provenance + grid definition |
| `node_covariates.csv` | Covariate vector at each observation node's own coordinates |
| `dem.tif`, `polaris_soil.tif`, `sda_soil.json` | Raw fetch products (provenance) |
| **Characterization** | |
| `response_<station>.json` | Per depth-node fingerprints (τ, event response, memory, climatology, percentile envelope, QC) |
| `station_similarity.json` | Behavioural groups + attribution, pooled and by depth band |
| `station_groups.csv` | Node → behavioural group, with all response metrics |
| `station_characterization.png` | Six-panel characterization figure |
| **Zones** | |
| `zones.tif` | Response-zone label raster (int16, `-1` nodata) |
| `soil_moisture_zones.geojson` | Zone polygons (EPSG:4326); slivers **tagged** `below_min_area`, not dropped |
| `zone_stats.json` | Per-zone area, covariate mean/std, feature centroids, station membership, station-free flags, validation |
| `station_zones.csv` | Node → zone |
| **The map** | |
| `soil_moisture_now.tif` | Gridded current estimate (float32, tiled + overviews, provenance in tags) |
| `soil_moisture_uncertainty.tif` | Per-pixel 1σ |
| `soil_moisture_now.json` | Per-zone and per-station tables, method record, class areas, provenance |
| `estimation_skill.json` | Leave-one-station-out per station and per zone, against two baselines, with a plain-language `verdict` |
| `soil_moisture_map.png` | Six-panel map figure |
| `soil_moisture_map.html` | Self-contained interactive page — no network access of any kind |

## How the map is built

**Zones** come from clustering the covariate grid, not the stations, so they
cover the whole site including where no sensor exists. All 49 bands are
standardised, reduced by PCA to 95 % of variance (the same soil property
appears at four depth intervals, so raw-band KMeans would silently weight
whichever property has the most bands), clustered with k chosen by silhouette,
then smoothed with a 5-pixel modal filter.

**The estimate** works in relative saturation `S = θ / θ_s` rather than raw
water content: a station reporting `S = 0.4` says "this profile is at 40 % of
its own pore space", which transfers across a soil boundary; `0.14 m³/m³` does
not. Each pixel gets

```
θ(pixel) = (S_clim_zone + ΔS_zone) × θ_s(pixel),   clipped to [θ_r, θ_s]
```

Because `θ_s` comes from the *pixel*, the map carries real soil texture inside
each zone instead of flat blocks. A zone with no station borrows its anomaly
from the nearest analogue **in covariate space**, never the nearest neighbour in
metres — soil/terrain similarity is the whole point.

A second tier (random forest on covariates + residual interpolation) is gated on
`analysis.min_stations_for_regression` distinct reporting locations, default 8.
With the ~7 the public network provides it stays off and the output records why;
a 49-covariate forest fitted to 7 points would be decoration.

**Uncertainty** per pixel is `sqrt(model_spread² + distance² + station_free²)` —
the spread of contributing stations, a distance term saturating at the observed
between-station spread over `analysis.decorrelation_length_m`, and a flat
penalty inside station-free zones.

---

# What the results actually show

These are load-bearing caveats, not boilerplate.

**The usable public network is ~7 surface locations**, not the ~30 the original
framing implies. Every uncertainty layer, station-free-zone flag and small-n
guard exists because of this.

**Leave-one-station-out skill**, measured on the real 2026-07-01 state:

| | RMSE (m³/m³) | bias |
|---|---|---|
| zone-anchored estimate | 0.0147 | −0.0012 |
| site-mean baseline | 0.0144 | +0.0001 |
| climatology-only baseline | 0.0389 | +0.0328 |

**The anomaly step is doing the work — 2.6× better than climatology. The zoning
currently is not.** 6 of 7 reporting stations land in one zone, so the zone
labels have almost no discriminating power where they can be validated, and the
adjusted Rand index between covariate zones and observed behaviour is slightly
*negative*. This is reported, not tuned away: `estimation_skill.json` carries a
`verdict` string and the HTML prints it above the map.

**The driver rankings do not generalise.** Cross-validated by *location*
(`cv_scheme: leave-one-location-out`), **8 of the 9 surface metrics have a
negative R²** — worse than predicting the mean. Only `seasonal_amplitude` clears
zero, at +0.24. The importances and Spearman ρ still rank candidate drivers
(slope, TWI, heat load, curvature); they do not establish them.

> ⚠ Before 2026-08-06 this was cross-validated leave-one-**node**-out, which
> leaked: inside a depth band `depth_cm` is dropped and sibling nodes at one
> plot share coordinates, so 16 of the 24 surface rows are *exact duplicates* of
> another row. That reported `plant_available_range` at R² = +0.61 where the
> honest value is −0.57. `loo_r2` is kept as an alias of `cv_r2`, but every
> value now comes from the grouped scheme. Do not "restore" the older, prettier
> numbers.

**2051 ha (14 % of the site) sits in a zone with no station** and is estimated
from a covariate-space analogue. It is hatched in the figure, flagged in the
JSON, and carries an explicit uncertainty penalty.

**Grid resolution does not imply precision.** 10 m is the covariate resolution,
not the resolution at which 7 stations can constrain soil moisture.

**USCRN Nunn has been dark since 2026-05-28.** All five of its depths are stale
at the current as-of date and excluded from the map, leaving 5 NEON plots and 2
SCAN stations.

The single change that would improve all of this is more distinct station
locations — the ARS network — not more modelling.

---

# Configuration

`site_config.json` drives everything. Under `analysis`:

| Key | Default | Effect |
|---|---|---|
| `nowcast_window_days` | 30 | `--mode nowcast` fetch window |
| `historical_window` | 1997-01-01 → 2026-07-01 | window for all other modes |
| `min_reporting_stations` | 3 | below this, the map is published with a warning |
| `min_stations_for_regression` | 8 | distinct locations needed before tier 2 engages |
| `max_current_age_days` | 5 | staleness cutoff for "current" |
| `surface_depth_max_cm` | 20 | which depth-nodes count as surface |
| `cluster_k_candidates` | 3–8 | k search for behavioural groups and zones |
| `decorrelation_length_m` | 2000 | distance scale in the uncertainty layer |
| `station_free_zone_penalty` | 0.03 | added uncertainty inside station-free zones |
| `min_zone_polygon_ha` | 1.0 | polygons below this are tagged, not dropped |
| `sm_thresholds` | 0.06/0.12/0.20/0.30 | dryness class breakpoints |

## Data sources

| Source | Stations | Auth | Default |
|---|---|---|---|
| NOAA **USCRN** | Nunn 7 NNE (WBAN 94074, on site) — 5/10/20/50/100 cm. ⚠ Dark since 2026-05-28 | none | on |
| NRCS **SCAN** (AWDB) | `2197:CO:SCAN` "CPER" + `2017:CO:SCAN` "Nunn #1" — 5 depths, records to 1997 | none | on |
| **NEON** | `DP1.00094.001` soil water content, 5 soil plots × 8 depths, 2016-07→ | free token | on if token set |
| **Sage/Waggle** | node X001 at the NEON tower | none | off (publishes nothing public yet) |
| **POLARIS** | ~30 m van Genuchten `θs/θr/α/n` + ksat/texture/bd/om at 4 depth intervals — no pedotransfer needed | none | on |
| **SSURGO** (Soil Data Access) | Horizon-level cross-check, best-effort | none | on |
| **USGS 3DEP** | 10 m DEM → slope, aspect, curvature, TPI, TWI, heat load | none | on |

**Every in-situ source is best-effort** (the USCRN outage forced this): on
persistent failure a fetcher writes its declared (empty) output, logs an ERROR
and exits 0. `harmonize` fails only if *every* source came back empty.
Depth-resolved sensors publish on `station@<depth>cm` nodes.

Because NEON serves one package per site-month, a multi-year window fans out
**one fetch job per month** (121 for the full record). The month list is read
from NEON's own catalogue at generation time rather than guessed from a
publication lag, which drifts.

---

# Layout

```
workflow_generator.py    Pegasus DAG generator (--mode all by default)
fetch_data.sh            Download every input once into inputs/
example_usage.sh         Minimal no-Pegasus smoke test
site_config.json         Stations, per-source config, analysis grid + parameters
bin/
  fetch_*.py                 Per-source fetchers (best-effort, retry, write-empty)
  harmonize.py               Merge + dedupe into the frozen contract
  soil_moisture_map.py       Point-scale layer
  build_covariates.py        Covariate stack + per-node extraction
  station_response.py        Per-station response fingerprints
  station_similarity.py      Behavioural groups + attribution (--stage ...)
  visualize_response.py      Characterization figure
  delineate_zones.py         Covariate-grid zones + validation
  estimate_soil_moisture.py  Gridded upscaling + uncertainty + skill
  visualize_soil_moisture.py Map figure + self-contained HTML
Docker/                  Container (pegasus/cper-soilmoisture, python:3.11-slim)
SPEC.md                  Contract, architecture, evaluation, verified sources
```

Related: [`../drought-workflow/`](../drought-workflow/) — the Snowy Range
forest/snow workflow this repo shares its observation contract with (kept
byte-identical; see SPEC.md §1). Note `../soilmoisture-workflow/` is an
unrelated irrigation workflow.
