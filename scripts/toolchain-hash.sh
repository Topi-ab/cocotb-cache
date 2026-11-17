#!/bin/bash
# Print the hash that names the toolchain image in CI: a hash of the git blobs
# of everything that determines the image (docker/ and the pinned cocotb
# version). Committed content only, so it is the same on every checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
git ls-files -s -- docker cocotb.version | sha256sum | cut -c1-16
