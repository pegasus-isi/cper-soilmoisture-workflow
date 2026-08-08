# SPEC — CPER soil-moisture workflow

The single reference for this workflow: what it is for, the contracts
implementers must hold constant, the architecture it is being built toward,
how it will be judged, and the verified facts about every data source.

**Site.** USDA-ARS Central Plains Experimental Range, ~15,000 acres (62.7 km²)
of shortgrass steppe in Weld County, CO. Reference point: NEON site `CPER`,
domain D10, **40.815536, −104.745591**, ~1650 m. Analysis grid: UTM 13N
(EPSG:32613) at 10 m.

**Status.** **M1–M4 all implemented and verified on a real HTCondor pool**
(2026-08-06, cpegasus run0007: 186/186 jobs, 9 min 56 s). Remaining: recurrence
(cron / `pegasus-em`), the hierarchical-workflow and job-clustering refinements
in §8 M4, and `docs/CPER_WALKTHROUGH.md`. See §8.

---

## 1. What this workflow is for

From the researcher's email, three asks in order:

1. **Characterize each monitoring station's historical soil-moisture response**
   and relate it to that station's soil profile, topographic position,
   vegetation, and climate. *"Being able to visualize this would be great."*
2. **Delineate areas expected to behave similarly**, by overlaying the existing
   CPER soil-property maps with topography and station locations.
3. **Estimate current soil moisture across the whole site** from live sensor
   observations — a **dynamic map that updates as new field data arrive.**

Plus a fourth, meta ask: *more examples of how Pegasus is being used* (§8, M4).

⚠ **One premise did not survive verification.** The email describes ~30
monitoring stations; only about **four publicly reachable soil-moisture
stations** exist at CPER (§10). His network is presumably ARS-internal. This
is the project's one blocking dependency (§11 Q1) and it shapes the whole
design: with this few stations, uncertainty layers and station-free-zone
flagging are load-bearing, not decoration.

The good news: every source except his own network is anonymous or free-token,
so the entire pipeline can be built and cross-validated on public data and
then re-pointed at his stations. **Data access is not on the critical path.**

**Relationship to `../drought-workflow/`.** This is a deliberately separate
workflow (rangeland soil moisture vs. subalpine forest/snow), not a second
region inside it. It shares that repo's observation contract verbatim (§2) so
fetchers stay portable. Do not merge them, and do not refactor
`drought-workflow` while working here — the one permitted change there is a
README pointer, already made. Note `../soilmoisture-workflow/` is an unrelated
irrigation workflow.

## 2. Observation contract (frozen)

Long-format CSV, byte-identical to `../drought-workflow/` so fetchers stay
portable between the two workflows:

```
timestamp, source, node, lat, lon, variable, value, unit
```

- `timestamp`: ISO-8601, parsed as UTC by `drought_common.load_observations`.
- `variable` / canonical units: the vocabulary in `bin/drought_common.py`
  (`soil_moisture` m3/m3, `soil_temp` degC, `air_temp` degC, `precip` mm,
  `rel_humidity` percent, `surface_temp` degC, ...). Fetchers convert units
  *before* emitting; `harmonize` warns on mixed units but does not convert.
- `bin/drought_common.py`, `bin/harmonize.py`, and `bin/fetch_sage_data.py`
  are copies from `drought-workflow`. Do not fork their behaviour; if a change
  is needed, make it in both repos. Only extract a shared package once the
  copies actually diverge in behaviour — not pre-emptively, for three files.

## 3. Node id convention (CPER addition)

Depth-resolved sensors encode depth in the node id:

```
<station>@<depth-cm>cm      e.g.  USCRN:94074@20cm, SCAN:2197@5cm,
                                  NEON:CPER:SP3@6cm
```

Bare node ids (`USCRN:94074`) carry the station's depth-less variables
(air temp, precip, RH, ...). Station ids: `USCRN:<wban>`, `SCAN:<station-id>`,
`NEON:<site>:SP<plot>`. Consumers parse with
`soil_moisture_map.split_node()` — regex `^(.+)@(\d+)cm$`.

This stays inside the frozen contract (the `node` column is free-form); the
variable vocabulary is NOT extended with per-depth names.

## 4. Source roles and failure policy

**Every in-situ source is best-effort**: on persistent failure (or a
legitimately empty window) the fetcher writes its declared output, logs an
ERROR, and exits **0**. `harmonize` is the backstop — it fails the run only
when EVERY source is empty — and the M4 estimator additionally refuses to
publish below `analysis.min_reporting_stations`.

| Source | Fetcher | Notes |
|---|---|---|
| USCRN Nunn 7 NNE | `fetch_uscrn_data.py` | ⚠ station outage since **2026-05-28** (daily + hourly both dark) — the event that decided this policy: no single station may kill a nowcast while others on site are live |
| SCAN 2197 + 2017 (AWDB) | `fetch_awdb_data.py` | two stations in one job; current to within a day |
| NEON DP1.00094.001 | `fetch_neon_data.py` | needs `NEON_TOKEN`; dropped at generation when unset |
| Sage/Waggle | `fetch_sage_data.py` | currently always empty at CPER — no public data |

Never exit without writing every declared output — HTCondor holds the job on
stage-out otherwise and the DAG hangs. All fetch jobs get
`add_dagman_profile(retry="2")` plus in-process HTTP retries with backoff.
Config errors (missing block, no dates) still exit non-zero: those are
generation mistakes that should fail before anyone trusts the run.

## 5. Credentials

`NEON_TOKEN` (free, data.neonscience.org profile) is captured at generation
time and injected with `add_env(NEON_TOKEN=...)`. A missing token drops the
NEON source at generation with a warning — it is never defaulted to `""`.
Same pattern applies to Earthdata credentials if SMAP is added.

HTCondor runs jobs in a clean environment: a credential exported in the submit
shell never reaches the job or its container. This has already cost one
debugging cycle in the sibling workflow; do not rediscover it.

## 6. Target architecture

Three branches, selected by `--mode`. The split between an expensive **static**
branch (changes rarely, registered and reused) and a cheap **dynamic** branch
(re-runs on a cadence) is the single most important design decision — it is
what makes "updates as new data arrive" affordable, and it maps directly onto
Pegasus data reuse.

```
  ── STATIC BRANCH (run once per site; outputs registered and reused) ──────────
  fetch_soil_properties ─┐
  fetch_terrain ─────────┼─> build_covariates ─┬─> station_covariates.csv ──┐
  (station metadata) ────┘                     └─> covariates.tif           │
                                                                            │
  ── CHARACTERIZATION BRANCH (long historical window, monthly-ish) ──────────┤
  fetch_uscrn ───┐                        ┌─> station_response(1) ─┐        │
  fetch_awdb ────┤                        ├─> station_response(2) ─┤        │
  fetch_neon ────┼─> harmonize ─> split ──┼─> ...  (1 job/station) ┼─> merge─┤
  fetch_archive ─┤   (historical)         └─> station_response(N) ─┘        │
  fetch_stations ┘                                                   │      │
                                          station_similarity ◄───────┴──────┤
                                                   │                        │
                                          delineate_zones ◄─────────────────┤
                                                   │                        │
                                          zones.geojson + attribution.json ─┤
                                                                            │
  ── NOWCAST BRANCH (every N hours; the only part that repeats) ─────────────┤
  fetch_uscrn ───┐                                                          │
  fetch_awdb ────┼─> harmonize ─> soil_moisture_map ─> estimate_soil_moisture┘
  fetch_neon ────┤   (last N days)       (point scale)          │
  fetch_stations ┘                                              ├─> soil_moisture_now.tif
                                                                ├─> uncertainty.tif
                                                                └─> dynamic_map.html + .png
```

A `nowcast` run is ~6 jobs and minutes long; it consumes `zones.geojson` and
`covariates.tif` from the replica catalog instead of rebuilding them.

**Implemented today (M1)** — `characterize` and `nowcast` differ only in the
fetch window until the M3 fan-out lands:

```
fetch_uscrn ─┐
fetch_awdb  ─┼─> harmonize ─> soil_moisture_map
fetch_neon  ─┤
(fetch_sage)─┘
```

## 7. Generator modes

`workflow_generator.py --mode {fetch,observe,static,characterize,nowcast,all}`,
**default `all`**. The default is the whole product; the others scale back.

| Mode | Branches | Window | Notes |
|---|---|---|---|
| `all` | M1+M2+M3+M4 | `analysis.historical_window` | **Default.** 137 jobs. |
| `nowcast` | M1+M2+M3+M4 | last `nowcast_window_days` | Identical DAG to `all`; meant to be paired with `--reuse-dir`, which reduces it to ~16 executable jobs. Warns if run without it, because recomputing fingerprints over 30 days makes the "climatology" the nowcast window itself. |
| `characterize` | M1+M2+M3 | historical | Fingerprints, zones, attribution; no gridded map. |
| `static` | M2 | none | Build + register the covariate stack. |
| `observe` | M1 | historical | Observations + point-scale layer only. |
| `fetch` | fetchers only | historical | Download-only DAG; outputs staged out **and registered**. Run once, then `--reuse-dir`. |

`--reuse-dir DIR` registers every declared output already present in DIR as a
replica, so Pegasus's workflow reduction prunes its producer. This is the
mechanism behind C4. Note that Pegasus prunes a job only when *every* one of its
outputs is in the catalog, and that the generated catalogs use fixed filenames —
always pair generate → plan rather than generating several variants first.

## 8. Roadmap

| Milestone | Contents | Deliverable to the researcher |
|---|---|---|
| **M1** ✅ | Observation pipeline: fetchers, harmonize, point-scale layer, generator | USCRN + SCAN + NEON observations flowing; point-scale soil moisture on real CPER data |
| **M2** ✅ | Static covariates | Covariate stack + `station_covariates.csv`; a static map of soil and terrain at his stations |
| **M3** ✅ | Station characterization + zone delineation | **His "useful first step"**: per-station response fingerprints, what drives them, and a first zone delineation with validation. *Both halves done 2026-08-05/06.* |
| **M4** ✅ | Live upscaling, dynamic map, recurrence | **The dynamic soil-moisture map**, with uncertainty, cross-validated skill and a self-contained HTML page. *Implemented and verified on the pool 2026-08-06 (cpegasus run0007, 186/186 jobs, 9 min 56 s).* Recurrence (cron/`pegasus-em`) not yet set up. |

M2 is mostly mechanical. **M3 is where the science judgement lives and should
be reviewed with him before M4 is built on top of it.** M4 is the demo.

### M1 — observation pipeline (done)

Fetchers in §4, all emitting the frozen contract. `fetch_awdb_data.py` is a
light generalization of the sibling workflow's `fetch_snotel_data.py` — same
AWDB REST API, SCAN triplets instead of SNOTEL ones — which is the strongest
practical argument for keeping the observation contract identical across the
two workflows.

All three verified on the pool on 2026-08-03 (run0005): NEON with a real
token, the published container, and `pegasus-plan --submit` on HTCondor.

### M2 — static covariates (done)

Implemented and run on the pool 2026-08-03 (run0007). Deviations from the
plan below, all deliberate: terrain derivatives are computed in
`build_covariates` on the final metric grid rather than in `fetch_terrain`
(the math runs once, in projected space, and fetchers stay pure fetchers);
they use plain numpy rather than `py3dep`/`whitebox`, keeping the container
lean; TWI uses D8 accumulation on an **unconditioned** DEM (no pit fill),
which the manifest records as a covariate-grade, not routing-grade, layer.
1 m lidar remains an option, not a product requirement — the 10 m COG is
what the static branch reads today.

- **`fetch_soil_properties.py`** — two complementary products:
  - **Soil Data Access (SDA)** for authoritative horizon-level properties over
    the CPER bbox: sand/silt/clay, available water capacity, θ⅓ and θ15 bar,
    bulk density, Ksat, organic matter, rock-fragment volume, depth to
    restriction.
  - **POLARIS** (~30 m gridded) for continuous surfaces *including van
    Genuchten `alpha`, `n`, `theta_r`, `theta_s`* — directly usable for
    soil-moisture behaviour, so **no pedotransfer step is needed**.
  - Considered and rejected as primaries: SOLUS100 (no AWC or water-retention
    layers), SoilGrids (250 m; reported 33 % clay where SSURGO says 17.5 %).
    gSSURGO's Valu1 table has pre-computed available water storage for
    0–5/0–30 cm via Planetary Computer STAC if a shortcut is wanted.
- **`fetch_terrain.py`** — derive elevation, slope, aspect (as
  northness/eastness so it is numerically usable), plan/profile curvature,
  topographic wetness index, topographic position index, flow accumulation,
  and a heat-load index. On shortgrass steppe TWI and TPI will likely matter
  more than slope. Sources in preference order in §10.
- **`build_covariates.py`** — reproject/resample everything onto the analysis
  grid (UTM 13N, 10 m, with the option of 1 m terrain aggregated to the grid),
  write a multi-band `covariates.tif` plus a `covariates_manifest.json`
  recording every band's source, date, and units, and extract the covariate
  vector at each station into `station_covariates.csv`.

These outputs are **registered in the replica catalog** so later runs skip this
branch entirely via Pegasus data reuse.

### M3 — historical characterization and zones (characterization done)

**Implemented 2026-08-03** — `station_response.py`, `station_similarity.py`,
`visualize_response.py`, and the `characterize` branch in the generator;
verified locally on 12 years of SCAN data and inside the `:m3` container.
`delineate_zones.py` is **not** built yet. Deviations and additions from the
plan below, all deliberate:

- **Fan-out unit is the station, not the node.** One job fingerprints every
  depth-node of a station, so a NEON plot with six sensor depths costs one
  job rather than six. Node-level granularity is preserved in the output; only
  the scheduling granularity is coarser.
- **NEON fetch fans out per month** (120 jobs for CPER's full record). NEON
  serves one package per site-month, so this is the natural parallel axis and
  it is what makes a decade-long pull practical. The month list comes from
  NEON's own catalogue at generation time (token-free metadata endpoint)
  rather than a configured lag, which drifts.
- **Cross-validation is grouped by location, not by node** (2026-08-06). Inside
  a depth band `depth_cm` is dropped and sibling nodes at one plot share
  coordinates, so 16 of 24 surface rows are exact duplicates — leave-one-node-out
  scored the model against its own training rows. Grouped by location, **8 of 9
  surface metrics have negative R²**. Reported as `cv_r2`/`cv_scheme`/`cv_folds`;
  `loo_r2` kept as an alias.
- **The stage is split across the DAG** (`--stage cluster|attribute|merge`).
  `station_similarity.json` is read only by `visualize_response`, so the zone and
  M4 branches hang off the ~11 s cluster stage while nine per-metric attribution
  jobs run beside them. 677 s → ~33 s of wall clock; whole run 21 m 46 s → 9 m 56 s.
- **Attribution is run twice: pooled and within depth bands.** Depth dominates
  every response metric inside a single profile (verified: importance 0.78–0.96
  on SCAN), so a pooled fit mostly rediscovers depth. The depth-band fits
  answer the question that actually matters — at comparable depth, which soil
  and terrain properties separate one location from another.
- **`build_covariates` is fed the observations in this mode**, emitting
  `node_covariates.csv` so each NEON soil plot gets covariates at its own
  coordinates instead of all 38 nodes inheriting the tower's. Kept under a
  distinct LFN so data reuse cannot hand a characterize run the coarser
  station-level table.
- **Small-n guards are load-bearing, not decoration** (C5): fits are skipped
  below 6 nodes or 3 distinct locations, leave-one-out R² is reported and
  allowed to be negative, `n_distinct_locations` is carried into the outputs,
  and the figure *withholds* the driver-scatter panel below 3 locations — a
  covariate taking only two values across the network will correlate with
  anything.
- **No scipy**: recession τ is a log-linear fit with a search over the
  asymptote, and the TPI focal mean is an integral image. scipy arrives only
  as an sklearn dependency.

Verified physics on the 12-year SCAN record (2 stations × 5 depths): τ rises
with depth (11.4 d at 5 cm → 32.8 d at 102 cm), event response attenuates with
depth (0.0030 → 0.0002 ΔVWC/mm), and the sandy site dries faster than the clay
site at every depth (SCAN:2197 *Vona*, 75 % sand, τ = 13.0 d at 10 cm vs
SCAN:2017 *Ascalon*, 19 % clay, τ = 25.1 d) — the M2 covariates predicting the
M3 fingerprint, which is exactly the relationship the attribution exists to
quantify once enough distinct locations are in.

#### Original plan (retained; `delineate_zones.py` is now built — see above)

- **`station_response.py`** — one job per station, fanned out in parallel.
  From each station's full record, compute a response fingerprint:
  - VWC climatology and percentiles per depth; seasonal amplitude
  - wet/dry reference points: field-capacity proxy (post-drainage plateau
    after large events), seasonal-minimum proxy for the dry end, and the
    plant-available range between them
  - **event response**: per precip event, ΔVWC per mm, lag to peak, and how
    deep the wetting front is detectable — this is what separates a sandy
    ridge station from a clay swale station
  - **dry-down recession constant τ** from exponential fits to post-event
    dry-downs; the classic single-number summary of soil-moisture response
  - **memory**: autocorrelation e-folding time
  - data-quality flags and sensor-health summary (gaps, flatlines, out-of-range)
- **`station_similarity.py`** — two things in one job:
  1. Cluster stations on their *response* fingerprints → **behavioural groups**
     ("these 6 stations dry down the same way").
  2. Regress/attribute those fingerprint metrics onto the M2 soil, terrain, and
     climate covariates — correlation matrix plus random-forest importance
     ranking → **"τ is driven mostly by clay % and TWI; the event-response
     ratio by rock fragment volume."** That is the literal answer to ask 1.
- **`visualize_response.py`** — the "being able to visualize this would be
  great" deliverable: station map coloured by behavioural group, fingerprint
  metric vs. covariate scatter panels, dry-down curve overlays by group, and a
  covariate-importance bar chart.
- **`delineate_zones.py`** — cluster the *covariate grid* (not the stations)
  into k response zones, k chosen by silhouette score. Then **validate against
  the fingerprints**: compare each station's covariate cluster label against
  its behavioural group (adjusted Rand index / cross-tab). Agreement is the
  evidence that the zones mean something; disagreement localizes where the
  covariates are missing something. Outputs `soil_moisture_zones.geojson` +
  `zones.tif`, per-zone covariate statistics, station-to-zone membership, and
  a flag for **zones containing no station** — pure extrapolation on the
  dynamic map, and shown differently.

### M4 — live upscaling, dynamic map, recurrence

**Implemented 2026-08-06** — `delineate_zones.py` (the M3 half M4 stands on),
`estimate_soil_moisture.py`, `visualize_soil_moisture.py`, and the `m4` branch
in the generator, which is now the **default** (`--mode all`). **Verified on a
real pool** (cpegasus run0007: full 186-job `--mode all` from scratch, 9 min
56 s, 0 failures; run0004: `--reuse-dir` reduced the same DAG to 16 jobs in
4 min 2 s), reproducing the local and in-container numbers exactly. Deviations
from the plan below, all deliberate:

- **Tier 1 works in relative saturation `S = θ/θ_s`, not raw VWC or absolute
  anomaly.** A station at `S = 0.4` is at 40 % of its own pore space, which
  transfers across a soil boundary; `0.14 m³/m³` does not. The painted value is
  `θ(pixel) = (S_clim_zone + ΔS_zone) × θ_s(pixel)` clipped to `[θ_r, θ_s]`.
  Taking `θ_s` from the **pixel** rather than the zone is what gives the map
  within-zone soil texture instead of flat zone blocks, and guarantees physical
  plausibility for free.
- **Tier 2's gate is `analysis.min_stations_for_regression` (default 8) distinct
  locations, not `min_reporting_stations` (3)** as this section originally said.
  A 49-covariate forest fitted to the 7 locations the public network provides is
  decoration, not a refinement. Tier 2 therefore never engages today and the
  output records why; the code path is complete for when the ARS network lands.
- **The uncertainty distance term saturates at the observed between-station
  spread**, not an invented constant — that is the natural scale for "how wrong
  can distance make you".
- **Skill is reported against two baselines** (site mean, climatology-only).
  Measured on 2026-07-01: zone-anchored RMSE 0.0147, site-mean 0.0144,
  climatology 0.0389. Read honestly, the **anomaly step is worth 2.6× over
  climatology while the zoning currently buys nothing** — 6 of 7 stations fall
  in one zone, and the zone-vs-behaviour ARI is slightly negative. This is the
  expected consequence of station scarcity (§12) and is surfaced in
  `estimation_skill.json` as a plain-language `verdict` and printed above the
  map in the HTML.
- **Station-free zones borrow from their nearest analogue in covariate space**,
  using the per-zone feature centroids `delineate_zones` emits for exactly this
  purpose. Never nearest-in-metres — that is what C2 rules out.
- **Zone polygons below `min_zone_polygon_ha` are tagged, not dropped.**
  Dropping them punched holes in the polygon layer that read as missing data and
  made the geojson disagree with `zones.tif` about how much of the site is
  covered.
- **`--mode fetch` + `--reuse-dir`** implement the data-reuse story below.
  Measured with a real planner: 147 abstract jobs plan to **186** executable
  jobs cold and **16** against a populated `--reuse-dir`.

#### Original plan (retained; the tier structure is as built)

**`estimate_soil_moisture.py`**, a two-tier estimator so it degrades
gracefully:

1. **Zone-anchored anomaly upscaling (primary, robust).** For each zone,
   compute the current anomaly (relative to the M3 climatology) from its member
   stations, then apply that anomaly to the zone's climatology. Anomaly-based
   upscaling travels much better across a heterogeneous site than absolute VWC.
2. **Regression + residual interpolation (refinement).** Fit a random forest of
   current station VWC on covariates, predict the grid, interpolate the station
   residuals and add them back. Falls back to tier 1 below
   `analysis.min_reporting_stations`.

Every pixel gets an uncertainty value combining model spread, distance to the
nearest contributing station, and a penalty for station-free zones. Products:
`soil_moisture_now.tif` (COG), `soil_moisture_uncertainty.tif`, and
`soil_moisture_now.json` with per-zone and per-station tables. **Validation:**
leave-one-station-out cross-validation reported as RMSE/bias per station and
per zone in `estimation_skill.json` — without this the map is a picture, not a
research product.

**`visualize_soil_moisture.py`** — a PNG panel (current map over zones, station
dots, uncertainty, per-zone timeline strips) plus a **self-contained HTML page**
with zone polygons, station popups showing each fingerprint, and a run
timestamp. Static and self-contained means it can be dropped on any web host or
opened from a file — the least-friction way to give a field researcher
something that "updates". Each product embeds the covariate-manifest and zone
versions, so a map can always be traced to the exact static baseline it used.

**Making it actually dynamic — the Pegasus part, and the part worth showing
off:**

- **Data reuse.** Static and zone products are registered outputs; a `nowcast`
  DAG planned against those registrations has its static branch pruned
  automatically — same abstract workflow, radically smaller executable one.
- **Hierarchical workflow.** Express `characterize` as a sub-workflow job so a
  monthly re-characterization is one node inside the recurring parent rather
  than a separate operational procedure.
- **Job clustering.** Per-station jobs are individually small; horizontal
  clustering keeps scheduling overhead from dominating.
- **Recurrence.** Cron or `pegasus-em` submitting `nowcast` on a cadence, with
  a rolling fetch window and a bookmark for "data since last run".
- **Provenance and operations.** `pegasus-statistics` for per-stage cost,
  `pegasus-analyzer` for failures, per-station retry so one dead logger does
  not sink a run.

**Docs (`docs/CPER_WALKTHROUGH.md`)** — the researcher's own three-step
narrative mapped onto DAG nodes, written for someone who does not know Pegasus:
one figure of the DAG, one paragraph per stage, and a "what Pegasus is buying
you here" section (reuse of the expensive static branch, automatic retry on
flaky station APIs, provenance from raw observation to published map, the same
DAG on a laptop / campus pool / testbed). For his "more examples" ask, this
repo sits alongside nine other Pegasus workflows spanning air quality, crop
health, seismology, genomics, metagenomics, bioacoustics, and irrigation
support — several with the same edge-sensor shape as his Sage deployment. A
short curated list plus **a live run of the CPER nowcast on his own data**
answers the question better than documentation.

### Container

M1 runs on a lean `python:3.11-slim` + pandas/numpy/requests image
(`kthare10/cper-soilmoisture:latest`). **M2 (`:m2`, built 2026-08-03)** adds
only **rasterio** — keeping terrain derivatives in numpy meant no
scipy/whitebox/geopandas were needed, so the image stayed on `python:3.11-slim`
instead of moving to a GDAL base. New tag per change; `:latest` is not mutated.

⚠ **`libexpat1` is a required apt package**: rasterio's manylinux wheel bundles
GDAL/PROJ but dynamically links the system libexpat, which `python:3.11-slim`
omits. A macOS venv uses a different wheel and will not reproduce the failure —
verify container imports on a worker
(`apptainer exec docker://... python3 -c "import rasterio"`). An import-time
crash precedes the write-empty-output handlers, so HTCondor *holds* the job on
stage-out rather than failing it.

M3 will add scikit-learn (clustering, random-forest attribution) and a
plotting stack; geopandas/shapely arrive with zone polygonization.

## 9. Evaluation — constraints, non-constraints, expected outputs

Stated before implementation so success is not defined after the fact.
Everything traces to a line of the researcher's email or a verified fact in §10.

### 9.1 Constraints (binding)

| # | Constraint | Where it comes from |
|---|-----------|---------------------|
| C1 | **Whole-site coverage.** The map covers the entire ~15,000-acre site, not just neighbourhoods of stations. | *"a real time soil moisture map for the entire area"* |
| C2 | **Interpolation must be soil/terrain-aware, not distance-only.** Each station is representative of its soil profile, vegetation, and topographic position — plain IDW across a soil boundary is disqualified by the researcher's own framing. | *"Each monitoring location is unique because of differences in soil profile, vegetation, and topographic position"* |
| C3 | **Start from the existing CPER soil-property maps.** Zone delineation overlays those maps with topography and station data; we may supplement with POLARIS/SDA but not ignore what he already has (§11 Q2). | *"Soil property maps are already available for CPER"* |
| C4 | **The map must update as new field data arrive.** Operationally: a `nowcast` re-run must be cheap (a handful of jobs, minutes) by reusing registered static products — otherwise "dynamic" is a claim, not a property. | *"a dynamic soil moisture map that updates as new field data become available"* |
| C5 | **Honest uncertainty.** With only ~4 public stations, every map ships an uncertainty layer and explicit station-free-zone flags, and the grid resolution must not imply precision the network cannot support. | Verified station scarcity; §12 |
| C6 | **Visualization is a first-class deliverable**, for both the historical characterization and the live map. | *"Being able to visualize this would be great"* |
| C7 | **The observation contract stays byte-identical** to `drought-workflow`, so fetchers remain portable. | §2 |
| C8 | **Credential and failure discipline.** Tokens injected via `add_env`; fetch jobs follow §4 and never exit without writing declared outputs. | §4, §5 |
| C9 | **Provenance.** Every published map embeds the covariate-manifest and zone versions it was built from, and is reproducible from the replica catalog + run record. | Research-product framing |

### 9.2 Non-constraints (explicitly not binding)

The feedback is deliberately open on several axes. Naming them prevents
over-building:

- **No latency or cadence is specified.** "Real time" is undefined until §11
  Q5/Q6 are answered; a **daily** nowcast is the working default. Not building
  sub-hourly streaming — telemetry, not our pipeline, sets the floor anyway.
- **No grid resolution is mandated.** 10 m is the honest default; the 1 m lidar
  is an input option, not a product requirement.
- **Nowcast, not forecast.** The ask is *current* conditions from live
  observations. No predictive modelling is in scope.
- **No publication platform is specified** (§11 Q8). A self-contained HTML page
  + COG satisfies M4; ArcGIS/web-service integration is follow-on work.
- **Sage is not a required data source.** Verification found no soil moisture
  anywhere in Sage's public API (§11 Q7); the fetcher stays best-effort and may
  return empty indefinitely.
- **No particular estimation method is required.** Zone-anchored anomaly
  upscaling is acceptable alone; the regression tier is a refinement. Satellite
  products (SMAP) are a sanity check, never a mapping input.
- **Vegetation/grazing covariates are not requested** (though §11 Q10 flags
  them as likely valuable — an enhancement pending his answer).
- **No operational SLA.** Per-run monitoring and retry are sufficient; 24/7
  service guarantees are out of scope.

### 9.3 Expected outputs and acceptance criteria

| Ask | Output artifacts | Accepted when |
|-----|------------------|---------------|
| **1. Historical per-station characterization** (M3) | `station_response` fingerprints (climatology, event response, dry-down τ, memory, QC flags) per station; `station_similarity` attribution (correlation matrix + random-forest importances); `visualize_response` figure set | Every station with ≥1 year of record gets a complete fingerprint; the attribution names which soil/terrain/climate covariates drive each fingerprint metric, with importance scores; the figures let him see behavioural groups and what separates them without reading a table |
| **2. Delineate similar-behaviour areas** (M3) | `soil_moisture_zones.geojson` + `zones.tif`, per-zone covariate summaries, station-to-zone membership, station-free-zone flags | k is chosen by silhouette score, not fiat; agreement between covariate zones and behavioural groups is reported (adjusted Rand index / cross-tab), and disagreements are localized on a map rather than averaged away |
| **3. Dynamic soil-moisture map** (M4) | `soil_moisture_now.tif` (COG), `soil_moisture_uncertainty.tif`, `soil_moisture_now.json`, self-contained HTML + PNG, `estimation_skill.json` | Leave-one-station-out RMSE/bias reported per station and per zone, and the estimator **beats two baselines**: (a) the site-wide mean of reporting stations and (b) distance-only IDW — if it can't, the map adds nothing over what he can already do; a `nowcast` re-run completes in minutes via data reuse (C4); the estimator degrades to tier 1 below the minimum-station threshold instead of failing; every product embeds its static-baseline versions (C9) |
| **4. More Pegasus examples** (M4) | `docs/CPER_WALKTHROUGH.md`; curated list of the sibling workflows; a live `nowcast` run | The walkthrough maps his three-step narrative onto DAG nodes and states concretely what Pegasus bought; the strongest acceptance test is a live run over CPER data he recognizes |

**On quantitative skill targets:** no absolute RMSE threshold is set a priori —
with ~4 public stations, LOSO is a demanding test and an arbitrary number would
be theatre. The binding criterion is *relative*: beat the two baselines above,
and report skill honestly enough that he can judge usability. If his
~30-station network comes online, re-run the same evaluation; the skill report
is designed so before/after is directly comparable.

**Pipeline-level acceptance (every mode):** `harmonize` output is non-empty
(every in-situ source is best-effort — a lesson forced during M1, when USCRN
Nunn went dark on 2026-05-28 and a required-source policy would have killed
every nowcast while SCAN stayed live; `harmonize` fails only when *all* sources
are empty, and the M4 estimator refuses to publish below
`analysis.min_reporting_stations`); no job exits without writing its declared
outputs; a `nowcast` DAG planned against registered static products contains no
static-branch jobs (data reuse actually pruned them); and a from-scratch `all`
run on a clean machine reproduces the published map bit-for-bit given the same
inputs.

### 9.4 M1 outputs

| File | Producer | Contents |
|---|---|---|
| `<source>_observations.csv` | fetchers | per-source long-format observations |
| `observations.csv` | harmonize | merged, deduped, analysis-ready |
| `harmonization_report.json` | harmonize | provenance + per-source/variable coverage |
| `soil_moisture_points.json` | soil_moisture_map | per-node current/mean/min/anomaly/class + `current_date`/`age_days`/`stale`, per-station surface aggregation, IDW preview grid, daily series |

Observations are **hard-bounded to `--as-of`** (the generator passes the
fetch-window end), so "current" is the last value at or before that date and
period statistics never include later data. Nodes whose last observation is
older than `analysis.max_current_age_days` are flagged `stale` and **excluded**
from the per-station surface aggregation, the region mean, and the IDW grid —
a station that went dark degrades the map instead of silently publishing
weeks-old readings as current conditions.

The IDW grid is a point-scale preview only; the defensible spatial product is
the M4 zone-anchored upscaling (C2, C5).

### 9.5 M2 outputs

| File | Producer | Contents |
|---|---|---|
| `dem.tif` | fetch_terrain | 3DEP 10 m crop over bbox +0.01°, source CRS (EPSG:4269) |
| `polaris_soil.tif` | fetch_soil_properties | 40 bands = 10 variables × 4 depth intervals, log10 vars linearized |
| `sda_soil.json` | fetch_soil_properties | SSURGO map units, major-component horizons, per-station dominant component |
| **`covariates.tif`** | build_covariates | 49 bands on the analysis grid (9 terrain + 40 soil); **registered** |
| **`covariates_manifest.json`** | build_covariates | per-band source/URL/fetch time/units + grid definition + caveats; **registered** |
| **`station_covariates.csv`** | build_covariates | covariate vector per station (and per observation node in `--mode all`); **registered** |

Verified values (run0007): grid EPSG:32613 @10 m, 1017×1447; NEON tower
elevation 1653.9 m (published ~1650); SDA reports *Ascalon* at the tower,
independently matching NEON's megapit series; SCAN:2197 is distinctly sandier
(75 % sand, ksat 12.6 cm/hr, SDA *Vona*) than the two Ascalon sites — evidence
the covariates separate station environments, which is the M3 input.

## 10. Verified data sources

All endpoints checked live on **2026-07-29** (HTTP status observed, not
inferred), except where a later M1 note says otherwise.

### In-situ soil moisture — the mapping inputs

| Source | Station / product | Record | Depths | Auth |
|---|---|---|---|---|
| **NOAA USCRN** | **Nunn 7 NNE, WBAN 94074** (40.81/−104.76, on site; official name is literally "Ag. Res. Svc. Central Plains Exp. Range"). ⚠ dark since 2026-05-28 | 2004→ (air/precip); soil sensors ~2009–2011 | 5, 10, 20, 50, 100 cm VWC + soil temp | none |
| **NRCS SCAN** (AWDB REST) | `2197:CO:SCAN` "CPER" (40.8228/−104.7107, 2013-09→), `2017:CO:SCAN` "Nunn #1" (40.8599/−104.7403, SM from 1997-03) | 1997→ | −2, −4, −8, −20, −40 in `SMS` + `STO` | none |
| **NEON** | `DP1.00094.001` soil water content & salinity | 2016-07→2026-06 (120 months) | ~6–200 cm, all 5 instrumented soil plots, 1-min and 30-min | **token** |
| **Ag Data Commons** | article 24855600 — `CPER-DEX_SoilMoisture.csv` | 2019→ | 10–100 cm in 10 cm steps, 11 plots, ~weekly | none |
| **EDI / LTER PASTA** | `knb-lter-sgs.164.17` weekly TDR soil water; `.147`–`.152` neutron probe | 1983–1992, 1997–2001 | profile | none |
| CPER / ARS-LTAR network | his ~30 stations | unknown | unknown | **TBD (§11 Q1)** |

**Fetcher gotchas found while implementing M1:**

- **USCRN daily01**: 28 whitespace-separated fixed fields; sentinels are
  −9999.x (met) and −99.000 (soil moisture); station lat/lon are embedded in
  every row (prefer them over config). One file per station-year at
  `.../daily01/{YYYY}/CRND0103-{YYYY}-CO_Nunn_7_NNE.txt`; a missing year 404s.
- **AWDB depth wildcard**: a bare `SMS` element can return `[]`. Use `SMS:*`
  and read the depth from `stationElement.heightDepth` (inches, negative below
  surface). Keep `ordinal == 1` only — `STO` has a duplicate ordinal-2 sensor
  that would otherwise collide under harmonize's dedup key.
- **AWDB `/stations`** silently ignores `stateCodes` and `networkCodes` —
  always filter by explicit station triplets. It is also where SCAN lat/lon
  come from.
- **NEON** serves monthly packages; fan out one job per product-month and
  merge — this is where the workflow first gets real parallelism. Depths and
  per-plot coordinates come from the month's `sensor_positions` file, whose
  column names have drifted across releases (match by substring). Filter on
  `VSWCFinalQF == 0`.

### Other observations

| Source | Variables | Auth | Notes |
|---|---|---|---|
| NEON | `DP1.00041.001` soil temp; **`DP1.00044.001` precip (weighing gauge)**; `DP1.00002.001` air temp; `DP1.00098.001` RH; `DP1.00096.001` megapit soil properties (2012-08 only); `DP4.00200.001` eddy covariance | token | ⚠ `DP1.00006.001` is **not available at CPER** — split into `DP1.00044.001` (CPER ✓) and `DP1.00045.001` (CPER ✗) |
| EDI / LTER PASTA | CPER met: `knb-lter-sgs.118.20` **15-minute, 1986–2010**; `.116.19` hourly; `.115.17` daily | none | The deepest historical record available anywhere for this site |
| Ag Data Commons | `CPER-PPT_gapfilled_1980-2018.csv` (24854316); 30-catch-can precip network 1982–2013 (24855249) | none | Figshare API works anonymously; download via `ndownloader.figshare.com/files/{id}` (the `figshare.com/ndownloader/...` host hits a bot wall) |
| **gridMET** | daily `pr, tmmn, tmmx, rmin, rmax, srad, vs, pet, etr, vpd` at ~4 km | none | THREDDS NCSS point subset, CSV-native, **current to within days**. Preferred climate forcing. ⚠ port 8443 does not connect |
| Daymet | daily climate at 1 km | none | Single-pixel API works, ⚠ **record ends 2025** and out-of-range requests silently clamp — validate the returned date range |
| CoAgMet | air temp, RH, precip, wind, solar, ETr; soil **temp** at 5/15 cm | none | ⚠ **No station at Nunn** and **no usable soil moisture** near CPER — nearest is Ault at 27.5 km, soil temp only |
| SMAP | `SPL4SMGP.008` / `SPL3SMP_E.006` (9 km), `SPL2SMAP_S.003` (3 km) | Earthdata Login (free) | Coarse anomaly check only — one 9 km cell ≈ the whole 62.7 km² site. ⚠ legacy NSIDC pool is dead; use `earthaccess` |
| Sentinel-1 | — | — | ⚠ **No turnkey CONUS soil-moisture product** (Copernicus SSM 1 km is Europe-only). Deriving SM from RTC backscatter is a research project, not a fetch job |

### Static covariates

| Source | Products | Auth | Notes |
|---|---|---|---|
| **Soil Data Access (SDA)** | horizon properties: sand/silt/clay, AWC, θ⅓, θ15, bulk density, Ksat, OM, rock fragments, depth to restriction | none | ⚠ **POST-only** (GET returns 400); WKT is lon-lat; spatial helper is `SDA_Get_Mukey_from_intersection_with_WktWgs84`. Verified: 169 horizon rows over the bbox; point query at the tower → *Ascalon fine sandy loam*, which independently matches NEON's megapit soil series. Python client: `py-soildb`; ⚠ `pysda` on PyPI is an unrelated package |
| **POLARIS** | ~30 m gridded incl. **van Genuchten `alpha`, `n`, `theta_r`, `theta_s`** + bd/clay/sand/silt/ksat/om/ph, six depth intervals | none | **No pedotransfer step needed.** One 1°×1° tile covers all of CPER. ⚠ HTTP-only host; `ksat`/`alpha`/`om` are log10-transformed |
| **USGS 3DEP via `prd-tnm` S3** | **10 m**: one COG (`USGS_13_n41w105.tif`) contains the whole bbox. **1 m lidar**: 15 tiles, projects `CO_EasternColorado_2018_A18` + `CO_NorthwestCO_2020_D20` | none | Simplest and most reliable terrain route. `py3dep` + `whitebox` for TWI/curvature. 1 m resolves the swale-and-ridge microtopography that plausibly drives station-to-station differences |
| 3DEP ImageServer | server-side `Slope Degrees`, `Aspect Degrees`, hillshade, contours; 1 m pixel | none | ⚠ `identify` misreads shorthand `geometry=lon,lat` as Web Mercator → NoData; pass the JSON geometry form |
| OpenTopography | point clouds (`CO_Eastern_B1_2018`) | ⚠ **API key now required** (free) | Catalogue endpoint still open; prefer `prd-tnm` to avoid the credential |
| gSSURGO / gNATSGO | 10 m mukey raster; **Valu1** table has pre-computed AWS for 0–5/0–30/0–100 cm | none | ⚠ Box download UI is not curl-scriptable; the scriptable route is Planetary Computer STAC `gnatsgo-rasters` |
| SOLUS100 | 100 m texture, bulk density, fragments, SOC, depth to bedrock | none | ⚠ **No AWC or water-retention layers** — needs a PTF, so secondary to POLARIS |
| SoilGrids | 250 m global | none | Works, but reported 33 % clay where SSURGO says 17.5 % — prefer US-native products |
| CPER pasture / grazing boundaries | management covariates | ARS internal | §11 Q10. ⚠ AgCROS ArcGIS services carry CPER **geometry** (boundary, soils, 141 ecological sites, pastures) but **no observations**; several similarly named layers are traps — `soil_moisture1994` is the national soil-moisture *regime* polygon map, and `MoistureTDR`/`Moisture2024` are at Cook Agronomy Farm in Washington |

### Sage / Waggle at CPER

Anonymous query API confirmed working
(`POST https://data.sagecontinuum.org/api/v1/query`). Node manifest is at
`auth.sagecontinuum.org/manifests/` — note `api.sagecontinuum.org/api/v1/nodes`
returns 404.

| VSN | Location | Status | Hardware | Public data |
|---|---|---|---|---|
| **X001** | 40.815536, −104.745591 — **the exact NEON CPER tower** | Deployed | PTZ camera, LoRaWAN, Orin AGX 64 | ⚠ **none** (empty over a 90-day scan) |
| V003 | same coordinates | Retired | 2× StarDot netcam = the NEON PhenoCams | none |
| H019 | 40.8416, −104.7161 — ≈ the ARS/LTAR station | Deployed | **LoRaWAN gateway**, AGX Thor | system telemetry only |
| W021 | Fort Collins (CSU) | Deployed | incl. a "CSU Soil Sensor" | ⚠ env + sys only — **the soil sensor publishes nothing** |

⚠ **There is no soil moisture anywhere in the public Sage API** — network-wide
regex scans for `.*soil.*` and `.*moisture.*` returned nothing. Sage at CPER is
an imagery and LoRaWAN-gateway asset for now.

### Recommended starting set

| Role | Source | Auth |
|---|---|---|
| Primary in-situ soil moisture | **USCRN Nunn 7 NNE** — on-site, 5 depths (while it is publishing) | none |
| Second in-situ | NRCS SCAN `2197:CO:SCAN` + `2017:CO:SCAN` — 5 depths, back to 1997 | none |
| Third in-situ (profile) | NEON `DP1.00094.001` — 5 soil plots, 2016-07→ | token |
| Historical depth | EDI PASTA (15-min met 1986–2010; TDR 1997–2001) + ADC CPER-DEX | none |
| Soil hydraulics | POLARIS (van Genuchten) + SDA horizon query | none |
| Terrain | `prd-tnm` 1 m lidar, or the single 10 m `n41w105` COG | none |
| Climate forcing | gridMET THREDDS NCSS | none |
| Coarse anomaly check | SMAP SPL4SMGP.008 | Earthdata Login |

## 11. Open questions for the researcher

Verification answered three outright; they are kept with their answers, since
he should see them.

1. **Where does the 30-station data live, and in what format?** ⚠ **The
   blocking question.** Only ~4 publicly reachable soil-moisture stations exist
   at CPER (§10), and the old LTAR met portal (`ltar.nal.usda.gov`) no longer
   resolves. The "30" most likely traces to the 30 **rain catch cans** of a
   precipitation-only network that ran 1982–2013; his own network is presumably
   ARS-internal. A station metadata table (id, lat/lon, sensor model, depths,
   install date) plus a few sample logger exports unblocks everything.
2. **Which soil-property map is "already available" for CPER?** We can proceed
   on SSURGO (Ascalon fine sandy loam at the tower, matching NEON's megapit)
   plus POLARIS. If he has a CPER-specific finer survey — likely, given the
   site's research history — that is strictly better; a file or citation is all
   we need.
3. ~~Is there a lidar DEM for CPER?~~ **Answered: yes.** 1 m USGS lidar covers
   the site from two 2018/2020 projects, anonymously downloadable.
4. **Should NEON soil products be used as an anchor** alongside the ARS
   stations? NEON's five soil plots plus USCRN give a credible public backbone;
   worth knowing whether he considers them representative.
5. **Target update cadence, latency, and grid resolution** for the dynamic map
   — hourly or daily? Station density is the real constraint (§12), so 10 m is
   the honest default.
6. **How do the stations report?** Telemetered (cell/LoRa/Sage) or periodic
   manual download? This sets what "real time" can mean. Node **H019** near the
   ARS station is a deployed **LoRaWAN gateway** — if his stations have LoRa
   radios, that may already be the telemetry path.
7. ~~Which Sage node VSNs are at CPER?~~ **Answered, and worth flagging back to
   him:** **X001** is deployed at the exact NEON tower coordinates but
   **publishes nothing** to the public Sage query API (empty over a 90-day
   scan). **V003** is its retired predecessor; **H019** is a live LoRaWAN
   gateway publishing only system telemetry. Also: there is no soil moisture
   anywhere in Sage's public API. Is X001 meant to be public, or writing to a
   project-private path? He is actively deploying Sage nodes, and it matters
   for the "live" path.
8. **Where should the map be published** — a web page, ArcGIS, or something
   internal to CPER?
9. **Is leave-one-station-out cross-validation an acceptable skill metric**, or
   would he rather hold out specific stations he considers representative?
10. **Vegetation and grazing.** CPER has long-term grazing treatments; should
    pasture/treatment boundaries and vegetation state be covariates? They
    plausibly matter as much as soil texture for surface soil moisture, and
    they are the one covariate class the feedback does not mention.

## 12. Risks

- **Data access is the schedule risk, not the code — and it is the confirmed
  top risk.** Only ~4 public stations are reachable and the LTAR portal is
  gone, so his network is the difference between a demonstration and a research
  product. Mitigation: build on public data first (which works end-to-end),
  with a CSV adapter ready for his exports.
- **Station density vs. site size is worse than assumed.** With ~4 stations
  plus 5 NEON soil plots plus 11 DEX plots rather than 30 stations,
  zone-anchored anomaly upscaling is not merely preferable — it is the **only**
  defensible approach, and several zones will still contain no station. A fine
  continuous surface would imply precision the network cannot support no matter
  how good the 1 m terrain is. Uncertainty layers and station-free-zone
  flagging carry more weight here than they would with 30 stations.
- **Individual stations go dark without warning.** USCRN Nunn stopped
  publishing mid-M1 (2026-05-28). Mitigation is already in the design: all
  sources best-effort (§4), staleness bounding (§9.4), and the
  `min_reporting_stations` gate.
- **Soil map resolution may not resolve the variability.** SSURGO map units can
  be coarser than the toposequence variation the stations see. POLARIS at 30 m
  and 1 m terrain both help, but M3's validation against behavioural groups is
  the real check; if agreement is poor, the fix is finer soil data or
  terrain-led zoning — and we will know which.
- **Credential handling for NEON (and SMAP).** Both fail as opaque 403/302
  errors on a worker node if the credential is exported in the submit shell
  instead of injected with `add_env` (§5).
- **"Real time" depends on telemetry.** If stations are logger-download rather
  than telemetered, the map updates at download cadence, not hourly. Settle
  expectations early (§11 Q6).
- **Divergence between the two workflows.** The cost of a separate repo is
  three copied files that can drift. Mitigation: treat the observation contract
  as frozen in both (§2).
- **Scope creep back into `drought-workflow`.** Resist "just generalizing" the
  forest workflow while building this (§1).
