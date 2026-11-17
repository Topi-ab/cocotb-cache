# Contributing to cocotb-cache

Thanks for your interest. This page explains how to set up the development environment, run the tests and send
changes.

## Everything runs in Docker

Development and tests run in one container image with the whole toolchain: GHDL, NVC, Verilator, Icarus Verilog, an
S3-compatible test server (versitygw) and the Python wheels, cocotb included. Nothing needs to be installed on the
host apart from Docker.

```sh
docker/build.sh            # build the image cocotb-sims:1 (the first build compiles Verilator and Icarus)
./drun versions            # tool versions inside the container
./drun pytest              # the test suite
./drun                     # a shell in the container
```

`./drun` runs a command in the container with the repository mounted at `/work` and your own user ID. On the first
start it creates a venv in `.venv/` with cocotb (the PyPI release pinned in `cocotb.version`) and this package in
editable mode; later starts take well under a second. Changes to Python code are live; a change to
`pyproject.toml`, `setup.py` or the image reinstalls the package automatically. The compiler cache of the Verilator builds (ccache)
is kept in `.ccache/` in the repository, so repeated test runs compile faster.

To change the pinned cocotb version, edit `cocotb.version` and rebuild the image. `scripts/fetch-cocotb` clones the
matching cocotb sources into `ext/cocotb` as a read-only reference for reading cocotb's code; nothing is built from
it.

## Tests

- All tests are pytest tests and run in parallel with pytest-xdist (`-n auto` is the default). Every test must be
  independent: its own `tmp_path`, its own S3 key prefix, no fixed shared paths. Use `-n0` to debug serially.
- Markers: `sim` (needs a simulator) and `s3` (needs the S3 test server). `./drun pytest -m "not sim and not s3"` runs
  the fast unit tests only.
- S3 tests use the `local_s3` and `s3_prefix` fixtures: every xdist worker starts its own versitygw on localhost.
  No test may use a real bucket; the test configuration removes `AWS_*` and `COCOTB_CACHE_*` from every test's
  environment.
- End-to-end tests run user-style runner scripts in subprocesses with the cache on (`tests/cache_harness.py`), on all
  four simulators and the small designs in `tests/hdl/`.
- The examples in `docs/examples/` are run by `tests/test_docs.py`, which also checks that the documentation shows
  them verbatim. If you change an example, change the documentation with it.

## Code

- Code, comments, docstrings, log messages and commit messages are in English.
- The cache must never break a user's run: anything unexpected falls back to running without the cache, with a
  warning. A wrong hit is worse than a needless miss, so when an input cannot be accounted for, the cache is
  bypassed.
- cocotb is not modified; the shim hooks into `cocotb_tools.runner` at run time. `tests/test_contract.py` pins the
  properties of cocotb's runner it relies on; a new cocotb version is supported only after that test passes against
  it.
- The storage layout is versioned (`v1`, see [docs/format.md](docs/format.md)); a change to manifests, hashing or
  compression needs a new version.

## Credentials for manual tests

For trying the cache against a real bucket, put the standard `AWS_*` variables and `COCOTB_CACHE_S3_BUCKET` in
`secrets/s3.env` (ignored by git, keep it mode 600); `./drun` passes it to the container as an env file, so the
values never appear on a command line. The test suite does not use it.

## Sending changes

Open a pull request against `main`. CI runs the full test suite split into parallel jobs (`./drun pytest
--shard=K/N` runs the K-th of N shards locally). The image is built only when `docker/` or `cocotb.version` changes:
it is kept in the GitHub Container Registry under a hash of those files (`scripts/toolchain-hash.sh`). Please add tests for new
behaviour and keep the documentation in `README.md` and `docs/` in step with the code.

## Releases

The version lives in one place, `__version__` in `src/cocotb_cache/__init__.py` (it is also part of the cache key,
so a new version invalidates cached results). To release:

1. set `__version__`, add a dated entry `## [<version>] - YYYY-MM-DD` to `CHANGELOG.md`, commit and push to `main`;
2. push a tag `v<version>`, for example `git tag v0.1.0 && git push origin v0.1.0`.

A tag without a matching `CHANGELOG.md` entry, or one that does not match `__version__`, stops the release before
anything is published.

The `release` workflow then runs the full test suite, builds the sdist and the wheel from it
(`scripts/build_dist.py`), checks them against the tag and installs the wheel into a fresh venv
(`scripts/check_dist.py --install`), and publishes to PyPI with Trusted Publishing from the `pypi` environment. That environment accepts only `v*` tags
and needs a maintainer's approval: the publish job waits until it is approved under *Review deployments* in the
workflow run, so the built files (the `dist` artifact) can be checked first. Run
by hand (Actions → release → Run workflow), it stops before publishing, also when started on a tag. Locally:

```sh
./drun python scripts/build_dist.py
./drun python scripts/check_dist.py --install
```
