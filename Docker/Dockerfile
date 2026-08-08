# CPER soil-moisture workflow container (M3: observations + static covariates
# + station characterization).
#
# M1 needed only pandas/numpy/requests; M2 added rasterio (whose manylinux
# wheel bundles GDAL/PROJ, so python:3.11-slim still works — no system GDAL);
# M3 adds scikit-learn (behavioural clustering + random-forest attribution)
# and matplotlib (the figure set). Terrain derivatives and the recession fits
# stay in plain numpy (SPEC.md "Container"), so scipy arrives only as an
# sklearn dependency and geopandas still isn't needed. Published as
# pegasus/cper-soilmoisture:m3 — a new tag per SPEC.md, never a mutated
# :latest.
FROM python:3.11-slim

# libexpat1 is NOT optional: rasterio's manylinux wheel bundles GDAL/PROJ but
# still dynamically links the system libexpat, and python:3.11-slim omits it —
# without this, `import rasterio` dies with
# "ImportError: libexpat.so.1: cannot open shared object file".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        wget curl ca-certificates libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    pandas \
    numpy \
    requests \
    rasterio \
    scikit-learn \
    matplotlib

# Matplotlib must not try to write a font cache into a read-only or
# non-existent HOME on a worker node; the jobs use the Agg backend.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
