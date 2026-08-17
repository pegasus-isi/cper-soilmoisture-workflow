# Building the workflow containers with Apptainer

> This is a **shared reference kept in sync across the Pegasus workflow repos**
> (airquality, cper-soilmoisture, crophealth, drought, earthquake, eht, gwas-qc,
> mag, nextgen, proteinfold, quantumchem, rnaseq, soilmoisture, tnseq). It is
> copied into each repo so a single-repo clone is self-contained, which is why the
> tables below cover workflows you may not have checked out. Commands that name a
> specific workflow directory are examples — substitute your own.

All 14 workflows define their containers as Apptainer definition files under
`<workflow>/Apptainer/*.def`, and their `workflow_generator.py` defaults to a
locally built `.sif`:

```python
container = Container(
    "my_container",
    container_type=Container.SINGULARITY,
    image="file:///abs/path/to/My_Container.sif",
    image_site="local",   # the site where the .sif physically lives
)
```

Pegasus stages the `.sif` like any other input file, so there is no registry
push/pull step and no Docker Hub login on the submit host.

The original `Docker/` directories are kept as-is. They are no longer the
supported path, but they remain a working fallback and the published
`kthare10/*` images are still valid.

## You cannot build these on a Mac

Two separate reasons, and the second is the one that actually bites:

1. **Apptainer needs a Linux kernel** — user namespaces, loop devices,
   squashfs/overlayfs. There is no macOS port. You can install it inside a Linux
   VM (Colima, Lima, UTM), which is what upstream recommends.

2. **Architecture.** An Apple Silicon Mac and any VM on it are `aarch64`. A
   `.sif` has **no multi-arch manifest** — one file, one architecture — so an
   image built there simply will not exec on an `x86_64` worker node. This is
   different from Docker, where one tag could serve both.

Several definitions are hard-bound to `x86_64` regardless of where you build
them, because they fetch architecture-specific release artifacts:

| Definition | x86_64-only because |
|---|---|
| `gwas-qc-workflow/Apptainer/GWAS_QC_Container.def` | `plink_linux_x86_64_*.zip` |
| `tnseq-workflow/Apptainer/Tnseq_Container.def` | `seqkit_linux_amd64.tar.gz` |
| `nextgen-workflow/Apptainer/Teehr_Container.def` | base tag `awiciroh/ngiab-teehr:x86` |

Forcing an `x86_64` build under qemu emulation (`colima start --arch x86_64`, or
binfmt) technically works but is very slow, and the conda/micromamba solves in
the `mag` and `rnaseq` definitions are unreliable under it. Don't.

## Recommended workflow: author locally, build remotely

The `.def` files are plain text — edit them on the Mac. Build on a host whose
architecture matches your worker nodes (FABRIC `pegasus2`, a Chameleon node, any
`x86_64` Linux box with Apptainer). `apptainer build` needs no root.

```sh
# 1. Sync the workflow to the build/submit host
rsync -av --exclude .venv --exclude scratch --exclude output \
    earthquake-workflow/ pegasus2:~/earthquake-workflow/

# 2. Build there. Run from the WORKFLOW ROOT, not from inside Apptainer/ —
#    %files source paths resolve against the invocation directory, exactly like
#    Docker's build context.
ssh pegasus2
cd ~/earthquake-workflow
apptainer build Apptainer/Earthquake_Container.sif Apptainer/Earthquake_Container.def

# 3. Verify before submitting
apptainer exec Apptainer/Earthquake_Container.sif python -c "import pandas, sklearn; print('ok')"
apptainer exec Apptainer/Earthquake_Container.sif which curl wget   # PegasusLite needs both

# 4. Generate and submit — the generator finds the .sif by default
./workflow_generator.py --regions california --start-date 2024-01-01 -o workflow.yml
pegasus-plan --submit -s condorpool -o local workflow.yml
```

If the `.sif` lives somewhere other than `<workflow>/Apptainer/`, every generator
takes an override flag (see the table below).

## Publishing images to ghcr.io (optional)

Nothing requires a registry — Pegasus stages the `.sif` from the submit host. But
publishing is worth it when you want one build shared by a team, an immutable
artifact to cite in a paper, or a cache so worker hosts don't each rebuild.

GitHub Container Registry accepts a `.sif` as an **OCI artifact** via ORAS, which
Apptainer speaks natively.

### One-time: authenticate

Create a GitHub personal access token (classic) with **`write:packages`** (and
`read:packages`); `repo` scope alone is not enough. Then:

```sh
# Avoid putting the token in your shell history or in argv
export GHCR_TOKEN=...          # or: read -rs GHCR_TOKEN
echo "$GHCR_TOKEN" | apptainer registry login --username <github-username> \
    --password-stdin oras://ghcr.io
```

`apptainer remote list` shows what you are logged in to. Older Singularity builds
use `singularity remote login`.

### Push

Tag with the owner that should own the package — your user or the org:

```sh
cd earthquake-workflow

# Immutable, reproducible tag: the git SHA of the definition file
TAG=$(git rev-parse --short HEAD)
apptainer push Apptainer/Earthquake_Container.sif \
    oras://ghcr.io/pegasus-isi/earthquake-workflow:$TAG

# Optionally also move a floating tag
apptainer push Apptainer/Earthquake_Container.sif \
    oras://ghcr.io/pegasus-isi/earthquake-workflow:latest
```

Lowercase only — ghcr.io rejects uppercase in the path. New packages are
**private** by default; make them public under *Package settings → Change
visibility*, or keep them private and have consumers `apptainer registry login`
first. Link the package to its repo from the same settings page so it inherits
the repo's README and permissions.

### Consume on the submit host

Pull it back to the path the generator already expects, and everything downstream
is unchanged:

```sh
cd earthquake-workflow
apptainer pull Apptainer/Earthquake_Container.sif \
    oras://ghcr.io/pegasus-isi/earthquake-workflow:$TAG
./workflow_generator.py --regions california --start-date 2024-01-01 -o workflow.yml
```

### Do not put `oras://` in the transformation catalog

Pegasus documents these `image` schemes: `docker://`, `shub://`, `library://`,
`shifter://`, and a `file://` URL to an exported image. **`oras://` is not among
them** — the Python API passes `image` through as an opaque string, so an
`oras://` URL is accepted at generation time and then fails in the planner or on
the worker, which is a slow way to find out.

So treat ghcr.io as a distribution channel, not a runtime source: `apptainer pull`
on the submit host, then let Pegasus stage the local `.sif` as it already does.

If you specifically want Pegasus to resolve the image itself, the supported
registry route is Sylabs Cloud (`library://`) rather than ghcr.io, or push a real
OCI **image** (not a SIF artifact) and reference it as
`docker://ghcr.io/owner/name:tag` with `image_site="docker_hub"`. That second
route means maintaining the `Docker/` build path, which is what this migration
moved away from.

### A note on architecture

A pushed `.sif` is still single-architecture. If you publish both, encode it in
the tag (`:$TAG-amd64`, `:$TAG-arm64`) — ORAS artifacts get no automatic
multi-arch manifest, so nothing will pick the right one for you.

## Definition files and generator flags

| Workflow | Definition file(s) | Generator flag |
|---|---|---|
| airquality | `AirQuality_Forecast_Container.def` | `--container-sif` |
| cper-soilmoisture | `CPER_SoilMoisture_Container.def` | `--container-image` |
| crophealth | `CropHealth_Container.def` | `--container-sif` |
| drought | `Drought_Container.def` | `--container-sif` |
| earthquake | `Earthquake_Container.def` | `--container-sif` |
| eht | `eht-difmap.def`, `eht-ehtim.def`, `eht-rex.def`, `eht-smili.def` | `--sif-dir` |
| gwas-qc | `GWAS_QC_Container.def` | `--container-sif` |
| mag | `MAG_Container.def` | `--container-image` |
| nextgen | `NextGen_Container.def`, `Teehr_Container.def` | `--container-image`, `--teehr-image` |
| proteinfold | `ProteinFold_Py_Container.def`, `ColabFold_Container.def`, `AlphaFold3_Container.def`, `Boltz_Container.def` | `--py-sif`, `--colabfold-sif`, `--alphafold3-sif`, `--boltz-sif` |
| quantumchem | `QuantumChem_VQE_Container.def`, `QuantumChem_Classical_Container.def` | edit `CONTAINER_SIF` / `CLASSICAL_SIF` |
| rnaseq | `RNASeq_Container.def` | `--container-sif` |
| soilmoisture | `SoilMoisture_Container.def` | `--container-sif` |
| tnseq | `Tnseq_Container.def` | `--container-sif` |

`cper-soilmoisture`, `mag`, `nextgen` and `eht` still accept a registry
reference on the same flag, so the old Docker Hub path keeps working:

```sh
# mag: bare name -> docker://kthare10/mag-workflow:latest, image_site=docker_hub
./workflow_generator.py --container-image kthare10/mag-workflow:latest ...

# eht: clear --sif-dir to pull the four images from Docker Hub instead
./workflow_generator.py --sif-dir '' ...
```

The discriminator is the `.sif` suffix, not the presence of a slash — a bare
registry name like `pegasus/cper-soilmoisture:m3` contains a slash too.

## Docker → Apptainer translation notes

Things that changed shape in the conversion, and that will bite you when editing
a `.def`:

- **`ENV` splits in two.** Docker's `ENV` applies at build time *and* run time.
  Apptainer's `%environment` is **run time only — it is not sourced during
  `%post`**. Anything the build itself depends on must be exported inside
  `%post` as well. This is why `eht-difmap.def` sets `PGPLOT_DIR` twice,
  `eht-rex.def` sets `PIP_DEFAULT_TIMEOUT` in `%post`, and `eht-smili.def` sets
  `SETUPTOOLS_USE_DISTUTILS` in both places.
- **No `WORKDIR`.** Create the directory in `%post` (`mkdir -p /app/output`) and
  let Pegasus jobs `cd` into their own working directory at run time.
- **No `ARG`.** Version pins became plain shell variables at the top of `%post`
  (`DIFMAP_VERSION=2.5r`, `EHTIM_REF=v1.1.0`, `SMILI_REF=v0.0.0`).
- **`%post` already runs as root**, so all the `USER root` / `USER $MAMBA_USER`
  switching in the micromamba images is gone.
- **`%files` runs before `%post`**, regardless of where the section sits in the
  file, and its sources resolve against the build invocation directory.
- **No `ENTRYPOINT`.** `apptainer exec <img> <cmd>` — the path PegasusLite takes
  — bypasses `%runscript` entirely, so the `ENTRYPOINT []` reset the NextGen
  Dockerfile needed has no equivalent and no longer matters.
- **One `RUN` per failure boundary is gone.** Each `%post` starts with `set -eu`
  so the build still aborts on the first error, matching Docker's semantics.
- **`curl` and `wget` are mandatory in every image.** PegasusLite downloads its
  worker package *inside* the container before the job script runs; without them
  jobs die in PegasusLite before doing any work.

## Which containers embed their wrapper scripts

Most workflows register transformations with `is_stageable=True`, so Pegasus
stages `bin/*` at run time and the image only supplies dependencies. Two
definitions have a load-bearing `%files`:

- **`mag`** — `is_stageable=False` with `pfn=/usr/local/bin/<tool>.sh`, so
  `bin/*.sh` **must** be baked in.
- **`drought`** — copies `bin/` and `region_config.json` for standalone use, but
  the Pegasus run does not depend on it: `drought_common.py` and
  `region_config.json` are registered in the Replica Catalog and added as
  explicit job inputs.

`quantumchem` copies only its `requirements-*.txt` files.

## Validating a definition without building it

`apptainer build` is the real test, but a static check catches quoting and
`%files` mistakes before you burn a remote build:

```sh
./validate_defs.py .        # from this directory; checks all */Apptainer/*.def
```

It verifies the `Bootstrap:`/`From:` header, that every `%section` name is real,
that `%post`/`%runscript`/`%environment` bodies are valid shell (`bash -n`), and
that every `%files` source actually exists in the build context.
