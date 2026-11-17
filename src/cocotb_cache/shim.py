"""Runtime hook into cocotb's Python runner (docs/user-guide.md: "How it works").

``install()`` registers a meta-path finder at every interpreter start. When
``cocotb_tools.runner`` is imported, the module is executed normally and then
``patch_runner()`` wraps methods of its ``Runner`` base class:

* ``build()`` accepts the extra keywords ``cache_inputs`` (user-declared
  input files), ``cache_env_whitelist`` (environment variables
  the testbench may read) and ``cache_fix_seed`` (derive the
  random seed from the key when none is given) and removes them
  before calling cocotb.
  This is done at every level, so user code that passes them always runs.
* With a supported cocotb, ``_execute()`` (called exactly once per
  ``build()``/``test()`` with the final command lines) gives the simulator
  the restricted environment, with the cache on or off, so a run behaves the
  same in every mode.
* With the cache on, ``_execute()`` is also the seam where the cache acts
  (:func:`cocotb_cache.cache.run_phase`: hit or miss).

See :func:`patch_runner` for the levels ("strip", "env", "full").

Nothing here may break a run: an unsupported cocotb version, an unexpected
``_execute`` signature or any internal error leaves cocotb without the cache
(at most stripped) and logs a warning.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

log = logging.getLogger("cocotb_cache")

TARGET = "cocotb_tools.runner"

# cocotb releases whose runner internals (the _execute seam) are verified by
# the contract test. Prefix match on the version string.
SUPPORTED_COCOTB = ("2.1.",)

# Runner classes the cache handles. Each must inherit build(), test() and
# _execute() from Runner unchanged; others always pass through.
SUPPORTED_RUNNERS = frozenset({"Icarus", "Ghdl", "Nvc", "Verilator"})

MODES = ("off", "read", "rw")
PATCHED_ATTR = "_cocotb_cache_patched"  # "strip", "env" or "full"
PHASE_ATTR = "_cocotb_cache_phase"
INPUTS_ATTR = "_cocotb_cache_inputs"
ENV_WHITELIST_ATTR = "_cocotb_cache_env_whitelist"
FIX_SEED_ATTR = "_cocotb_cache_fix_seed"
CACHE_INPUTS = "cache_inputs"
CACHE_ENV_WHITELIST = "cache_env_whitelist"
CACHE_FIX_SEED = "cache_fix_seed"


def mode() -> str:
    """The configured cache mode (``COCOTB_CACHE``); invalid values mean off."""
    value = os.environ.get("COCOTB_CACHE", "off").strip().lower() or "off"
    if value not in MODES:
        log.warning("cocotb-cache: COCOTB_CACHE=%r is not one of %s; cache disabled", value, "/".join(MODES))
        return "off"
    return value


# --- import hook ------------------------------------------------------------------


def install() -> None:
    """Activate the hook; see :mod:`cocotb_cache._hook` (kept import-light).

    Installed in every mode, because the ``cache_inputs`` keyword must be
    stripped even with the cache off.
    """
    from cocotb_cache import _hook

    _hook.install()


# --- patching ---------------------------------------------------------------------


def _cocotb_version() -> str:
    from importlib.metadata import version

    return version("cocotb")


def _check_supported(module: ModuleType) -> str | None:
    """Return why the runner cannot be patched, or ``None`` if it can."""
    import inspect

    version = _cocotb_version()
    if not version.startswith(SUPPORTED_COCOTB):
        return f"cocotb {version} is not a supported version ({', '.join(v + 'x' for v in SUPPORTED_COCOTB)})"
    runner = getattr(module, "Runner", None)
    if runner is None:
        return f"{TARGET} has no Runner class"
    for name in ("build", "test", "_execute"):
        if not callable(getattr(runner, name, None)):
            return f"Runner.{name} is missing"
    params = list(inspect.signature(runner._execute).parameters)
    if params != ["self", "cmds", "cwd"]:
        return f"Runner._execute has an unexpected signature {params}"
    return None


def _safe_patch(module: ModuleType) -> None:
    try:
        patch_runner(module)
    except Exception as e:  # never break the user's run
        log.warning("cocotb-cache: could not hook into cocotb (%s: %s); cache disabled", type(e).__name__, e)


def patch_runner(module: ModuleType) -> bool:
    """Patch ``Runner`` at the level the mode and cocotb allow. Idempotent.

    Levels (stored in ``Runner._cocotb_cache_patched``):

    * ``"strip"``: unsupported cocotb. ``build()`` only removes the
      ``cache_inputs`` / ``cache_env_whitelist`` keywords; nothing that
      relies on cocotb's internals is touched.
    * ``"env"``: supported cocotb, cache off. Also ``_execute()`` gives the
      simulator the restricted environment (docs/user-guide.md: "The environment the simulator sees"), so a run
      behaves the same with the cache on or off.
    * ``"full"``: supported cocotb, cache on. Also the cache seam.

    Returns ``True`` if the cache is active (``"full"``).
    """
    runner = module.Runner
    state = getattr(runner, PATCHED_ATTR, None)
    if state is not None:
        return state == "full"
    active = mode() != "off"
    reason = _check_supported(module)
    if reason is None:
        level = "full" if active else "env"
    else:
        level = "strip"
        if active:
            log.warning("cocotb-cache: %s; cache disabled", reason)

    orig_build = runner.build

    @functools.wraps(orig_build)
    def build(self: Any, *args: Any, **kwargs: Any) -> Any:
        __tracebackhide__ = True
        setattr(self, INPUTS_ATTR, _list_arg(kwargs.pop(CACHE_INPUTS, None), CACHE_INPUTS))
        setattr(self, ENV_WHITELIST_ATTR, _list_arg(kwargs.pop(CACHE_ENV_WHITELIST, None), CACHE_ENV_WHITELIST))
        fix_seed = kwargs.pop(CACHE_FIX_SEED, False)
        if not isinstance(fix_seed, bool):
            raise TypeError(f"{CACHE_FIX_SEED} must be True or False, got {type(fix_seed).__name__}")
        setattr(self, FIX_SEED_ATTR, fix_seed)
        if level == "strip":
            return orig_build(self, *args, **kwargs)
        with _phase(self, "build"):
            return orig_build(self, *args, **kwargs)

    build.__signature__ = _with_cache_params(orig_build)  # type: ignore[attr-defined]
    runner.build = build
    if level == "strip":
        setattr(runner, PATCHED_ATTR, "strip")
        return False

    orig_test, orig_execute = runner.test, runner._execute
    use_cache = level == "full"

    @functools.wraps(orig_test)
    def test(self: Any, *args: Any, **kwargs: Any) -> Any:
        __tracebackhide__ = True
        with _phase(self, "test"):
            return orig_test(self, *args, **kwargs)

    @functools.wraps(orig_execute)
    def _execute(self: Any, cmds: Any, cwd: Any) -> None:
        __tracebackhide__ = True
        return execute(self, orig_execute, cmds, cwd, use_cache=use_cache)

    runner.test = test
    runner._execute = _execute
    setattr(runner, PATCHED_ATTR, level)
    return use_cache


def _list_arg(value: Any, name: str) -> tuple[str, ...]:
    """Validate a ``build(name=[...])`` list of paths / patterns; not a single string."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, os.PathLike)):
        raise TypeError(f"{name} must be a list of strings, not a single string")
    try:
        items = list(value)
    except TypeError:
        raise TypeError(f"{name} must be a list of strings, got {type(value).__name__}") from None
    out = []
    for item in items:
        if not isinstance(item, (str, os.PathLike)):
            raise TypeError(f"{name} entries must be str or path-like, got {type(item).__name__}")
        out.append(os.fspath(item))
    return tuple(out)


def _with_cache_params(func: Callable[..., Any]) -> Any:
    """Signature of *func* plus keyword-only cache parameters (for help())."""
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    insert_at = len(params)
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        insert_at -= 1
    extras = ((CACHE_INPUTS, None), (CACHE_ENV_WHITELIST, None), (CACHE_FIX_SEED, False))
    for offset, (pname, default) in enumerate(extras):
        params.insert(insert_at + offset, inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default))
    return sig.replace(parameters=params)


@contextmanager
def _phase(runner: Any, phase: str) -> Iterator[None]:
    previous = getattr(runner, PHASE_ATTR, None)
    setattr(runner, PHASE_ATTR, phase)
    try:
        yield
    finally:
        setattr(runner, PHASE_ATTR, previous)


def handles(runner: Any) -> bool:
    """Whether the cache acts for this runner instance.

    Only the supported simulator classes, and only if they still use the base
    class methods (a subclass overriding them may bypass the phase wrappers).
    """
    cls = type(runner)
    if cls.__name__ not in SUPPORTED_RUNNERS:
        return False
    return not any(name in vars(cls) for name in ("build", "test", "_execute"))


def execute(runner: Any, original: Callable[..., None], cmds: Any, cwd: Any, *, use_cache: bool = True) -> None:
    """The ``_execute`` seam.

    For a handled runner the simulator gets the restricted environment
    (:func:`cocotb_cache.envfilter.simulator_env`) and, with the cache on,
    :func:`cocotb_cache.cache.run_phase` decides between hit and miss. The
    runner's own ``env`` is restored afterwards.
    """
    __tracebackhide__ = True
    phase = getattr(runner, PHASE_ATTR, None)
    handled = phase is not None and handles(runner)
    _trace({"runner": type(runner).__name__, "phase": phase, "active": handled and use_cache, "ncmds": len(cmds)})
    if not handled:
        return original(runner, cmds, cwd)
    from .envfilter import simulator_env

    full_env = runner.env
    try:
        from .cache import fault

        fault("restrict")
        whitelist = _whitelist_for(runner, phase)
        restricted = simulator_env(full_env, whitelist=whitelist)
    except Exception as e:  # a bug here must not break the run
        log.warning(
            "cocotb-cache: could not restrict the environment (%s: %s); running without the cache", type(e).__name__, e
        )
        return original(runner, cmds, cwd)
    runner.env = restricted
    try:
        if use_cache:
            from . import cache

            ctx = cache.context_from_env(mode())
            if ctx is not None:
                ctx.trace = _trace
                return cache.run_phase(
                    runner, original, cmds, cwd, phase=phase, ctx=ctx, whitelist=whitelist,
                    fix_seed=_fix_seed_for(runner, phase),
                )
        return original(runner, cmds, cwd)
    finally:
        runner.env = full_env


def _fix_seed_for(runner: Any, phase: str) -> bool:
    """build(cache_fix_seed=...) of this runner, or of the build's sidecar."""
    if hasattr(runner, FIX_SEED_ATTR):
        return bool(getattr(runner, FIX_SEED_ATTR))
    build_dir = getattr(runner, "build_dir", None)
    if phase == "test" and build_dir is not None:
        from pathlib import Path

        from .cache import read_sidecar

        sidecar = read_sidecar(Path(build_dir))
        if sidecar is not None:
            return bool(sidecar.get("cache_fix_seed", False))
    return False


def _whitelist_for(runner: Any, phase: str) -> tuple[str, ...]:
    """The cache_env_whitelist that applies to this phase.

    Set by build() on the same runner object; for a test() on another object
    or in another process, read from the build's sidecar.
    """
    if hasattr(runner, ENV_WHITELIST_ATTR):
        return tuple(getattr(runner, ENV_WHITELIST_ATTR))
    build_dir = getattr(runner, "build_dir", None)
    if phase == "test" and build_dir is not None:
        from pathlib import Path

        from .cache import read_sidecar

        sidecar = read_sidecar(Path(build_dir))
        if sidecar is not None:
            return tuple(sidecar.get("cache_env_whitelist", ()))
    return ()


def _trace(record: dict[str, Any]) -> None:
    """Debug/test aid: append one JSON line per event to $COCOTB_CACHE_TRACE."""
    path = os.environ.get("COCOTB_CACHE_TRACE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"pid": os.getpid(), **record}) + "\n")
    except Exception:  # tracing must never affect the run
        pass
