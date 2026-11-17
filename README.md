# cocotb-cache

A transparent build and test cache for [cocotb](https://www.cocotb.org/)'s Python runner, stored in S3.

When the same HDL, testbench, parameters and tools are built and simulated again, cocotb-cache restores the
result from S3 instead of compiling and simulating: the build directory, the results file, the console output and
the exit status come back exactly as they were. Your runner scripts and testbenches stay almost as they are: at
most a few optional `cache_*` arguments to `build()`, when a testbench reads files or environment variables the
cache cannot see on its own.

- **Transparent:** install it into the venv and turn it on; most projects need no code changes. It works under
  pytest and in plain runner scripts (`python runner.py`).
- **Correctness first:** the key covers every input (sources and `` `include `` files, Python test modules,
  command lines, the relevant environment, simulator and cocotb versions). When anything is uncertain, the run
  simply happens without the cache. A wrong hit is worse than a needless miss.
- **Two phases:** `build()` and `test()` are cached separately, so a testbench-only change reuses the cached build.
- **Never breaks a run:** if S3 is slow or down, the run continues without the cache.

Supported: cocotb 2.1.x, Icarus Verilog, Verilator, GHDL and NVC, Python 3.11+, any S3-compatible storage.
The Makefile flow is not supported.

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install cocotb==2.1.0
pip install cocotb-cache
```

## Configure

The cache is off until you turn it on. Point it at a bucket with the standard AWS variables:

```sh
export COCOTB_CACHE=rw                       # off (default), read, or rw
export COCOTB_CACHE_S3_BUCKET=my-cocotb-cache
export AWS_ACCESS_KEY_ID=...                 # or a profile / role, as for any AWS client
export AWS_SECRET_ACCESS_KEY=...
export AWS_ENDPOINT_URL=https://s3.example.com   # only for S3-compatible servers
```

Use `rw` where results should be stored (for example in CI) and `read` for developers who only use them.
See [docs/s3-setup.md](https://github.com/Topi-ab/cocotb-cache/blob/main/docs/s3-setup.md) for setting up a bucket.

## Run

An ordinary runner script works as it is:

```python
# An ordinary cocotb runner script: nothing in it is specific to cocotb-cache.
import logging
from pathlib import Path

from cocotb_tools.runner import get_runner

logging.basicConfig(level=logging.INFO)  # optional: shows "cocotb-cache: hit ..."

here = Path(__file__).resolve().parent
runner = get_runner("icarus")
runner.build(
    sources=[here / "counter.sv"],
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    timescale=("1ns", "1ps"),
)
runner.test(
    test_module="test_counter",
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    seed=12345,  # a fixed seed makes the test phase cacheable
)
```

The first run simulates and stores the result; the next run of the same inputs is restored:

```text
INFO:Icarus:cocotb-cache: hit a436e2181abc2ea7f639317cf9b698f4a4f37bf0d316cc6daad0944dd5ee0100 (build)
INFO:Icarus:cocotb-cache: hit 28850118f49a95fa74cf96faea6481ac6ef717b55e714ed3846a9d34a025b0db (test)
```

The hexadecimal string is the cache key; `cocotb-cache inspect a436e218` shows what it consists of.

Two things to know:

- **The simulator only sees declared environment variables.** If your testbench reads one of its own (say
  `TB_MODE`), declare it with `build(..., cache_env_whitelist=["TB_MODE"])`; otherwise it is not visible to the
  testbench, so that it cannot change a result behind the cache's back.
- **Tests without a fixed seed are not cached** (cocotb would pick a random one). Give `seed=`, or let the cache
  derive a reproducible one with `build(..., cache_fix_seed=True)`.

More in the [user guide](https://github.com/Topi-ab/cocotb-cache/blob/main/docs/user-guide.md): what is cached, the extra `build()` parameters, configuration, the
`cocotb-cache` command line, limitations and troubleshooting. The storage layout is described in
[docs/format.md](https://github.com/Topi-ab/cocotb-cache/blob/main/docs/format.md).

## License

BSD-3-Clause, like cocotb.
