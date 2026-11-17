"""The cache itself: what happens in ``Runner._execute`` (docs/format.md).

:func:`run_phase` is called by the shim with the restricted environment
already in place. It builds the phase's SRC manifest, looks up its RESULT and
then either restores the result (hit) or runs the original ``_execute``,
records what it produced and publishes it (miss).

Rules that keep a wrong hit impossible and never break a run:

* Anything uncertain bypasses the cache and runs normally: an incomplete
  input collection, an unidentifiable toolchain, an interactive run, a test
  without a fixed random seed, a missing build link, a ``build_dir``
  with symlinks.
* Any store failure (S3 down, missing or corrupt blob) is logged and the run
  continues without the cache; a failed restore falls back to running.
* Infrastructure failures (simulator killed by a signal, Ctrl-C) are not
  stored.
"""

from __future__ import annotations

import builtins
import dataclasses
import json
import logging
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, console
from .collect import Collected, collect_inputs
from .envfilter import bypass_reasons, key_env
from .hashing import sha256_file
from .identity import IdentityError, toolchain
from .manifest import (
    InputFile,
    Outcome,
    OutputDir,
    OutputFile,
    ResultManifest,
    SrcManifest,
)
from .paths import Normalizer, project_root
from .pyinputs import collect_python_inputs

log = logging.getLogger("cocotb_cache")

SIDECAR_DIR = ".cocotb-cache"
SIDECAR = f"{SIDECAR_DIR}/build.json"
_SIGNAL_RC = re.compile(r"return code: (-\d+)")


# --- configuration --------------------------------------------------------------


@dataclass
class Context:
    """Per-process cache configuration."""

    mode: str  # "read" or "rw"
    store: Any  # cocotb_cache.store.Store
    root: Path
    store_inputs: bool = False
    trace: Callable[[dict[str, Any]], None] | None = None
    # Set after the first S3 failure: the rest of the process runs without the
    # cache instead of waiting for a broken S3 again in every phase.
    disabled_reason: str | None = None


_context: Context | None = None
_context_failed = False
_warned: set[str] = set()


def fault(point: str) -> None:
    """Test-only fault injection: raise at *point* if it is listed in
    ``COCOTB_CACHE_TEST_FAULT`` (comma-separated). Never set in normal use;
    the variable is blocked from the simulator and the key like all
    ``COCOTB_CACHE_*``."""
    points = os.environ.get("COCOTB_CACHE_TEST_FAULT", "")
    if points and point in {p.strip() for p in points.split(",")}:
        raise RuntimeError(f"injected fault: {point}")


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        log.warning("cocotb-cache: %s", msg)


def context_from_env(mode: str) -> Context | None:
    """The process-wide context from ``COCOTB_CACHE_*`` / ``AWS_*``; None if unusable."""
    global _context, _context_failed
    if _context is not None or _context_failed:
        return _context
    bucket = os.environ.get("COCOTB_CACHE_S3_BUCKET", "").strip()
    if not bucket:
        _context_failed = True
        _warn_once("COCOTB_CACHE_S3_BUCKET is not set; cache disabled")
        return None
    try:
        from .store import Store, make_client

        store = Store(make_client(), bucket, os.environ.get("COCOTB_CACHE_S3_PREFIX", ""))
    except Exception as e:
        _context_failed = True
        _warn_once(f"cannot create the S3 client ({type(e).__name__}: {e}); cache disabled")
        return None
    store_inputs = os.environ.get("COCOTB_CACHE_STORE_INPUTS", "0").strip().lower() in ("1", "true", "yes", "on")
    _context = Context(mode=mode, store=store, root=project_root(), store_inputs=store_inputs)
    return _context


# --- sidecar -----------------------------------------------------------


def read_sidecar(build_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((build_dir / SIDECAR).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("build"), str) else None


def _write_sidecar(build_dir: Path, key: str, whitelist: Iterable[str], fix_seed: bool = False) -> None:
    path = build_dir / SIDECAR
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"build": key, "cache_env_whitelist": sorted(set(whitelist)), "cache_fix_seed": bool(fix_seed)})
    )
    os.replace(tmp, path)


# --- file state --------------------------------------------------------------------


def _walk(base: Path) -> Iterable[Path]:
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if not (d == base and n == SIDECAR_DIR))
        for name in sorted(filenames):
            yield d / name


def _snapshot(paths: Iterable[Path]) -> dict[Path, tuple[int, int, int]]:
    state = {}
    for p in paths:
        try:
            st = p.lstat()
        except OSError:
            continue
        state[p] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return state


class _Unstorable(Exception):
    """The phase produced something the cache cannot store faithfully."""


def _file_entry(path: Path, base: str, rel: str) -> tuple[OutputFile, Path]:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise _Unstorable(f"{path} is a symlink or special file")
    digest, size = sha256_file(path)
    exe = bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    return OutputFile(base, rel, digest, size, executable=exe), path


@dataclass
class _Places:
    build_dir: Path
    test_dir: Path | None
    log_file: Path | None
    results_file: Path | None

    def bases(self) -> dict[str, Path]:
        out = {"build_dir": self.build_dir}
        if self.test_dir is not None:
            out["test_dir"] = self.test_dir
        if self.log_file is not None:
            out["log_file"] = self.log_file
        if self.results_file is not None:
            out["results_file"] = self.results_file
        return out

    def watched(self) -> list[Path]:
        files = list(_walk(self.build_dir))
        if self.test_dir is not None and self.test_dir != self.build_dir:
            files.extend(_walk(self.test_dir))
        for f in (self.log_file, self.results_file):
            if f is not None:
                files.append(f)
        return files


def _inside(path: Path, base: Path | None) -> bool:
    return base is not None and (path == base or base in path.parents)


def _collect_outputs(
    phase: str, places: _Places, before: dict[Path, tuple[int, int, int]]
) -> tuple[list[tuple[OutputFile, Path]], list[OutputDir]]:
    """Outputs of the phase: all of build_dir for a build, changes otherwise."""
    outputs: list[tuple[OutputFile, Path]] = []
    seen: set[Path] = set()
    after = _snapshot(places.watched())

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        for base_name, base in (("log_file", places.log_file), ("results_file", places.results_file)):
            if base is not None and path == base:
                outputs.append(_file_entry(path, base_name, ""))
                return
        # Most specific directory first (test_dir may lie inside build_dir).
        dirs = [("test_dir", places.test_dir), ("build_dir", places.build_dir)]
        dirs.sort(key=lambda d: len(str(d[1])) if d[1] else 0, reverse=True)
        for base_name, base in dirs:
            if _inside(path, base) and path != base:
                outputs.append(_file_entry(path, base_name, path.relative_to(base).as_posix()))
                return

    for path, state in after.items():
        if phase == "build" and _inside(path, places.build_dir) and not (
            places.log_file is not None and path == places.log_file
        ):
            add(path)
        elif before.get(path) != state:
            add(path)

    empty_dirs: list[OutputDir] = []
    if phase == "build":
        for dirpath, dirnames, filenames in os.walk(places.build_dir):
            d = Path(dirpath)
            dirnames[:] = [n for n in dirnames if not (d == places.build_dir and n == SIDECAR_DIR)]
            if d != places.build_dir and not dirnames and not filenames:
                empty_dirs.append(OutputDir("build_dir", d.relative_to(places.build_dir).as_posix()))
    return outputs, empty_dirs


# --- the phase ----------------------------------------------------------------------


def _normalize_cmds(cmds: Any, n: Normalizer) -> list[list[str]]:
    return [[n.text(str(a)) for a in cmd] for cmd in cmds]


def _merge_inputs(*groups: Iterable[InputFile]) -> list[InputFile]:
    by_path: dict[str, InputFile] = {}
    for group in groups:
        for f in group:
            by_path.setdefault(f.path, f)
    return list(by_path.values())


def _derive_seed(key: str) -> int:
    """A seed in 1..2**31-1 from a hex key (valid for COCOTB_RANDOM_SEED and PYTHONHASHSEED)."""
    return int(key[:8], 16) % (2**31 - 1) + 1


def _raise_like(outcome: Outcome) -> None:
    exc_type = getattr(builtins, outcome.exception or "", None)
    if isinstance(exc_type, type) and issubclass(exc_type, Exception):
        raise exc_type(outcome.message)
    raise RuntimeError(f"{outcome.exception}: {outcome.message}")


def _is_infra_failure(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError)):
        return True
    m = _SIGNAL_RC.search(str(exc))
    return m is not None  # negative return code: killed by a signal


def run_phase(
    runner: Any,
    original: Callable[..., None],
    cmds: Any,
    cwd: Any,
    *,
    phase: str,
    ctx: Context,
    whitelist: tuple[str, ...] = (),
    fix_seed: bool = False,
) -> None:
    """Hit or miss for one ``_execute`` call (runner.env is already restricted).

    An unexpected internal error while preparing (a bug in the cache) never
    breaks the run: it is logged and the phase runs without the cache.
    Errors of the run itself propagate unchanged.
    """
    __tracebackhide__ = True
    try:
        plan = _prepare(runner, cmds, cwd, phase=phase, ctx=ctx, whitelist=whitelist, fix_seed=fix_seed)
    except Exception as e:
        log.warning("cocotb-cache: internal error (%s: %s); running without the cache", type(e).__name__, e)
        (ctx.trace or (lambda r: None))({"event": "error", "phase": phase, "where": "prepare"})
        return original(runner, cmds, cwd)
    if plan is None:
        return original(runner, cmds, cwd)
    return _hit_or_miss(runner, original, cmds, cwd, plan)


def _disable(ctx: Context, reason: str, trace: Callable[[dict[str, Any]], None], phase: str, key: str | None) -> None:
    if ctx.disabled_reason is None:
        ctx.disabled_reason = reason
        _warn_once(f"{reason}; the cache is off for the rest of this process")
    trace({"event": "error", "phase": phase, "key": key, "where": reason})


def _safe_write_sidecar(say: Any, build_dir: Path, key: str, whitelist: Iterable[str], fix_seed: bool) -> None:
    """Write the sidecar; a failure only costs the test phase its cache link."""
    try:
        fault("sidecar")
        _write_sidecar(build_dir, key, whitelist, fix_seed)
    except Exception as e:
        say.warning("cocotb-cache: cannot write %s (%s: %s); the test phase will not be cached",
                    build_dir / SIDECAR, type(e).__name__, e)


@dataclass
class _Plan:
    phase: str
    ctx: Context
    src: SrcManifest
    key: str
    places: _Places
    whitelist: tuple[str, ...]
    say: Any
    fix_seed: bool = False


def _prepare(
    runner: Any,
    cmds: Any,
    cwd: Any,
    *,
    phase: str,
    ctx: Context,
    whitelist: tuple[str, ...] = (),
    fix_seed: bool = False,
) -> _Plan | None:
    """Build the SRC manifest; ``None`` means: run without the cache."""
    say = getattr(runner, "log", log)
    trace = ctx.trace or (lambda record: None)

    def bypass(reason: str) -> None:
        trace({"event": "bypass", "phase": phase, "reason": reason})
        say.info("cocotb-cache: not cached (%s): %s", phase, reason)

    if not cmds:
        trace({"event": "bypass", "phase": phase, "reason": "no commands"})
        return None
    if ctx.disabled_reason is not None:
        trace({"event": "bypass", "phase": phase, "reason": "cache disabled after an earlier S3 error"})
        return None
    fault("prepare")
    env = dict(runner.env)
    reasons = bypass_reasons(env)
    if reasons:
        bypass("; ".join(reasons))
        return None

    build_dir = Path(runner.build_dir).resolve()
    test_dir = Path(runner.test_dir).resolve() if phase == "test" else None
    work = Path(cwd).resolve()
    # Only the venv itself: mapping the base installation (/usr) to ${VENV} too
    # would make /usr/bin/x and <venv>/bin/x the same string in the key.
    normalizer = Normalizer(ctx.root, (Path(sys.prefix),), build_dir, test_dir)

    # --- SRC manifest -------------------------------------------------------------
    try:
        tc = toolchain(type(runner).__name__)
    except IdentityError as e:
        bypass(str(e))
        return None
    cache_inputs = tuple(getattr(runner, "_cocotb_cache_inputs", ())) if phase == "build" else ()
    collected = collect_inputs(cmds, work, phase=phase, root=ctx.root, build_dir=build_dir, cache_inputs=cache_inputs)
    groups = [collected.inputs]
    problems = list(collected.problems)
    build_link = None
    if phase == "test":
        modules = [m for m in env.get("COCOTB_TEST_MODULES", "").split(",") if m.strip()]
        py: Collected = collect_python_inputs(modules, env.get("PYTHONPATH", "").split(os.pathsep), work, root=ctx.root)
        groups.append(py.inputs)
        problems.extend(py.problems)
        if not env.get("COCOTB_RANDOM_SEED") and not fix_seed:
            problems.append("no fixed random seed (give seed=, or build(..., cache_fix_seed=True))")
        sidecar = read_sidecar(build_dir)
        if sidecar is None:
            problems.append("no cached build for this build_dir (built without the cache?)")
        else:
            build_link = sidecar["build"]
    if problems:
        bypass(problems[0])
        return None

    src = SrcManifest(
        phase=phase,
        runner=type(runner).__name__,
        cmds=_normalize_cmds(cmds, normalizer),
        cwd=normalizer.text(str(work)),
        env=key_env(env, normalizer=normalizer, whitelist=whitelist),
        inputs=_merge_inputs(*groups),
        toolchain=tc,
        build=build_link,
        cache_inputs=cache_inputs,
        cmdfiles=collected.cmdfiles,
        cache_env_whitelist=whitelist if phase == "build" else (),
    )
    if phase == "test" and fix_seed:
        # cache_fix_seed: whatever the user did not give of COCOTB_RANDOM_SEED
        # and PYTHONHASHSEED is derived from the key of everything else, so
        # the same inputs always get the same values; the simulator sees them
        # and the final key contains them.
        derived = _derive_seed(src.key())
        updates = {name: str(derived) for name in ("COCOTB_RANDOM_SEED", "PYTHONHASHSEED") if not env.get(name)}
        if updates:
            runner.env.update(updates)  # the simulator's (restricted) copy
            env.update(updates)
            src = dataclasses.replace(src, env=key_env(env, normalizer=normalizer, whitelist=whitelist))
            say.info("cocotb-cache: fixed %s = %s (cache_fix_seed)", " and ".join(sorted(updates)), derived)
    key = src.key()
    log_file = Path(runner.log_file).resolve() if getattr(runner, "log_file", None) else None
    results_file = None
    if phase == "test" and env.get("COCOTB_RESULTS_FILE"):
        rf = Path(env["COCOTB_RESULTS_FILE"]).resolve()
        if not (_inside(rf, test_dir) or _inside(rf, build_dir)):
            results_file = rf
    places = _Places(build_dir, test_dir, log_file, results_file)
    return _Plan(phase, ctx, src, key, places, whitelist, say, fix_seed)


def _hit_or_miss(runner: Any, original: Callable[..., None], cmds: Any, cwd: Any, plan: _Plan) -> None:
    __tracebackhide__ = True
    phase, ctx, src, key, places, whitelist, say = (
        plan.phase, plan.ctx, plan.src, plan.key, plan.places, plan.whitelist, plan.say
    )
    build_dir = places.build_dir
    trace = ctx.trace or (lambda record: None)

    # --- lookup and hit -------------------------------------------------------------
    result = None
    try:
        result = ctx.store.get_result(key)
    except Exception as e:
        _disable(ctx, f"S3 lookup failed ({type(e).__name__}: {e})", trace, phase, key)
        return original(runner, cmds, cwd)
    if result is not None:
        try:
            chunks = _restore(result, places, ctx.store)
        except Exception as e:
            say.warning("cocotb-cache: restore of %s failed (%s: %s); running normally", key, type(e).__name__, e)
            trace({"event": "restore-failed", "phase": phase, "key": key})
        else:
            if phase == "build":
                _safe_write_sidecar(say, build_dir, key, whitelist, plan.fix_seed)
            trace({"event": "hit", "phase": phase, "key": key})
            console.replay(chunks)
            say.info("cocotb-cache: hit %s (%s)", key, phase)
            if not result.outcome.ok:
                _raise_like(result.outcome)
            return None

    # --- miss -----------------------------------------------------------------------
    if phase == "build":
        # A failed build must not leave the previous build's link behind.
        try:
            (build_dir / SIDECAR).unlink(missing_ok=True)
        except OSError:
            pass
    before = _snapshot(places.watched())
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    error: BaseException | None = None
    try:
        fault("tee")
        capture = console.tee()
        recording = capture.__enter__()
    except Exception as e:
        # Without the console the result cannot be replayed: run, do not store.
        say.warning("cocotb-cache: cannot capture the console (%s: %s); this run is not stored", type(e).__name__, e)
        trace({"event": "miss", "phase": phase, "key": key})
        original(runner, cmds, cwd)
        if phase == "build":
            _safe_write_sidecar(say, build_dir, key, whitelist, plan.fix_seed)
        return None
    try:
        original(runner, cmds, cwd)
    except BaseException as e:  # re-raised below, after recording
        error = e
    finally:
        capture.__exit__(None, None, None)
    duration = time.monotonic() - t0
    trace({"event": "miss", "phase": phase, "key": key})
    say.info("cocotb-cache: miss %s (%s)", key, phase)

    if error is not None and _is_infra_failure(error):
        raise error
    outcome = Outcome(ok=True) if error is None else Outcome(ok=False, exception=type(error).__name__, message=str(error))
    if phase == "build" and error is None:
        _safe_write_sidecar(say, build_dir, key, whitelist, plan.fix_seed)
    if ctx.mode == "rw":
        try:
            fault("publish")
            _publish(src, key, phase, places, before, recording, outcome, started, duration, ctx)
            trace({"event": "stored", "phase": phase, "key": key})
        except _Unstorable as e:
            say.warning("cocotb-cache: result of %s not stored: %s", phase, e)
        except Exception as e:
            _disable(ctx, f"storing the result failed ({type(e).__name__}: {e})", trace, phase, key)
    if error is not None:
        raise error
    return None


def _publish(
    src: SrcManifest,
    key: str,
    phase: str,
    places: _Places,
    before: dict[Path, tuple[int, int, int]],
    recording: console.Recording,
    outcome: Outcome,
    started: datetime,
    duration: float,
    ctx: Context,
) -> None:
    store = ctx.store
    outputs, dirs = _collect_outputs(phase, places, before)
    for entry, path in outputs:
        store.put_file(path, entry.sha256)
    console_digest = None
    if not recording.is_empty():
        console_digest, _ = store.put_bytes(recording.to_bytes())
    if ctx.store_inputs:
        for f in src.inputs:
            store.put_file(ctx.root / f.path, f.sha256)
    store.put_src(src)
    result = ResultManifest(
        src=key,
        phase=phase,
        outputs=tuple(e for e, _ in outputs),
        dirs=tuple(dirs),
        console=console_digest,
        outcome=outcome,
        info={
            "started": started.isoformat(timespec="seconds"),
            "duration_s": round(duration, 3),
            "shim": __version__,
        },
    )
    store.put_result(result)  # last: the commit point


def _restore(result: ResultManifest, places: _Places, store: Any) -> list[tuple[int, bytes]]:
    """Write the stored outputs into the current run's places; return console chunks."""
    chunks = console.parse(store.get_bytes(result.console)) if result.console else []
    bases = places.bases()
    for entry in result.outputs:
        base = bases.get(entry.base)
        if base is None:
            raise RuntimeError(f"no place for output base {entry.base!r} in this run")
        dest = base if entry.path == "" else base / entry.path
        store.get_file(entry.sha256, dest, expected_size=entry.size, executable=entry.executable)
    for d in result.dirs:
        base = bases.get(d.base)
        if base is not None:
            (base / d.path).mkdir(parents=True, exist_ok=True)
    return chunks
