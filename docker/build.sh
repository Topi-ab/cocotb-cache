#!/bin/bash
# Build the toolchain image. The cocotb version comes from cocotb.version at
# the repository root (single source of truth).
set -e
cd "$(dirname "$0")"
COCOTB_VERSION="$(tr -d '[:space:]' < ../cocotb.version)"
exec docker build --build-arg "COCOTB_VERSION=$COCOTB_VERSION" -t "${COCOTB_IMAGE:-cocotb-sims:1}" "$@" .
