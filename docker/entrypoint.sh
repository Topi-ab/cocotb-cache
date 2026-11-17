#!/bin/bash
# Container entrypoint: make sure /work/.venv contains cocotb from PyPI at the
# version pinned in /work/cocotb.version (the same release users install), then
# install the project's own package (cocotb_cache) in editable mode from /work
# (the package itself ships its cocotb_cache.pth activation hook, see
# setup.py), and finally run the given command inside that venv.
#
# Everything is installed offline from the wheels baked into the image
# (/opt/wheels). cocotb is never built from source; ext/cocotb is only a
# read-only reference checkout.
set -euo pipefail

WORK="${COCOTB_WORK:-/work}"
VENV="${COCOTB_VENV:-$WORK/.venv}"
STAMP="$VENV/.cocotb-stamp"
PROJECT_STAMP="$VENV/.cocotb-cache-stamp"

log() { echo "[entrypoint] $*" >&2; }

if [ ! -f "$WORK/cocotb.version" ]; then
    log "$WORK/cocotb.version not found; running without the project venv"
    exec "$@"
fi
COCOTB_VERSION="$(tr -d '[:space:]' < "$WORK/cocotb.version")"

if [ "$COCOTB_VERSION" != "${COCOTB_CACHE_IMAGE_COCOTB_VERSION:-}" ]; then
    log "cocotb.version is $COCOTB_VERSION but the image has wheels for" \
        "${COCOTB_CACHE_IMAGE_COCOTB_VERSION:-<none>}: rebuild the image (docker/build.sh)"
    exit 1
fi

# Everything that determines the venv's base contents. Any change recreates
# the venv from scratch, which is fast because no package is built.
compute_stamp() {
    {
        python3 --version
        echo "cocotb==$COCOTB_VERSION"
        ls /opt/wheels
    } | sha256sum | cut -d' ' -f1
}

# Serialize setup between concurrently starting containers.
exec 9>"$WORK/.venv.lock"
flock 9

want="$(compute_stamp)"
have="$(cat "$STAMP" 2>/dev/null || true)"

if [ "$want" != "$have" ]; then
    log "creating venv $VENV (cocotb $COCOTB_VERSION from PyPI wheel)"
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --no-index --find-links /opt/wheels --upgrade \
        pip "setuptools>=77,<84" wheel >&2
    "$VENV/bin/pip" install -q --no-index --find-links /opt/wheels \
        "cocotb==$COCOTB_VERSION" >&2
    echo "$want" > "$STAMP"
    log "cocotb $("$VENV/bin/python" -c 'import cocotb; print(cocotb.__version__)')"
fi

# The project's own package (cocotb_cache), editable from /work. Python changes
# are live; a change to its build files (pyproject.toml, setup.py, the .pth) or
# to the wheel set in the image triggers a reinstall. Dependencies are resolved
# offline from /opt/wheels, so a dependency missing from the image fails here.
if [ -f "$WORK/pyproject.toml" ]; then
    pwant="$( { cd "$WORK" && sha256sum pyproject.toml setup.py src/cocotb_cache.pth; ls /opt/wheels; } | sha256sum | cut -d' ' -f1)"
    phave="$(cat "$PROJECT_STAMP" 2>/dev/null || true)"
    if [ "$pwant" != "$phave" ]; then
        log "installing project (editable) from $WORK"
        "$VENV/bin/pip" install -q --no-index --find-links /opt/wheels --no-build-isolation \
            -e "$WORK[test]" >&2
        rm -rf "$WORK/build"
        echo "$pwant" > "$PROJECT_STAMP"
        log "cocotb_cache $("$VENV/bin/python" -c 'import cocotb_cache; print(cocotb_cache.__version__)')"
    fi
fi

flock -u 9
exec 9>&-

# Image identity for the cache key (see Dockerfile).
if [ -r /etc/cocotb-cache-image-id ]; then
    export COCOTB_CACHE_IMAGE="cocotb-sims:$(cat /etc/cocotb-cache-image-id)"
fi

export VIRTUAL_ENV="$VENV"
export PATH="$VENV/bin:$PATH"
exec "$@"
