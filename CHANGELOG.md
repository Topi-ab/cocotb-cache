# Changelog

All notable changes to cocotb-cache. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/). A release tag `vX.Y.Z` is published only when this
file has an entry for `X.Y.Z`.

## [0.1.0] - 2026-11-17

First release.

### Added

- Transparent cache for the build and test phases of cocotb's Python runner (`cocotb_tools.runner`): installing the
  package activates it, runner scripts and testbenches stay as they are.
- Content-addressed store in any S3-compatible bucket (storage layout `v1`): gzip-compressed blobs, SRC and RESULT
  manifests, the RESULT manifest written last as the commit point.
- Cache keys from what the simulator really uses: the simulator command lines, the SystemVerilog `include files
  (found with pyslang), the testbench's Python modules, the declared environment, and the versions of Python,
  cocotb, the simulator and the tools.
- Supported: cocotb 2.1.x with Icarus Verilog, Verilator, GHDL and NVC, Python 3.11 and later. An unsupported cocotb
  version or simulator runs without the cache.
- Extra `build()` parameters: `cache_inputs` (further input files), `cache_env_whitelist` (environment variables the
  testbench reads) and `cache_fix_seed` (a reproducible seed derived from the key).
- The simulator sees only a restricted environment, without credentials or undeclared variables.
- Modes `off`, `read` and `rw` (`COCOTB_CACHE`); on any storage error or unexpected state the run falls back to
  running without the cache.
- Command line `cocotb-cache` with `inspect`, `stat` and `gc` (report only unless `--delete`).
