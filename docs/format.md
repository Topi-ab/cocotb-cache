# cocotb-cache storage format (v1)

This page describes how cocotb-cache lays out its data in S3. You need it only to inspect a bucket by hand, to
write tools around it, or to check what is stored; `cocotb-cache inspect` shows the same information.

## Overview

Each `build()` and `test()` call is a **phase**. A phase is described by two JSON manifests:

| Manifest | Content | Key |
|---|---|---|
| **SRC** | everything that determines the phase's result | SHA-256 of its own canonical encoding |
| **RESULT** | everything the phase produced | the key of its SRC manifest |

Files (outputs, the console log and optionally the inputs) are stored once each, content-addressed by the SHA-256
of their content. Finding `result/<key>` means: exactly these inputs have been run, and this is what came out.

## Key layout

```text
<prefix>v1/
├── blobs/<h[0:2]>/<h>.gz      file contents, gzip-compressed
├── src/<h[0:2]>/<h>.json      SRC manifests, plain JSON
└── result/<h[0:2]>/<h>.json   RESULT manifests, plain JSON, keyed by the SRC key
```

- `<prefix>` is `COCOTB_CACHE_S3_PREFIX` (empty by default).
- `v1` is the schema version. A change to the manifest format, the hashing or the compression gets a new version,
  so old and new data never mix.
- The two-character level spreads keys over 256 prefixes.

## Hashing

- **SHA-256**, as 64 lowercase hex characters.
- A **file's** hash is over its raw bytes, without any newline or encoding normalisation.
- A **manifest's** hash is over its canonical JSON: keys sorted, separators `,` and `:` without spaces, UTF-8
  without `\u` escapes, no trailing newline. Lists whose order has no meaning are sorted before encoding:
  `inputs` by `path`, `outputs` and `dirs` by `(base, path)`, `cache_inputs` and `cache_env_whitelist`
  alphabetically and without duplicates. `cmds` and `cmdfiles` keep their order.
- An SRC manifest is stored in exactly the canonical form that was hashed, so anyone can verify it.

## Compression

- **Blobs** are gzip-compressed (level 6, header `mtime` 0, no file name), so the same content gives the same bytes.
  Their key is the hash of the **uncompressed** content; readers decompress and verify the hash, and reject a blob
  that does not match.
- **Manifests** are stored uncompressed (they are at most a few tens of kilobytes) and verified on read.
- Content types are `application/gzip` and `application/json`. `Content-Encoding: gzip` is deliberately not used,
  because some HTTP clients would then decompress transparently.

## Paths

Paths that depend on the machine or the run are replaced by placeholders, so a key is the same wherever the project,
the venv or a temporary build directory lives:

| Placeholder | Replaces |
|---|---|
| `${BUILD_DIR}` | the runner's `build_dir` |
| `${TEST_DIR}` | the runner's `test_dir` |
| `${VENV}` | the Python environment (`sys.prefix`) |
| `${ROOT}` | the project root (`COCOTB_CACHE_ROOT`, else the git work tree top, else the current directory) |

The most specific one wins, and a prefix is replaced only at path boundaries. Stored relative paths (`inputs`,
`outputs`) use `/` and never contain `..`.

## SRC manifest

| Field | Content |
|---|---|
| `schema` | `"cocotb-cache/src/v1"` |
| `phase` | `"build"` or `"test"` |
| `runner` | the cocotb runner class: `Icarus`, `Verilator`, `Ghdl`, `Nvc` |
| `cmds` | the simulator command lines (a list of argument lists), normalised |
| `cwd` | their working directory, normalised |
| `env` | the part of the simulator's environment that can change the result, normalised (see the [user guide](user-guide.md#the-environment-the-simulator-sees)) |
| `inputs` | input files under the project root: `{path, sha256, size, executable}` |
| `cmdfiles` | SHA-256 of every command file (`-f`), in processing order, wherever it lives |
| `build` | test phase only: the SRC key of the build it runs on |
| `cache_inputs`, `cache_env_whitelist` | build phase only: the user's declarations from `build()` |
| `toolchain` | `python`, `platform`, `cocotb.version`, `cocotb.dist` (hash of the installed cocotb files), `simulator` (version text of every tool used), `shim` (cocotb-cache version), and `image` when set |

A build manifest from a real run (Icarus, the quickstart example):

```json
{
  "cache_env_whitelist": [],
  "cache_inputs": [],
  "cmdfiles": [
    "46969f38f339d3fde8758e37f30c904f5d53cd1861f1136925c1fb53e8763e94"
  ],
  "cmds": [
    [
      "iverilog",
      "-o",
      "${BUILD_DIR}/sim.vvp",
      "-s",
      "counter",
      "-g2012",
      "-f",
      "${BUILD_DIR}/cmds.f",
      "${ROOT}/sim/counter.sv"
    ]
  ],
  "cwd": "${BUILD_DIR}",
  "env": {
    "GPI_USERS": "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0;${VENV}/lib/python3.12/site-packages/cocotb/simulator.cpython-312-x86_64-linux-gnu.so,initialize",
    "LC_CTYPE": "C.UTF-8",
    "PYGPI_PYTHON_BIN": "${VENV}/bin/python",
    "PYTHONPATH": "${ROOT}/sim"
  },
  "inputs": [
    {
      "executable": false,
      "path": "sim/counter.sv",
      "sha256": "7c43a1b3083608b61f85986d4a0fb2a44cb5b9083176ab64e04c126fd226105e",
      "size": 163
    }
  ],
  "phase": "build",
  "runner": "Icarus",
  "schema": "cocotb-cache/src/v1",
  "toolchain": {
    "cocotb": {
      "dist": "edeeb7a537836b98bb7f52787ad9311d8ab648906bcfe5fa104c1ad99610b11c",
      "version": "2.1.0"
    },
    "platform": "linux-x86_64-glibc2.39",
    "python": "cpython-3.12.3",
    "shim": "0.1.0",
    "simulator": "Icarus Verilog version 13.0 (stable) (v13_0-dirty)\n---\nIcarus Verilog runtime version 13.0 (stable) (v13_0-dirty)"
  }
}
```

and the test that ran on that build:

```json
{
  "build": "7caea6db67b33862fc0dd07dc63196dc5d912d60d8acb8ee662e2a6868bb30ab",
  "cmdfiles": [],
  "cmds": [
    [
      "vvp",
      "-m",
      "${VENV}/lib/python3.12/site-packages/cocotb/libs/libcocotbvpi_icarus.so",
      "${BUILD_DIR}/sim.vvp",
      "-none"
    ]
  ],
  "cwd": "${BUILD_DIR}",
  "env": {
    "COCOTB_RANDOM_SEED": "12345",
    "COCOTB_RESULTS_ATTACHMENTS": "",
    "COCOTB_TEST_MODULES": "test_counter",
    "COCOTB_TOPLEVEL": "counter",
    "GPI_USERS": "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0;${VENV}/lib/python3.12/site-packages/cocotb/simulator.cpython-312-x86_64-linux-gnu.so,initialize",
    "LC_CTYPE": "C.UTF-8",
    "PYGPI_PYTHON_BIN": "${VENV}/bin/python",
    "PYTHONPATH": "${ROOT}/sim",
    "TOPLEVEL_LANG": "verilog"
  },
  "inputs": [
    {
      "executable": false,
      "path": "sim/test_counter.py",
      "sha256": "084ec17957787782f34fbdf623a49620793091406ef3fec44a389f4c13265f9f",
      "size": 378
    }
  ],
  "phase": "test",
  "runner": "Icarus",
  "schema": "cocotb-cache/src/v1",
  "toolchain": {
    "cocotb": {
      "dist": "edeeb7a537836b98bb7f52787ad9311d8ab648906bcfe5fa104c1ad99610b11c",
      "version": "2.1.0"
    },
    "platform": "linux-x86_64-glibc2.39",
    "python": "cpython-3.12.3",
    "shim": "0.1.0",
    "simulator": "Icarus Verilog version 13.0 (stable) (v13_0-dirty)\n---\nIcarus Verilog runtime version 13.0 (stable) (v13_0-dirty)"
  }
}
```

## RESULT manifest

| Field | Content |
|---|---|
| `schema` | `"cocotb-cache/result/v1"` |
| `src` | the SRC key (the same as the object's key) |
| `phase` | `"build"` or `"test"` |
| `outputs` | produced files: `{base, path, sha256, size, executable}` |
| `dirs` | empty directories to recreate: `{base, path}` |
| `console` | hash of the console blob, or `null` |
| `outcome` | `{"ok": true}`, or `{"ok": false, "exception": <type>, "message": <text>}` |
| `info` | for humans only, not used on restore: start time (UTC), duration, cocotb-cache version |

`base` says what a path is relative to: `build_dir`, `test_dir`, or a single file `log_file` (the runner's log
file) or `results_file` (a results file outside both directories); for the single files `path` is empty. A build
stores the whole build directory; a test stores the files it created or changed.

The two results of the run above:

```json
{
  "console": "f7b219d8f606316d1bfa1d4a97140d0835cf1449b8548533ddcd837e8339f2b9",
  "dirs": [],
  "info": {
    "duration_s": 0.005,
    "shim": "0.1.0",
    "started": "2026-11-17T01:42:44+00:00"
  },
  "outcome": {
    "ok": true
  },
  "outputs": [
    {
      "base": "build_dir",
      "executable": false,
      "path": "cmds.f",
      "sha256": "46969f38f339d3fde8758e37f30c904f5d53cd1861f1136925c1fb53e8763e94",
      "size": 19
    },
    {
      "base": "build_dir",
      "executable": true,
      "path": "sim.vvp",
      "sha256": "5a6a8926426af532f20880013f491a33d75265cca3371e1148f9e5efb9ce9f5c",
      "size": 1527
    }
  ],
  "phase": "build",
  "schema": "cocotb-cache/result/v1",
  "src": "7caea6db67b33862fc0dd07dc63196dc5d912d60d8acb8ee662e2a6868bb30ab"
}
```

```json
{
  "console": "f1b74ffb0be6dab5749d1a1ef39f40a8fa4357d34d160c8e53af401ccb6f567e",
  "dirs": [],
  "info": {
    "duration_s": 0.268,
    "shim": "0.1.0",
    "started": "2026-11-17T01:42:44+00:00"
  },
  "outcome": {
    "ok": true
  },
  "outputs": [
    {
      "base": "test_dir",
      "executable": false,
      "path": "results.xml",
      "sha256": "1145e890165de1b48235d5d3c03615e822eefd86c10018b40133f9aca373a97e",
      "size": 798
    }
  ],
  "phase": "test",
  "schema": "cocotb-cache/result/v1",
  "src": "d60c33c32346efed359eb20f2777d1efef12cce8c4442bb5a29d049c72e4427c"
}
```

### Console blob

The console output is one blob in JSON Lines, one record per chunk in the order it was read, keeping stdout and
stderr in their relative order:

```text
{"fd":1,"b64":"ICAgICAwLjAwbnMgSU5GTyAgICAgY29jb3RiIC4uLgo="}
{"fd":2,"b64":"..."}
```

`fd` is 1 (stdout) or 2 (stderr); `b64` is the chunk, base64-encoded.

## Build sidecar

After a build, `build_dir/.cocotb-cache/build.json` holds the build's SRC key, the `cache_env_whitelist` and the
`cache_fix_seed` flag. A test that runs on the build directory, also in another process, reads its `build` link from
there. The sidecar is local only and never uploaded.

## Flow

**Lookup:** build the SRC manifest, hash it, get `result/<key>.json`. Missing means a miss.

**Hit:** fetch every blob of `outputs` and `console`, verify each, write the files into the current run's
directories, replay the console, and raise the stored exception if `outcome.ok` is false. If anything fails, the
phase runs normally instead.

**Miss:** run the phase, then (in `rw` mode) upload in this order:

1. the output blobs and the console blob (each skipped if a `HEAD` finds it already stored), and the input blobs if
   `COCOTB_CACHE_STORE_INPUTS=1`;
2. `src/<key>.json`;
3. **last**, `result/<key>.json`, as a conditional put (`If-None-Match: *`).

The RESULT manifest is the commit point: because it is written last, every blob it refers to already exists, and an
interrupted upload never leaves a broken hit behind. Of two concurrent writers of the same key the first wins; the
other gets `412 Precondition Failed`, which is not an error, because both results come from the same inputs.

## Garbage

Blobs that no RESULT refers to any more (after results expire) and SRC manifests without a RESULT (runs that never
completed) are garbage. `cocotb-cache gc` removes them after a safety period (24 hours by default), which protects
uploads in progress. See [s3-setup.md](s3-setup.md#keeping-the-bucket-in-bounds).
