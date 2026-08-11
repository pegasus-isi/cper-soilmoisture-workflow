#!/usr/bin/env python3

"""CPER Soil-Moisture Workflow Generator for Pegasus WMS.

Builds the workflow described in SPEC.md: real-time soil-moisture mapping
for the USDA-ARS Central Plains Experimental Range from public in-situ sensor
networks (USCRN, NRCS SCAN, NEON) plus, later, the ARS station network.

**--mode all is the default and runs the whole pipeline**, ending in the gridded
dynamic map. The other modes exist to scale *back* when that is more than you
want:

    all          (default) everything below, on the historical window
    nowcast      everything, but on the last analysis.nowcast_window_days days.
                 Identical DAG to `all`; with --reuse-dir the expensive static
                 and characterization branches are pruned by Pegasus data
                 reuse and what is left is a handful of jobs (constraint C4).
    characterize everything except the gridded map — fingerprints, zones and
                 attribution
    static       the covariate stack only
    observe      observations only — fetch, harmonize, point-scale layer
    fetch        download only: every fetcher, outputs staged out and
                 registered, nothing computed. Run this once, then point a real
                 run at it with --reuse-dir so nothing is downloaded twice.

Full DAG (--mode all). NEON serves one package per site-month, so a multi-year
window fans out per month; fingerprints then fan out per station, and the
attribution fans out per response metric:

    fetch_neon --month 2016-07 ┐
        ... one job per month ...─> harmonize ─┬─> soil_moisture_map ──────────┐
    fetch_neon --month 2026-07 ┤               │      (point scale)            │
    fetch_uscrn, fetch_awdb ───┘               ├─> station_response x N ─┐     │
                                               │                        │     │
    fetch_soil_properties ─┐                   │        similarity_cluster     │
    fetch_terrain ─────────┴─> build_covariates│             │   │             │
                                  │            │  attribute x9 ┘   │           │
                                  │            │      │            │           │
                                  │            │ similarity_merge  │           │
                                  v            v                   v           │
                              delineate_zones <────────────────────-┘           │
                                      │                                        │
                              zones.tif ────┐                                  │
                                            v                                  v
                                        estimate_soil_moisture <───────────────-┘
                                                │
                                                v
                                        visualize_soil_moisture
                                        soil_moisture_now.tif
                                        soil_moisture_uncertainty.tif
                                        soil_moisture_map.png / .html

In every mode that includes both the observation and static branches, harmonize
feeds build_covariates so the covariate table gains per-depth node coordinates
(emitted as node_covariates.csv) — without it every NEON depth-node would
inherit the tower's soil and terrain.

Usage:
    ./workflow_generator.py --config site_config.json -o workflow.yml
    ./workflow_generator.py --mode fetch -o fetch.yml          # download once
    ./workflow_generator.py --reuse-dir output -o workflow.yml  # then reuse it
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from Pegasus.api import *

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

ALL_SOURCES = ("uscrn", "awdb", "neon", "sage")

MODES = ("fetch", "observe", "static", "characterize", "nowcast", "all")

# Which branches each mode builds. `all` and `nowcast` differ only in the
# fetch window: the DAG is identical, so a nowcast run planned against
# registered static/zone products reduces to a few jobs (SPEC.md C4).
MODE_BRANCHES = {
    "fetch":        {"fetch"},
    "observe":      {"m1"},
    "static":       {"m2"},
    "characterize": {"m1", "m2", "m3"},
    "nowcast":      {"m1", "m2", "m3", "m4"},
    "all":          {"m1", "m2", "m3", "m4"},
}

# What those branch ids mean, for the generation summary. The ids stay as they
# are because SPEC.md is the design record and still numbers the branches;
# nothing a user reads should need that numbering to make sense.
BRANCH_LABELS = {
    "fetch": "download only",
    "m1": "observations",
    "m2": "covariates",
    "m3": "characterization",
    "m4": "gridded map",
}

# Fan NEON out per month only when the window is long enough to be worth the
# scheduling overhead; a 30-day nowcast stays a single job.
MONTHLY_FANOUT_MIN = 3


def response_metrics():
    """The response metrics station_similarity attributes, one job each.

    Read out of the script itself rather than duplicated here, so the fan-out
    cannot silently drift from what the stage actually computes. Parsed with
    `ast` instead of imported because the submit host is not required to have
    pandas/sklearn installed - generation needs only pegasus-wms.api.
    """
    import ast

    src = Path(__file__).parent / "bin" / "station_similarity.py"
    tree = ast.parse(src.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "RESPONSE_METRICS"
                for t in node.targets):
            return [ast.literal_eval(e) for e in node.value.elts]
    raise RuntimeError(f"RESPONSE_METRICS not found in {src}")

# Fetch-job failure policy (SPEC.md section 4): every in-situ source is
# best-effort — it writes its declared output, logs an ERROR, and exits 0 on
# persistent failure, leaving harmonize to fail only if every source is empty.
FETCHERS = {
    # source: (transformation, script, memory)
    "uscrn": ("fetch_uscrn_data", "bin/fetch_uscrn_data.py", "1 GB"),
    "awdb": ("fetch_awdb_data", "bin/fetch_awdb_data.py", "1 GB"),
    "neon": ("fetch_neon_data", "bin/fetch_neon_data.py", "2 GB"),
    "sage": ("fetch_sage_data", "bin/fetch_sage_data.py", "1 GB"),
}


class CperSoilMoistureWorkflow:
    """Generate the Pegasus workflow for CPER soil-moisture mapping."""

    wf = sc = tc = rc = props = None
    wf_name = "cper-soilmoisture"

    def __init__(self, dagfile="workflow.yml", reuse_dir=None, output_dir=None):
        self.dagfile = dagfile
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.shared_scratch_dir = os.path.join(self.wf_dir, "scratch")
        # Where stage-out lands. Keeping downloads (--mode fetch --output-dir
        # inputs) apart from results (output/) is what makes the analysis
        # re-runnable: --reuse-dir inputs then reuses only the *inputs*, so
        # every stage after harmonize recomputes. Point --reuse-dir at a
        # directory that also holds computed products and Pegasus prunes those
        # too, leaving a run that finishes in seconds and proves nothing.
        self.local_storage_dir = (os.path.abspath(output_dir) if output_dir
                                  else os.path.join(self.wf_dir, "output"))
        # Directory of already-downloaded/­computed products to reuse. Every
        # declared output that is found there is registered as a replica, and
        # Pegasus workflow reduction then prunes the job that would have
        # produced it (see _declare_output).
        self.reuse_dir = os.path.abspath(reuse_dir) if reuse_dir else None
        self.reused = []
        self.produced = []

    def write(self):
        if self.sc is not None:
            self.sc.write()
        self.props.write()
        self.rc.write()
        self.tc.write()
        self.wf.write(file=self.dagfile)

    def create_pegasus_properties(self, inherit_pegasusrc=False):
        self.props = Properties()
        # pegasus-plan reads ./pegasus.properties INSTEAD of ~/.pegasusrc, so on
        # a managed submit host (ACCESS Pegasus writes its site catalog pointer,
        # staging site and data configuration into ~/.pegasusrc) the file we
        # generate here silently shadows the platform's own settings. Opt in to
        # layering ours on top of theirs.
        if inherit_pegasusrc:
            rc_path = Path.home() / ".pegasusrc"
            if rc_path.exists():
                n = 0
                for line in rc_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "!")) or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    self.props[key.strip()] = value.strip()
                    n += 1
                logger.info("Inherited %d properties from %s", n, rc_path)
            else:
                logger.warning("--inherit-pegasusrc: no %s to inherit from",
                               rc_path)
        self.props["pegasus.transfer.threads"] = "16"

    def create_sites_catalog(self, exec_site_name="condorpool",
                             universe="container"):
        logger.info("Creating site catalog for execution site: %s (%s universe)",
                    exec_site_name, universe)
        self.sc = SiteCatalog()
        local = Site("local").add_directories(
            Directory(Directory.SHARED_SCRATCH, self.shared_scratch_dir).add_file_servers(
                FileServer("file://" + self.shared_scratch_dir, Operation.ALL)
            ),
            Directory(Directory.LOCAL_STORAGE, self.local_storage_dir).add_file_servers(
                FileServer("file://" + self.local_storage_dir, Operation.ALL)
            ),
        )
        # universe="container" (the default) hands the container to HTCondor
        # instead of having PegasusLite run apptainer itself; Pegasus stages the
        # image in as a data dependency and HTCondor's file transfer moves it to
        # the worker. Pegasus recommends it for every HTCondor pool, and it is
        # the *only* thing that works where the execution point is already an
        # unprivileged container (OSG / OSPool / PATh, which is what ACCESS
        # Pegasus provisions): a nested unprivileged apptainer cannot set up its
        # mount namespace and dies with "Failed to set mount propagation:
        # Permission denied" before the task starts. universe="vanilla" is the
        # fallback for a pool whose HTCondor predates container universe.
        exec_site = (
            Site(exec_site_name)
            .add_condor_profile(universe=universe)
            .add_pegasus_profile(style="condor")
        )
        self.sc.add_sites(local, exec_site)

    def create_replica_catalog(self):
        logger.info("Creating replica catalog")
        self.rc = ReplicaCatalog()

    def _declare_output(self, job, file_obj, stage_out=True, register=False):
        """Attach an output to a job and reuse an existing copy if we have one.

        This is the whole of the data-reuse mechanism: Pegasus prunes a job from
        the executable workflow when *every* one of its outputs already has a
        replica in the catalog. Declaring outputs through one place means
        --reuse-dir works for fetchers, the static branch, the zones and the
        map without each branch having to know about it.
        """
        lfn = file_obj.lfn
        self.produced.append(lfn)
        job.add_outputs(file_obj, stage_out=stage_out,
                        register_replica=register)
        if self.reuse_dir:
            path = os.path.join(self.reuse_dir, lfn)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                self.rc.add_replica("local", file_obj, path)
                self.reused.append(lfn)

    def reuse_summary(self):
        """What --reuse-dir actually matched, so surprises surface at generation."""
        if not self.reuse_dir:
            return None
        missing = [f for f in self.produced if f not in set(self.reused)]
        return {"reuse_dir": self.reuse_dir,
                "n_reused": len(self.reused),
                "n_total_outputs": len(self.produced),
                "missing": missing}

    def create_transformation_catalog(self, exec_site_name, container_image,
                                      use_container=True):
        logger.info("Creating transformation catalog")
        self.tc = TransformationCatalog()
        container = None
        if use_container:
            # A bare name means Docker Hub. A full URL is passed through, so a
            # SIF built elsewhere can be used directly — the way out when the
            # staging site has no apptainer to convert docker:// itself, or
            # when you do not want every run re-pulling from a registry.
            if "://" in container_image:
                image_url = container_image
                image_site = {"http": "web", "https": "web",
                              "file": "local"}.get(
                                  container_image.split("://", 1)[0],
                                  "docker_hub")
            else:
                image_url = "docker://" + container_image
                image_site = "docker_hub"
            container = Container(
                "cper_soilmoisture_container",
                container_type=Container.SINGULARITY,
                image=image_url,
                image_site=image_site,
            )
            self.tc.add_containers(container)
        else:
            # Some execution endpoints cannot run Apptainer at all - notably
            # unprivileged Kubernetes pods, where it fails with "Failed to set
            # mount propagation: Permission denied" before the task starts.
            # Pegasus stages the scripts themselves, so the only thing the
            # container provides is the Python dependency set; without it the
            # execution site must already supply them (see --no-container).
            logger.warning(
                "Container disabled: jobs will run in the execution site's "
                "native environment, which must already provide python3 with "
                "pandas, numpy and requests (plus rasterio for --mode static, "
                "and scikit-learn/matplotlib for --mode characterize)."
            )

        specs = [FETCHERS[s] for s in ALL_SOURCES] + [
            ("harmonize", "bin/harmonize.py", "2 GB"),
            ("soil_moisture_map", "bin/soil_moisture_map.py", "2 GB"),
            # M2 static branch
            ("fetch_soil_properties", "bin/fetch_soil_properties.py", "2 GB"),
            ("fetch_terrain", "bin/fetch_terrain.py", "2 GB"),
            ("build_covariates", "bin/build_covariates.py", "4 GB"),
            # M3 characterization branch
            ("station_response", "bin/station_response.py", "2 GB"),
            # All three station_similarity stages (cluster / attribute / merge)
            # peak at 0.17 GB measured. They used to ask for 2 GB, and with a
            # 9-way attribution fan-out that over-request became the binding
            # constraint on a 7.9 GB worker: it kept delineate_zones (4 GB) out
            # of a slot until the whole fan-out drained, cancelling the point of
            # taking attribution off the critical path.
            ("station_similarity", "bin/station_similarity.py", "1 GB"),
            ("visualize_response", "bin/visualize_response.py", "2 GB"),
            # M3 zone delineation + M4 dynamic map. These hold the whole
            # 49-band stack in memory at once (1017 x 1447 float32 ~ 290 MB)
            # plus float64 clustering workspaces. Requests are ~2x the
            # *measured* peak RSS on the real grid (1.94 / 1.09 / 0.49 GB),
            # not a guess: an over-request is not free, it is unschedulable.
            # A 8 GB ask sat Idle forever on 7.75 GB workers and stalled the
            # DAG with no error anywhere — check `condor_q -better-analyze`
            # before raising these.
            ("delineate_zones", "bin/delineate_zones.py", "4 GB"),
            ("estimate_soil_moisture", "bin/estimate_soil_moisture.py", "4 GB"),
            ("visualize_soil_moisture", "bin/visualize_soil_moisture.py", "2 GB"),
        ]
        for name, rel, mem in specs:
            kwargs = {"container": container} if container is not None else {}
            self.tc.add_transformations(
                Transformation(
                    name,
                    site=exec_site_name,
                    pfn=os.path.join(self.wf_dir, rel),
                    is_stageable=True,
                    **kwargs,
                ).add_pegasus_profile(memory=mem)
            )

    @staticmethod
    def _neon_months(config, start, end):
        """Months to request from NEON, clipped to what NEON actually has.

        Asks NEON which site-months exist rather than guessing from a
        configured start and publication lag: the metadata endpoints need no
        token (SPEC.md section 10), and a hardcoded lag silently drifts —
        DP1.00094.001 at CPER really is ~2 months behind, not the 1 a naive
        reading suggests. Falls back to the configured window when the
        catalogue is unreachable, so generation still works offline.
        """
        neon = config.get("neon", {})
        site = neon.get("site", "CPER")
        product = neon.get("products", {}).get("soil_moisture",
                                               "DP1.00094.001")
        api = neon.get("api_base", "https://data.neonscience.org/api/v0")
        lo, hi = start[:7], end[:7]

        available = None
        try:
            import urllib.request
            with urllib.request.urlopen(f"{api}/sites/{site}", timeout=30) as r:
                data = json.load(r)["data"]
            for p in data.get("dataProducts", []):
                if p.get("dataProductCode") == product:
                    available = sorted(p.get("availableMonths", []))
                    break
            if available:
                logger.info("NEON %s at %s: %d months published (%s .. %s)",
                            product, site, len(available),
                            available[0], available[-1])
        except Exception as exc:
            logger.warning("NEON availability lookup failed (%s); falling back "
                           "to the configured window", exc)

        if available is None:
            avail_from = neon.get("available_from")
            if not avail_from:
                return []
            lag = int(neon.get("publication_lag_months", 2))
            cursor = datetime.utcnow().replace(day=1)
            for _ in range(lag):
                cursor = (cursor - timedelta(days=1)).replace(day=1)
            available, cur = [], datetime.strptime(avail_from, "%Y-%m")
            while cur <= cursor:
                available.append(cur.strftime("%Y-%m"))
                cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

        return [m for m in available if lo <= m <= hi]

    def _window(self, args, config):
        """Resolve the fetch window for the selected mode."""
        analysis = config.get("analysis", {})
        if args.start_date and args.end_date:
            return args.start_date, args.end_date
        if args.mode == "nowcast":
            days = int(analysis.get("nowcast_window_days", 30))
            end = datetime.utcnow().date()
            return (args.start_date or str(end - timedelta(days=days)),
                    args.end_date or str(end))
        hw = analysis.get("historical_window", {})
        dr = config.get("date_range", {})
        return (args.start_date or hw.get("start") or dr.get("start"),
                args.end_date or hw.get("end") or dr.get("end")
                or str(datetime.utcnow().date()))

    def create_workflow(self, args, config):
        logger.info("Creating workflow (mode=%s)", args.mode)
        self.wf = Workflow(self.wf_name)
        branches = MODE_BRANCHES[args.mode]

        config_file = File(os.path.basename(args.config))
        self.rc.add_replica("local", config_file, os.path.abspath(args.config))
        common = File("drought_common.py")
        self.rc.add_replica(
            "local", common, os.path.join(self.wf_dir, "bin/drought_common.py")
        )

        if "fetch" in branches:
            self._add_fetch_only_branch(args, config, config_file)
            return

        harmonize_job, observations, points = None, None, None
        if "m1" in branches:
            harmonize_job, observations, points = self._add_observation_branch(
                args, config, config_file, common)
        covariates = None
        if "m2" in branches:
            # When M1 is present the covariate extraction is fed the harmonized
            # observations, so every depth-node gets its own covariate vector
            # from its own coordinates (NEON's five soil plots are hundreds of
            # metres apart and must not all inherit the tower's soil and
            # terrain).
            covariates = self._add_static_branch(
                config_file, harmonize_job, observations)
        characterization = None
        if "m3" in branches:
            characterization = self._add_characterization_branch(
                config, config_file, common, harmonize_job, observations,
                covariates)
        if "m4" in branches:
            self._add_map_branch(args, config, config_file, covariates,
                                 characterization, points)

    def _add_fetch_only_branch(self, args, config, config_file):
        """Download everything once; compute nothing.

        Outputs are staged out *and* registered so a later real run pointed at
        the same directory with --reuse-dir skips every fetcher. The NEON
        monthly files are staged out here even though a normal run keeps them
        on scratch — they are the whole point of this mode.
        """
        start, end = self._window(args, config)
        logger.info("Fetch-only DAG, window %s .. %s", start, end)
        self._add_fetch_jobs(args, config, config_file, start, end,
                             stage_monthly=True, register=True)
        self._add_raw_static_fetch(config_file, register=True)

    def _add_fetch_jobs(self, args, config, config_file, start, end,
                        stage_monthly=False, register=False):
        """Every in-situ fetcher. Returns (jobs, output files)."""
        dates = ["--start-date", start, "--end-date", end]
        fetch_jobs, source_obs = [], []
        for source in args.sources:
            transformation, _, _ = FETCHERS[source]

            # NEON serves one package per site-month, so a multi-year
            # characterization window is naturally a fan-out: one job per
            # month, downloading and parsing in parallel, instead of one job
            # serially walking 120 months. This is the shape Pegasus exists
            # for, and it is what makes a 10-year NEON characterization
            # practical.
            months = (self._neon_months(config, start, end)
                      if source == "neon" else [])
            if len(months) > MONTHLY_FANOUT_MIN:
                logger.info("NEON per-month fan-out: %d jobs (%s .. %s)",
                            len(months), months[0], months[-1])
                for month in months:
                    obs = File(f"neon_observations_{month}.csv")
                    job = Job(transformation, node_label=f"fetch_neon_{month}")
                    job.add_args("--config", config_file, "--month", month,
                                 "--output", obs)
                    job.add_inputs(config_file)
                    # Monthly files stay on scratch in a normal run (121 extra
                    # staged files buy nothing) but are staged out and
                    # registered in --mode fetch, where they are the product.
                    self._declare_output(job, obs, stage_out=stage_monthly,
                                         register=register)
                    job.add_dagman_profile(retry="2")
                    job.add_env(NEON_TOKEN=args.neon_token)
                    self.wf.add_jobs(job)
                    fetch_jobs.append(job)
                    source_obs.append(obs)
                continue

            obs = File(f"{source}_observations.csv")
            job = Job(transformation, node_label=f"fetch_{source}")
            job.add_args("--config", config_file, *dates, "--output", obs)
            job.add_inputs(config_file)
            self._declare_output(job, obs, stage_out=True, register=register)
            job.add_dagman_profile(retry="2")  # retry transient API failures
            if source == "neon":
                # HTCondor runs jobs in a clean environment: a token exported
                # in the submit shell never reaches the job. Captured at
                # generation time in main() and injected here.
                job.add_env(NEON_TOKEN=args.neon_token)
            self.wf.add_jobs(job)
            fetch_jobs.append(job)
            source_obs.append(obs)
        return fetch_jobs, source_obs

    def _add_observation_branch(self, args, config, config_file, common):
        start, end = self._window(args, config)
        if not start or not end:
            logger.error("Could not resolve a fetch window; set --start-date/"
                         "--end-date or config date_range/analysis blocks")
            sys.exit(1)
        logger.info("Fetch window: %s .. %s", start, end)

        fetch_jobs, source_obs = self._add_fetch_jobs(
            args, config, config_file, start, end)

        # --- Harmonize ---
        observations = File("observations.csv")
        report = File("harmonization_report.json")
        harmonize = Job("harmonize", node_label="harmonize")
        harmonize.add_args("--inputs", *source_obs,
                           "--output", observations, "--report", report)
        harmonize.add_inputs(common, *source_obs)
        self._declare_output(harmonize, observations)
        self._declare_output(harmonize, report)
        self.wf.add_jobs(harmonize)
        for f in fetch_jobs:
            self.wf.add_dependency(f, children=[harmonize])

        # --- Point-scale soil moisture ---
        sm_json = File("soil_moisture_points.json")
        sm_job = Job("soil_moisture_map", node_label="soil_moisture")
        # --as-of anchors staleness: nodes whose last observation is older
        # than analysis.max_current_age_days before the window end are
        # excluded from the "current" aggregates instead of being published
        # as fresh (e.g. USCRN Nunn, dark since 2026-05-28).
        sm_job.add_args("--observations", observations,
                        "--config", config_file, "--as-of", end,
                        "--output", sm_json)
        sm_job.add_inputs(common, config_file, observations)
        self._declare_output(sm_job, sm_json)
        self.wf.add_jobs(sm_job)
        self.wf.add_dependency(harmonize, children=[sm_job])
        return harmonize, observations, (sm_job, sm_json)

    def _add_raw_static_fetch(self, config_file, register=False):
        """The two raster fetchers. Returns (soil_job, polaris, sda, terrain_job, dem)."""
        polaris = File("polaris_soil.tif")
        sda = File("sda_soil.json")
        soil_job = Job("fetch_soil_properties", node_label="fetch_soil")
        soil_job.add_args("--config", config_file,
                          "--output-polaris", polaris, "--output-sda", sda)
        soil_job.add_inputs(config_file)
        self._declare_output(soil_job, polaris, register=register)
        self._declare_output(soil_job, sda, register=register)
        soil_job.add_dagman_profile(retry="2")

        dem = File("dem.tif")
        terrain_job = Job("fetch_terrain", node_label="fetch_terrain")
        terrain_job.add_args("--config", config_file, "--output", dem)
        terrain_job.add_inputs(config_file)
        self._declare_output(terrain_job, dem, register=register)
        terrain_job.add_dagman_profile(retry="2")
        self.wf.add_jobs(soil_job, terrain_job)
        return soil_job, polaris, sda, terrain_job, dem

    def _add_static_branch(self, config_file, harmonize_job, observations):
        """M2: soil + terrain -> covariate stack, registered for data reuse."""
        soil_job, polaris, sda, terrain_job, dem = self._add_raw_static_fetch(
            config_file)

        stack = File("covariates.tif")
        manifest = File("covariates_manifest.json")
        # Distinct LFN when node-enriched: the station-level file registered by
        # a `static` run and the node-level file a `characterize` run needs are
        # different products, and sharing one name would let data reuse hand a
        # characterize run the coarser table.
        stations = File("node_covariates.csv" if observations is not None
                        else "station_covariates.csv")
        build_job = Job("build_covariates", node_label="build_covariates")
        build_job.add_args("--config", config_file, "--dem", dem,
                           "--polaris", polaris, "--sda", sda,
                           "--output-stack", stack,
                           "--output-manifest", manifest,
                           "--output-stations", stations)
        build_job.add_inputs(config_file, dem, polaris, sda)
        if observations is not None:
            # --mode all: per-depth node coordinates from the harmonized
            # observations enrich the station_covariates table.
            build_job.add_args("--observations", observations)
            build_job.add_inputs(observations)
        # Registered so later characterize/nowcast runs reuse the static
        # products instead of rebuilding them (SPEC.md section 8, M2).
        for f in (stack, manifest, stations):
            self._declare_output(build_job, f, register=True)

        self.wf.add_jobs(build_job)
        self.wf.add_dependency(soil_job, children=[build_job])
        self.wf.add_dependency(terrain_job, children=[build_job])
        if harmonize_job is not None:
            self.wf.add_dependency(harmonize_job, children=[build_job])
        return build_job, stack, manifest, stations

    def _add_characterization_branch(self, config, config_file, common,
                                     harmonize_job, observations, covariates):
        """M3: per-station response fingerprints -> groups -> attribution."""
        build_job, cov_file = (covariates[0], covariates[3]) if covariates \
            else (None, None)

        # Fan out one response job per station declared in the config. Each
        # job fingerprints every depth-node belonging to that station, so a
        # NEON plot with six sensor depths costs one job, not six.
        stations = [s["id"] for s in config.get("stations", [])]
        if not stations:
            logger.error("No stations in config; cannot characterize")
            sys.exit(1)
        logger.info("Characterization fan-out: %d stations", len(stations))

        response_files, response_jobs = [], []
        for station in stations:
            safe = station.replace(":", "_")
            out = File(f"response_{safe}.json")
            job = Job("station_response", node_label=f"response_{safe}")
            job.add_args("--observations", observations, "--station", station,
                         "--config", config_file, "--output", out)
            job.add_inputs(common, config_file, observations)
            self._declare_output(job, out)
            self.wf.add_jobs(job)
            self.wf.add_dependency(harmonize_job, children=[job])
            response_files.append(out)
            response_jobs.append(job)

        # --- Clustering: cheap (~1 s) and, critically, the only thing the zone
        # and M4 branches actually need from here. Keeping it separate takes
        # the ~11-minute attribution off the critical path entirely; only
        # visualize_response reads station_similarity.json.
        clusters = File("station_clusters.json")
        groups = File("station_groups.csv")
        cluster_job = Job("station_similarity", node_label="similarity_cluster")
        cluster_job.add_args("--stage", "cluster",
                             "--fingerprints", *response_files,
                             "--config", config_file,
                             "--output", clusters, "--output-groups", groups)
        cluster_job.add_inputs(*response_files, config_file)
        self._declare_output(cluster_job, clusters)
        self._declare_output(cluster_job, groups)
        self.wf.add_jobs(cluster_job)
        for j in response_jobs:
            self.wf.add_dependency(j, children=[cluster_job])

        # --- Attribution: one job per response metric. Each is an independent
        # random-forest fit plus a grouped cross-validation, so the axis is
        # embarrassingly parallel and a 9-way fan-out fits one scheduling wave
        # on a modest pool.
        metrics = response_metrics()
        logger.info("Attribution fan-out: %d response metrics", len(metrics))
        attr_files, attr_jobs = [], []
        for metric in metrics:
            out = File(f"attribution_{metric}.json")
            job = Job("station_similarity", node_label=f"attribute_{metric}")
            job.add_args("--stage", "attribute", "--metrics", metric,
                         "--fingerprints", *response_files,
                         "--config", config_file, "--output", out)
            job.add_inputs(*response_files, config_file)
            if cov_file is not None:
                job.add_args("--covariates", cov_file)
                job.add_inputs(cov_file)
            self._declare_output(job, out)
            self.wf.add_jobs(job)
            for j in response_jobs:
                self.wf.add_dependency(j, children=[job])
            if build_job is not None:
                self.wf.add_dependency(build_job, children=[job])
            attr_files.append(out)
            attr_jobs.append(job)

        # --- Merge back into the single artifact everything downstream expects.
        similarity = File("station_similarity.json")
        sim_job = Job("station_similarity", node_label="similarity_merge")
        sim_job.add_args("--stage", "merge", "--clusters", clusters,
                         "--attributions", *attr_files,
                         "--output", similarity)
        sim_job.add_inputs(clusters, *attr_files)
        self._declare_output(sim_job, similarity)
        self.wf.add_jobs(sim_job)
        self.wf.add_dependency(cluster_job, children=[sim_job])
        for j in attr_jobs:
            self.wf.add_dependency(j, children=[sim_job])

        figure = File("station_characterization.png")
        index = File("figure_index.json")
        viz_job = Job("visualize_response", node_label="visualize_response")
        viz_job.add_args("--groups", groups, "--similarity", similarity,
                         "--output", figure, "--output-index", index)
        viz_job.add_inputs(groups, similarity)
        if cov_file is not None:
            viz_job.add_args("--covariates", cov_file)
            viz_job.add_inputs(cov_file)
        self._declare_output(viz_job, figure)
        self._declare_output(viz_job, index)
        self.wf.add_jobs(viz_job)
        self.wf.add_dependency(sim_job, children=[viz_job])
        self.wf.add_dependency(cluster_job, children=[viz_job])
        return {"response_jobs": response_jobs, "response_files": response_files,
                # cluster_job, not sim_job: the zone/M4 chain consumes only
                # station_groups.csv, so it must not wait on the attribution.
                "cluster_job": cluster_job, "sim_job": sim_job,
                "groups": groups, "similarity": similarity}

    def _add_map_branch(self, args, config, config_file, covariates,
                        characterization, points):
        """M4: covariate zones -> gridded estimate + uncertainty -> map.

        Requires both the static stack (M2) and the fingerprints (M3): the zones
        come from the covariate grid, the climatology each anomaly is measured
        against comes from the fingerprints, and the current values come from
        the point-scale layer. All three are already in the DAG whenever this
        branch is generated.
        """
        if not covariates or not characterization or not points:
            logger.error("M4 needs the M1, M2 and M3 branches; got "
                         "m1=%s m2=%s m3=%s", bool(points), bool(covariates),
                         bool(characterization))
            sys.exit(1)
        build_job, stack, manifest, cov_file = covariates
        sm_job, sm_json = points
        _, end = self._window(args, config)

        # --- Zone delineation (the M3 deliverable M4 stands on) ---
        zones = File("zones.tif")
        zones_geojson = File("soil_moisture_zones.geojson")
        zone_stats = File("zone_stats.json")
        zone_members = File("station_zones.csv")
        zone_job = Job("delineate_zones", node_label="delineate_zones")
        zone_job.add_args("--covariates", stack, "--manifest", manifest,
                          "--config", config_file,
                          "--groups", characterization["groups"],
                          "--output-zones", zones,
                          "--output-geojson", zones_geojson,
                          "--output-stats", zone_stats,
                          "--output-membership", zone_members)
        zone_job.add_inputs(stack, manifest, config_file,
                            characterization["groups"])
        # Registered like the rest of the static baseline: a nowcast run reuses
        # the zones rather than re-clustering 1.5 M pixels every few hours.
        for f in (zones, zones_geojson, zone_stats, zone_members):
            self._declare_output(zone_job, f, register=True)
        self.wf.add_jobs(zone_job)
        self.wf.add_dependency(build_job, children=[zone_job])
        # Deliberately the cluster job, not the merge: zones need only
        # station_groups.csv, so the whole M4 chain runs alongside the
        # attribution fan-out instead of queueing behind it.
        self.wf.add_dependency(characterization["cluster_job"],
                               children=[zone_job])

        # --- Upscaling ---
        now_tif = File("soil_moisture_now.tif")
        unc_tif = File("soil_moisture_uncertainty.tif")
        now_json = File("soil_moisture_now.json")
        skill = File("estimation_skill.json")
        est_job = Job("estimate_soil_moisture", node_label="estimate")
        est_job.add_args("--points", sm_json, "--zones", zones,
                         "--zone-stats", zone_stats, "--covariates", stack,
                         "--manifest", manifest,
                         "--fingerprints", *characterization["response_files"],
                         "--config", config_file, "--as-of", end,
                         "--output-map", now_tif,
                         "--output-uncertainty", unc_tif,
                         "--output-json", now_json,
                         "--output-skill", skill)
        est_job.add_inputs(sm_json, zones, zone_stats, stack, manifest,
                           config_file, *characterization["response_files"])
        for f in (now_tif, unc_tif, now_json, skill):
            self._declare_output(est_job, f)
        self.wf.add_jobs(est_job)
        self.wf.add_dependency(zone_job, children=[est_job])
        self.wf.add_dependency(sm_job, children=[est_job])
        for j in characterization["response_jobs"]:
            self.wf.add_dependency(j, children=[est_job])

        # --- Visualisation ---
        map_png = File("soil_moisture_map.png")
        map_html = File("soil_moisture_map.html")
        mviz = Job("visualize_soil_moisture", node_label="visualize_map")
        mviz.add_args("--map", now_tif, "--uncertainty", unc_tif,
                      "--zones", zones, "--geojson", zones_geojson,
                      "--zone-stats", zone_stats, "--now-json", now_json,
                      "--skill", skill, "--points", sm_json,
                      "--fingerprints", *characterization["response_files"],
                      "--output-figure", map_png, "--output-html", map_html)
        mviz.add_inputs(now_tif, unc_tif, zones, zones_geojson, zone_stats,
                        now_json, skill, sm_json,
                        *characterization["response_files"])
        self._declare_output(mviz, map_png)
        self._declare_output(mviz, map_html)
        self.wf.add_jobs(mviz)
        self.wf.add_dependency(est_job, children=[mviz])
        self.wf.add_dependency(zone_job, children=[mviz])


def main():
    parser = argparse.ArgumentParser(
        description="Generate the Pegasus workflow for CPER soil-moisture mapping"
    )
    parser.add_argument("--config", default="site_config.json",
                        help="Site/station configuration JSON")
    parser.add_argument("--mode", default="all", choices=list(MODES),
                        help="How much of the pipeline to build. Default 'all' "
                             "runs everything and ends in the gridded map; "
                             "the others scale back (SPEC.md section 7). "
                             "'nowcast' is 'all' on a short window, meant to "
                             "be paired with --reuse-dir; 'fetch' downloads "
                             "and registers inputs without computing anything.")
    parser.add_argument("--sources", nargs="+", default=["uscrn", "awdb", "neon"],
                        choices=list(ALL_SOURCES),
                        help="Data sources to fetch. uscrn+awdb are "
                             "credential-free; neon needs NEON_TOKEN exported "
                             "at generation time; sage currently returns empty "
                             "at CPER (no public data).")
    parser.add_argument("--start-date", help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Override end date (YYYY-MM-DD)")
    parser.add_argument("-e", "--execution-site-name", default="condorpool",
                        help="HTCondor pool name for execution")
    parser.add_argument("--container-image",
                        default="pegasus/cper-soilmoisture:m3",
                        help="Docker container image for workflow jobs "
                             "(:m3 adds scikit-learn/matplotlib; SPEC.md "
                             "'Container': new tag, don't mutate :latest)")
    parser.add_argument("--exec-universe", default=None,
                        choices=["vanilla", "container"],
                        help="HTCondor universe for the execution site. "
                             "Default 'container': HTCondor starts the "
                             "container and PegasusLite runs inside it, which "
                             "is what Pegasus recommends for every HTCondor "
                             "pool and the only thing that works where the "
                             "execution point is itself an unprivileged "
                             "container (OSG / OSPool / PATh, i.e. ACCESS "
                             "Pegasus) — there, a nested apptainer dies with "
                             "'Failed to set mount propagation: Permission "
                             "denied'. 'vanilla' has PegasusLite launch "
                             "apptainer itself; use it if the pool's HTCondor "
                             "is too old for container universe. Implied by "
                             "--no-container.")
    parser.add_argument("--no-sites-catalog", action="store_true",
                        help="Do not write sites.yml; plan against the site "
                             "catalog the submit host already provides (ACCESS "
                             "Pegasus points at one from ~/.pegasusrc via "
                             "pegasus.catalog.site.repo.file). Pair it with "
                             "--inherit-pegasusrc and pass that platform's own "
                             "site name to -e.")
    parser.add_argument("--inherit-pegasusrc", action="store_true",
                        help="Seed the generated pegasus.properties from "
                             "~/.pegasusrc before adding ours. Without this, "
                             "the generated file SHADOWS ~/.pegasusrc entirely "
                             "— pegasus-plan reads one or the other, not both.")
    parser.add_argument("--no-container", action="store_true",
                        help="Run jobs in the execution site's native "
                             "environment instead of the container. Use when "
                             "the endpoint cannot run Apptainer (e.g. an "
                             "unprivileged Kubernetes pod failing with "
                             "'Failed to set mount propagation'). The site "
                             "must then already provide the Python deps.")
    parser.add_argument("--output-dir", metavar="DIR",
                        help="Where stage-out lands (default ./output). Give "
                             "a --mode fetch run its own directory — "
                             "'--mode fetch --output-dir inputs' — and then "
                             "'--reuse-dir inputs' reuses the downloads while "
                             "recomputing everything else, however many times "
                             "you re-run. Reusing a directory that also holds "
                             "computed products prunes those as well.")
    parser.add_argument("--reuse-dir", metavar="DIR",
                        help="Directory of already-fetched/computed products "
                             "(e.g. the output/ directory of a --mode fetch "
                             "run). Every declared output found there is "
                             "registered as a replica, so Pegasus data reuse "
                             "prunes the job that would have produced it. This "
                             "is how you download once and run many times.")
    parser.add_argument("-o", "--output", default="workflow.yml",
                        help="Output workflow file")
    args = parser.parse_args()

    args.sources = [s for s in ALL_SOURCES if s in args.sources]

    if args.reuse_dir and args.mode == "fetch":
        logger.warning("--reuse-dir is ignored in --mode fetch: reusing the "
                       "very files this mode exists to download would prune "
                       "the entire DAG.")
        args.reuse_dir = None
    if args.reuse_dir and not os.path.isdir(args.reuse_dir):
        logger.error("--reuse-dir %s is not a directory", args.reuse_dir)
        sys.exit(1)
    if args.mode == "nowcast" and not args.reuse_dir:
        # M4 measures each station's anomaly against its own monthly
        # climatology, which station_response derives from the fetched record.
        # Over a 30-day window that "climatology" is the current month itself,
        # so every anomaly collapses toward zero and the map degenerates to
        # climatology. A nowcast is meant to reuse the fingerprints and zones a
        # characterize/all run already registered.
        logger.warning(
            "--mode nowcast without --reuse-dir will recompute the response "
            "fingerprints from the short nowcast window, so the 'climatology' "
            "each anomaly is measured against becomes that same window and the "
            "map collapses toward climatology. Run --mode all (or "
            "characterize) once, then nowcast with --reuse-dir <output-dir>.")

    # Credential capture at generation time (never default a missing credential
    # to "": skip the source loudly instead, so the problem surfaces before
    # submit rather than as a 403 on a worker node).
    args.neon_token = os.environ.get("NEON_TOKEN")
    if args.mode != "static":
        if "neon" in args.sources and not args.neon_token:
            logger.warning(
                "NEON selected but NEON_TOKEN is not set in this environment; "
                "dropping the NEON source. Get a free token at "
                "data.neonscience.org (user profile), export NEON_TOKEN, and "
                "regenerate to include NEON."
            )
            args.sources = [s for s in args.sources if s != "neon"]
        if not args.sources:
            logger.error("No usable sources selected")
            sys.exit(1)

    # --exec-universe defaults to 'container' (see the flag's help), but there
    # is nothing for that universe to start when --no-container removed the
    # container, so that flag falls back to vanilla rather than erroring.
    if args.no_container:
        if args.exec_universe == "container":
            logger.error("--exec-universe container and --no-container "
                         "contradict each other: the container universe exists "
                         "to run the container. Pick one.")
            sys.exit(1)
        args.exec_universe = "vanilla"
    elif args.exec_universe is None:
        args.exec_universe = "container"
    if args.exec_universe == "container" and args.no_sites_catalog:
        logger.warning("--exec-universe container has no effect together with "
                       "--no-sites-catalog: the universe is a site-catalog "
                       "profile, so the submit host's own catalog decides.")

    if not os.path.exists(args.config):
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)
    with open(args.config) as fh:
        config = json.load(fh)

    try:
        wf = CperSoilMoistureWorkflow(dagfile=args.output,
                                      reuse_dir=args.reuse_dir,
                                      output_dir=args.output_dir)
        wf.create_pegasus_properties(
            inherit_pegasusrc=args.inherit_pegasusrc)
        if args.no_sites_catalog:
            logger.info("Not writing sites.yml: planning will use the submit "
                        "host's own site catalog. Site '%s' must exist there.",
                        args.execution_site_name)
        else:
            wf.create_sites_catalog(exec_site_name=args.execution_site_name,
                                    universe=args.exec_universe)
        wf.create_replica_catalog()
        wf.create_transformation_catalog(
            exec_site_name=args.execution_site_name,
            container_image=args.container_image,
            use_container=not args.no_container,
        )
        wf.create_workflow(args, config)
        wf.write()

        logger.info("\n" + "=" * 70)
        logger.info("WORKFLOW GENERATION COMPLETE")
        logger.info("=" * 70)
        logger.info("  Workflow file: %s", args.output)
        logger.info("  Mode: %s (%s)", args.mode,
                    ", ".join(BRANCH_LABELS.get(b, b)
                              for b in sorted(MODE_BRANCHES[args.mode])))
        logger.info("  Sources: %s", ", ".join(args.sources))
        logger.info("  Outputs: %s", wf.local_storage_dir)
        logger.info("  Execution: site %s, %s", args.execution_site_name,
                    "site catalog from the submit host"
                    if args.no_sites_catalog
                    else "%s universe, container %s" % (
                        args.exec_universe,
                        "disabled" if args.no_container else args.container_image))
        logger.info("  Jobs: %d", len(wf.wf.jobs))
        summary = wf.reuse_summary()
        if summary:
            logger.info("  Data reuse: %d of %d outputs already present in %s",
                        summary["n_reused"], summary["n_total_outputs"],
                        summary["reuse_dir"])
            if summary["n_reused"] == 0:
                logger.warning("  --reuse-dir matched nothing. Pegasus prunes a "
                               "job only when EVERY one of its outputs is in "
                               "the replica catalog, so nothing will be "
                               "skipped. Check the directory holds the LFNs "
                               "verbatim (e.g. neon_observations_2016-07.csv).")
            else:
                logger.info("  Pegasus will prune the producing jobs during "
                            "planning; `pegasus-plan --force` disables that.")
        logger.info("\nNext steps:")
        logger.info("  1. Review workflow: %s", args.output)
        logger.info("  2. Submit: pegasus-plan --submit -s %s -o local %s",
                    args.execution_site_name, args.output)
        logger.info("  3. Monitor: pegasus-status <submit_dir>")
        if args.mode == "fetch":
            logger.info("\nThis is a download-only DAG. When it finishes, run "
                        "the real workflow against its outputs:")
            logger.info("  ./workflow_generator.py --reuse-dir %s -o workflow.yml",
                        wf.local_storage_dir)
        logger.info("=" * 70 + "\n")
    except Exception as exc:
        logger.error("Failed to generate workflow: %s", exc)
        raise


if __name__ == "__main__":
    main()
