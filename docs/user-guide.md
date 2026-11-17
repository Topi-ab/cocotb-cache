# cocotb-cache user guide

cocotb-cache is a build and test cache for cocotb's Python runner (`cocotb_tools.runner`). It stores what a
`build()` or `test()` produced in S3, keyed by everything that went into it, and restores it the next time the same
inputs come along. This guide covers how it works, how to configure it, and what to watch out for. For a first try
see the [README](../README.md); for setting up a bucket see [s3-setup.md](s3-setup.md).

Contents:

1. [How it works](#how-it-works)
2. [Turning it on](#turning-it-on)
3. [The extra build() parameters](#the-extra-build-parameters)
4. [The environment the simulator sees](#the-environment-the-simulator-sees)
5. [Random seeds](#random-seeds)
6. [Configuration reference](#configuration-reference)
7. [The cocotb-cache command](#the-cocotb-cache-command)
8. [Using it in CI](#using-it-in-ci)
9. [Limitations](#limitations)
10. [Troubleshooting](#troubleshooting)
11. [What is stored, and what never is](#what-is-stored-and-what-never-is)

## How it works

cocotb's runner turns your `build()` and `test()` calls into simulator command lines and runs them. cocotb-cache
hooks in exactly there, once per call, so it sees the final command lines and the final environment. Your runner
scripts and testbenches stay almost as they are: installing the package adds a hook that Python loads at start-up,
and the hook patches cocotb's runner when your script imports it. At most you add a few optional `cache_*`
arguments to `build()` (see [below](#the-extra-build-parameters)), when a testbench reads files or environment
variables the cache cannot see on its own.

Each call is a **phase**, cached separately:

| Phase | Stored result |
|---|---|
| `build()` | the whole build directory |
| `test()` | the files the test created or changed (results file, logs, waveforms), the console output and the exit status |

The **key** of a phase is a SHA-256 hash of everything that can change its result:

- the simulator command lines and working directory, with machine-specific paths replaced by placeholders, so the
  key does not depend on where your project or a temporary build directory lives;
- the content of every input file: sources on the command line, every `` `include `` file the SystemVerilog sources
  actually include (found with the [pyslang](https://github.com/MikePopoloski/slang) preprocessor, including
  `` `ifdef `` choices), command files (`-f`), the cocotb test modules and the project files they import, and files you
  declare yourself;
- the part of the environment that can change a result (see below);
- the toolchain: Python, OS and C library, the cocotb release and a hash of its installed files, the version of every
  tool the runner uses (including the C/C++ compiler where the simulator compiles native code), and the
  cocotb-cache version;
- for a test, the key of the build it runs on.

So a change to a testbench reuses the cached build and only re-runs the test; a change to an HDL file, an include, a
parameter or a tool version rebuilds.

On a **miss** the phase runs as usual, you see its output as usual, and the result is stored. On a **hit** nothing is
compiled or simulated: the files are written back (each one checked against its hash), the console output is
replayed in its original order, and if the original run raised an error the same error is raised again. cocotb's
own code then continues as after a real run, so a hit behaves like the run it replaces, under pytest and in plain
scripts alike.

**Whenever the cache cannot be sure, it is bypassed** and the phase runs normally. A wrong hit is worse than a
needless miss. Bypassed are, for example: an `` `include `` that cannot be resolved, an input file outside the project
(see [project root](#the-project-root)), a test without a fixed seed, an interactive run (`GUI`, `COCOTB_ATTACH`,
`COCOTB_PDB_ON_EXCEPTION`), a simulator that cannot be identified, or a test whose build was not done through the
cache.

**Nothing in the cache can break a run.** If S3 is slow or unreachable, or anything unexpected happens inside the
cache, a warning is printed and the run continues without it. After the first S3 error the cache stays off for the
rest of the process, so a broken S3 costs one timeout, not one per phase.

### The project root

Input files must be inside the project, so that they can be stored relative to it and the key is the same on every
machine. The project root is, in this order: `COCOTB_CACHE_ROOT` if set, the top of the git work tree, or the
current directory. Files outside it are accepted only from the Python environment and system or tool locations
(`/usr`, `/opt`, ...), which the toolchain part of the key covers. Any other file outside the root, for example a
source in `/tmp`, makes the phase bypass the cache.

## Turning it on

The cache is off until `COCOTB_CACHE` says otherwise:

| `COCOTB_CACHE` | Behaviour |
|---|---|
| `off` (default) | no cache; cocotb behaves as without cocotb-cache, except that the simulator gets the restricted environment (below) |
| `read` | use stored results, never store anything |
| `rw` | use and store results |

and `COCOTB_CACHE_S3_BUCKET` names the bucket. Credentials, region and endpoint come from the standard AWS
mechanisms (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, `~/.aws/config`, instance roles,
`AWS_ENDPOINT_URL` for S3-compatible servers).

The cache works only in a Python environment where cocotb-cache is installed; a `python3` from elsewhere runs
cocotb without it.

This is an ordinary runner script, the same as in the README; with the cache on, its second run is a hit:

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

The `logging.basicConfig` line is optional; cocotb-cache reports hits, misses and bypasses through cocotb's runner
logger at INFO level, which Python does not print unless logging is configured. Warnings are always printed.

## The extra build() parameters

`build()` accepts three keyword arguments of its own. They are removed before cocotb sees the call, in every mode,
also with the cache off. Note that passing them makes cocotb-cache a requirement of your script: without it
installed, cocotb's `build()` rejects them.

| Parameter | Meaning |
|---|---|
| `cache_inputs=[...]` | files the simulation depends on that are not on the simulator's command line: data files read with `$readmemh` or VHDL `textio`, C/C++ sources of VHPIDIRECT or DPI code, files the testbench opens. Paths or globs (`**` allowed), relative to the project root or absolute inside it. |
| `cache_env_whitelist=[...]` | environment variables your testbench reads (names or globs). Only declared variables reach the simulator; see the next section. |
| `cache_fix_seed=True` | when no random seed is given, derive a reproducible one from the inputs instead of letting cocotb pick a random one; see [Random seeds](#random-seeds). |

The declarations belong to the build and also apply to the test that runs on it. A glob that matches nothing is
fine; a plain path that does not exist makes the phase bypass the cache (it is probably a typo). Changing the lists,
the files or the declared variables' values changes the key.

An example with all three:

```python
# The three extra build() parameters of cocotb-cache.
# Note: passing them makes cocotb-cache a requirement of this script.
from pathlib import Path

from cocotb_tools.runner import get_runner

here = Path(__file__).resolve().parent
runner = get_runner("icarus")
runner.build(
    sources=[here / "counter.sv"],
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
    timescale=("1ns", "1ps"),
    # Files the simulation reads that are not on the simulator's command line.
    cache_inputs=[str(here / "data" / "*.txt")],
    # Environment variables the testbench reads (everything else is hidden).
    cache_env_whitelist=["TB_MODE"],
    # No seed given below: derive a reproducible one from the inputs.
    cache_fix_seed=True,
)
runner.test(
    test_module="test_params",
    hdl_toplevel="counter",
    build_dir=here / "sim_build",
)
```

and its testbench, which reads the declared file and variable and uses random stimulus:

```python
import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


@cocotb.test()
async def counts_as_configured(dut):
    # A data file and an environment variable: both declared in runner.py.
    cycles = int((Path(__file__).parent / "data" / "cycles.txt").read_text())
    mode = os.environ["TB_MODE"]
    cycles += random.randint(0, 3)  # random, but reproducible with cache_fix_seed

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, cycles)
    await RisingEdge(dut.clk)
    dut._log.info("mode=%s cycles=%d q=%d", mode, cycles, int(dut.q.value))
    assert int(dut.q.value) in (cycles, cycles + 1)
```

## The environment the simulator sees

cocotb normally passes your whole environment to the simulator. A testbench could then read any variable, and a
change in it could change the result without the cache knowing. cocotb-cache therefore gives the simulator only
the variables it accounts for, **with the cache on or off**, so that a run behaves the same in every mode:

| Variables | Reach the simulator | Part of the key |
|---|---|---|
| Set by cocotb's runner, and `test(extra_env=...)` | yes | yes |
| cocotb's own: `COCOTB_*`, `GPI_*`, `PYGPI_*`, `TOPLEVEL_LANG`, `LIBPYTHON_LOC`, `SIM_CMD_PREFIX`, `SIM_CMD_SUFFIX`, `COVERAGE_RCFILE`, `NO_COLOR`, `WAVES`, `GUI` | yes | yes (including log format and debug switches: the console output is part of a result) |
| Locale and build tools: `LANG`, `LANGUAGE`, `LC_*`, `CC`, `CXX`, `CPPFLAGS`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `PYTHONHASHSEED` | yes | yes |
| System and tools: `PATH`, `LD_LIBRARY_PATH`, `HOME`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `VERILATOR_ROOT`, `LM_LICENSE_FILE`, `*_LICENSE_FILE`, `MAKEFLAGS`, `MFLAGS`, `MAKELEVEL`, `CCACHE_*`, `OBJCACHE` | yes | no (machine-specific; the toolchain identity covers which tools they find) |
| Where results go: `COCOTB_RESULTS_FILE`, `COCOTB_WAVEFORM_VIEWER` | yes | no |
| Yours, declared with `cache_env_whitelist` | yes | yes |
| Credentials: `AWS_*`, and any name containing `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL` or `PRIVATE`, or ending in `_KEY`, `_KEY_ID`, `_APIKEY` | **never** | **never**, even if whitelisted |
| The cache's own settings `COCOTB_CACHE*`, pytest's `PYTEST_*` | no | no |
| Everything else (`HOSTNAME`, `TERM`, `SSH_*`, undeclared testbench variables, ...) | no | no |

If a testbench reads a variable you did not declare, it simply does not find it (`os.environ["X"]` raises
`KeyError`). That error points at the missing `cache_env_whitelist` entry, instead of a stale result hiding it.

Only the simulator's copy of the environment is restricted; your runner script's own process keeps its full
environment.

## Random seeds

Without a seed, cocotb picks a random one for every run, so the result of a run cannot be reproduced and the test
phase is **not cached** (the build still is). Either give a seed (`test(seed=...)` or `COCOTB_RANDOM_SEED`), or use
`build(..., cache_fix_seed=True)`: the cache then derives the seed from the key of all other inputs, so the same
inputs always get the same seed, and the test becomes cacheable. It also fixes `PYTHONHASHSEED` (string hashing,
which can change e.g. set iteration order) if you did not set it. Values you set yourself always win.

With `cache_fix_seed=True` a run no longer gets a different seed each time unless its inputs change. If you rely on
fresh seeds for coverage, run those jobs with the cache off or give explicit varying seeds.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `COCOTB_CACHE` | `off` | `off`, `read` or `rw` |
| `COCOTB_CACHE_S3_BUCKET` | (none) | the bucket; without it the cache stays off with a warning |
| `COCOTB_CACHE_S3_PREFIX` | empty | key prefix inside the bucket, e.g. to share one bucket between projects |
| `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, ... | | standard AWS client configuration; path-style addressing is used, so S3-compatible servers work |
| `COCOTB_CACHE_S3_CONNECT_TIMEOUT` | `5` | seconds |
| `COCOTB_CACHE_S3_READ_TIMEOUT` | `30` | seconds |
| `COCOTB_CACHE_S3_MAX_ATTEMPTS` | `3` | tries per S3 request, including the first |
| `COCOTB_CACHE_ROOT` | git work tree top, else current directory | the project root (see above) |
| `COCOTB_CACHE_STORE_INPUTS` | `0` | `1`: also store the input files (sources) as blobs, as a record of what was run; only in `rw` mode |
| `COCOTB_CACHE_TRACE` | (none) | a file to append one JSON line per event to (see [Troubleshooting](#troubleshooting)) |

## The cocotb-cache command

The package installs a `cocotb-cache` command that uses the same configuration (or `--bucket` / `--prefix`).

```sh
cocotb-cache inspect a436e218     # what a key consists of: inputs, commands, tools, result
cocotb-cache inspect a436e218 --json
cocotb-cache stat                 # number and size of stored objects
cocotb-cache gc                   # report what garbage collection would delete
cocotb-cache gc --delete          # and delete it
```

`inspect` accepts a unique prefix of a key (at least 4 hex characters), as printed in the hit and miss messages.

`gc` keeps every file referenced by a completed result and removes unreferenced files and runs that never
completed, if they are older than `--min-age` hours (default 24, which protects runs that are uploading right now).
It deletes nothing without `--delete`. Old results are best expired with a lifecycle rule on the bucket's `result/`
prefix; the next `gc --delete` then removes the files they referenced. See [s3-setup.md](s3-setup.md).

## Using it in CI

A common setup: CI runs with `COCOTB_CACHE=rw` and credentials that may write, developers use `COCOTB_CACHE=read`
with read-only credentials and get CI's results for their unchanged parts. Concurrent jobs are safe: results are
written with a conditional put, the first writer wins, and every result is stored completely before it becomes
visible.

## Limitations

- **The Python runner only.** The Makefile flow is not supported.
- **cocotb 2.1.x** and the simulators **Icarus Verilog, Verilator, GHDL and NVC**. With another cocotb version the
  cache stays off (with a warning) and only the extra `build()` parameters are accepted; other simulators run
  without the cache.
- **Undeclared inputs are not seen.** Files read at run time (`$readmemh`, VHDL `textio`, files the testbench opens)
  and modules imported dynamically (`importlib.import_module(name)`) must be declared with `cache_inputs`. Python test
  modules and their project-internal imports are found automatically by static analysis.
- **Installed Python packages are not part of the key.** If a testbench depends on a package version (say numpy),
  a different version does not change the key. Pin such packages in your environment.
- **Include directories are hashed as a whole**, so an unrelated file in one causes a needless miss.
- **Waveforms** are stored when they are produced (they can be large).
- **A build directory containing symlinks** is not stored.
- The **replayed console output** keeps the order in which stdout and stderr were read, which is close to, but not
  guaranteed to be, the exact interleaving.
- Icarus checks whether its build is up to date by itself; a rebuild of an unchanged design in the same build
  directory runs no command at all and is not looked up in the cache (there is nothing to save).

## Troubleshooting

**Is the cache on?** Set `COCOTB_CACHE=rw` (or `read`) and `COCOTB_CACHE_S3_BUCKET`. With logging configured at INFO
level (`logging.basicConfig(level=logging.INFO)`), each phase reports `cocotb-cache: hit <key> (build)`,
`cocotb-cache: miss <key> (build)` or `cocotb-cache: not cached (test): <reason>`. A missing bucket or an S3 problem is reported as a warning.

**Why is it not cached?** The reason is in the `not cached` message. The common ones:

| Message | What to do |
|---|---|
| `no fixed random seed` | give `seed=`, or `build(..., cache_fix_seed=True)` |
| `input outside the project root: ...` | move the file into the project, or set `COCOTB_CACHE_ROOT` |
| ``an `include could not be resolved`` | the simulator would probably fail as well; check the include directories |
| `cache_inputs entry does not exist: ...` | a typo in a `cache_inputs` path |
| `no cached build for this build_dir` | the test runs on a build that was not done through the cache; build again with the cache on |
| `... is set (interactive run)` | expected for `GUI`, `COCOTB_ATTACH`, `COCOTB_PDB_ON_EXCEPTION` |

**Why does it miss when I expect a hit?** Compare the keys with `cocotb-cache inspect` on both: the inputs,
commands, environment and tools are listed. A changed tool version, an extra file in an include directory, or a
build directory path that differs are typical reasons. For a detailed record, set `COCOTB_CACHE_TRACE` to a file
name: every phase appends JSON lines with its events (`hit`, `miss`, `stored`, `bypass` with the reason,
`restore-failed`, `error`).

**A testbench fails with `KeyError` on an environment variable** it used to see: declare the variable with
`cache_env_whitelist`.

## What is stored, and what never is

Stored, under your bucket and prefix:

- the outputs of each phase: the build directory (compiled design and simulator binaries), the files a test wrote,
  its console output;
- for each phase two small JSON manifests: what went in (normalised command lines, the key environment variables,
  input file names with their hashes and sizes, tool versions) and what came out;
- the input files themselves only with `COCOTB_CACHE_STORE_INPUTS=1`.

Compiled designs and simulation logs can reveal as much as the sources; give access to the bucket accordingly.

Never stored: credentials and other secrets (see the environment table; they are not even passed to the
simulator), absolute paths of your machine (replaced by placeholders), host or user names.

The exact storage layout is described in [format.md](format.md).
