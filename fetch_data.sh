#!/usr/bin/env bash
#
# fetch_data.sh — download every workflow input once, into output/.
#
# Run this before the workflow. Afterwards, point a run at the directory:
#
#     ./fetch_data.sh
#     python3 workflow_generator.py --reuse-dir output -o workflow.yml
#     pegasus-plan --submit -s condorpool -o local workflow.yml
#
# Pegasus registers everything it finds in --reuse-dir as a replica and prunes
# the job that would have produced it, so the run starts at `harmonize` instead
# of re-downloading 121 NEON site-months.
#
# The work is not hardcoded here: the script asks workflow_generator.py for the
# download-only DAG and then executes exactly the jobs that DAG contains. Add a
# source or change the NEON month list and this script follows automatically -
# there is no second copy of the fetch logic to drift out of sync.
#
# Needs no Pegasus, no HTCondor and no container: these are plain Python
# scripts. If you do have a pool, `--mode fetch` runs the same downloads in
# parallel across it instead (see README).
#
# Usage:
#     ./fetch_data.sh                       # full historical window
#     ./fetch_data.sh --jobs 12             # more concurrency
#     ./fetch_data.sh --start-date 2024-01-01 --end-date 2024-06-30
#     ./fetch_data.sh --sources awdb        # skip NEON/USCRN
#
set -euo pipefail
cd "$(dirname "$0")"

JOBS=6
OUT=output
IMAGE=kthare10/cper-soilmoisture:m3
RUNNER=auto
GEN_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --jobs)   JOBS=$2; shift 2 ;;
        --output-dir) OUT=$2; shift 2 ;;
        --container) RUNNER=container; shift ;;
        --no-container) RUNNER=local; shift ;;
        --container-image) IMAGE=$2; RUNNER=container; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *)        GEN_ARGS+=("$1"); shift ;;
    esac
done

if [ -d .venv ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Decide how to run the fetchers BEFORE launching 121 of them. These are plain
# Python scripts, not containerised jobs, so an interpreter without pandas
# fails every single one identically - which is exactly what a submit host
# looks like, since generation needs only pegasus-wms.api.
DEPS='import pandas, numpy, requests, rasterio'
if [ "$RUNNER" = auto ]; then
    if python3 -c "$DEPS" >/dev/null 2>&1; then
        RUNNER=local
    elif command -v apptainer >/dev/null 2>&1 || command -v singularity >/dev/null 2>&1; then
        RUNNER=container
        echo "==> This interpreter has no pandas/rasterio; using the container instead"
    else
        cat >&2 <<EOF
ERROR: cannot run the fetchers here.

  This host's python3 is missing pandas/numpy/requests/rasterio, and there is
  no apptainer/singularity to fall back on. Pick one:

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

  ...or install apptainer and re-run, and the container will be used.
EOF
        exit 1
    fi
fi

APPTAINER=$(command -v apptainer || command -v singularity || true)
if [ "$RUNNER" = container ]; then
    [ -n "$APPTAINER" ] || { echo "ERROR: --container asked for but no apptainer/singularity found" >&2; exit 1; }
    echo "==> Fetchers will run in $IMAGE via $(basename "$APPTAINER")"
    if ! "$APPTAINER" exec "docker://$IMAGE" python3 -c "$DEPS" >/dev/null 2>&1; then
        echo "ERROR: cannot run $IMAGE (pull failed, or it lacks the deps)." >&2
        echo "       Build and push it first - see README 'Build the container'." >&2
        echo "       A stale ~/.docker/config.json is a common cause; see Troubleshooting." >&2
        exit 1
    fi
else
    python3 -c "$DEPS" 2>/dev/null || {
        echo "ERROR: this python3 lacks pandas/numpy/requests/rasterio." >&2
        echo "       pip install -r requirements.txt, or use --container." >&2
        exit 1
    }
fi

if [ -z "${NEON_TOKEN:-}" ]; then
    echo "WARNING: NEON_TOKEN is not set — NEON will be skipped and you will"
    echo "         get SCAN/USCRN only. Free token: data.neonscience.org profile."
    echo
fi

mkdir -p "$OUT"
DAG=$(mktemp -t fetch_dag.XXXXXX.yml)
trap 'rm -f "$DAG"' EXIT

echo "==> Working out what needs downloading"
python3 workflow_generator.py --mode fetch -o "$DAG" "${GEN_ARGS[@]}" 2>&1 \
    | grep -E "Fetch window|fan-out|Jobs:|WARNING|ERROR" || true

echo
echo "==> Downloading into $OUT/ with $JOBS parallel workers"
OUT="$OUT" JOBS="$JOBS" RUNNER="$RUNNER" IMAGE="$IMAGE" APPTAINER="$APPTAINER" \
python3 - "$DAG" <<'PY'
import os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

dag, out, jobs = sys.argv[1], os.environ["OUT"], int(os.environ["JOBS"])
wf = yaml.safe_load(open(dag))

# Map transformation -> the script Pegasus would have staged, straight from the
# transformation catalog the generator just wrote, so the two cannot disagree.
tc = yaml.safe_load(open("transformations.yml"))
script = {t["name"]: t["sites"][0]["pfn"] for t in tc["transformations"]}

tasks = []
for job in wf["jobs"]:
    args = [str(a) for a in job.get("arguments", [])]
    # Rewrite the declared outputs to land in the reuse directory. Everything
    # else (config path, --month, dates) is used exactly as the DAG has it.
    outs = {u["lfn"] for u in job.get("uses", []) if u["type"] == "output"}
    args = [os.path.join(out, a) if a in outs else a for a in args]
    tasks.append((job.get("nodeLabel") or job["name"], script[job["name"]], args,
                  sorted(outs)))

done_already = [t for t in tasks
                if all(os.path.getsize(os.path.join(out, f)) > 0
                       for f in t[3] if os.path.exists(os.path.join(out, f)))
                and all(os.path.exists(os.path.join(out, f)) for f in t[3])]
todo = [t for t in tasks if t not in done_already]
if done_already:
    print("   %d already present, skipping (delete to refetch)" % len(done_already))
if not todo:
    print("   nothing to do")
    sys.exit(0)

failed, t0 = [], time.time()


runner = os.environ["RUNNER"]
image, apptainer = os.environ["IMAGE"], os.environ["APPTAINER"]
out_abs = os.path.abspath(out)


def command(path, args):
    if runner != "container":
        return [sys.executable, path] + args
    # Bind the workflow dir (scripts + config) and the output dir explicitly:
    # the latter is often outside $PWD and apptainer will not guess it.
    return [apptainer, "exec",
            "--bind", "%s:%s" % (os.getcwd(), os.getcwd()),
            "--bind", "%s:%s" % (out_abs, out_abs),
            "docker://" + image, "python3", path] + args


def run(task):
    label, path, args, _ = task
    p = subprocess.run(command(path, args), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    return label, p.returncode, p.stdout or ""


with ThreadPoolExecutor(max_workers=jobs) as pool:
    futures = {pool.submit(run, t): t for t in todo}
    for i, fut in enumerate(as_completed(futures), 1):
        label, rc, log = fut.result()
        if rc != 0:
            failed.append((label, log))
        print("   [%3d/%3d] %-34s %s" % (i, len(todo), label,
                                         "ok" if rc == 0 else "FAILED"),
              flush=True)

print("\n   %d/%d succeeded in %.0f s" % (len(todo) - len(failed), len(todo),
                                          time.time() - t0))
if failed:
    # A fetcher that writes an empty file and exits 0 is a *successful*
    # best-effort run (a dead station, an empty window). A non-zero exit is a
    # real failure and is worth showing.
    print("\n   failures:")
    for label, log in failed[:5]:
        tail = [l for l in log.strip().splitlines() if l.strip()][-3:]
        print("     %s" % label)
        for line in tail:
            print("       %s" % line)
    sys.exit(1)
PY

echo
echo "==> Downloaded into $OUT/"
find "$OUT" -maxdepth 1 -type f ! -name '.*' | wc -l | sed 's/^/    files: /'
find "$OUT" -maxdepth 1 -name 'neon_observations_*.csv' | wc -l \
    | sed 's/^/    NEON site-months: /'
du -sh "$OUT" | awk '{print "    size:  " $1}'

cat <<EOF

Next:
    python3 workflow_generator.py --reuse-dir $OUT -o workflow.yml
    pegasus-plan --submit -s condorpool -o local workflow.yml

Empty files are expected for a source with no data in the window (a dark
station, for instance) — harmonize fails only if *every* source is empty.
EOF
