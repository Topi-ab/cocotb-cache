"""Environment of a phase: what the simulator sees and what enters the key
(docs/user-guide.md: "The environment the simulator sees"). Principle: same run, same result.

cocotb's runner copies the whole ``os.environ`` into the environment it gives
the simulator. Two views are derived from it:

* :func:`simulator_env`: the restricted environment the simulator actually
  gets. Only variables the key covers or the tools need: whatever is not
  listed here cannot influence a result behind the key's back. A testbench
  reading an undeclared variable gets an error instead of a wrong hit.
* :func:`key_env`: the part of it that enters the cache key, normalised.
  System and tool variables whose values are machine-specific but do not
  change a result (``PATH``, ``HOME``, ...) are passed to the simulator but
  kept out of the key; the toolchain identity covers which tools they find.

What is passed (each rule applies only if the name is not blocked):

1. keys the runner set or changed compared to ``os.environ`` (``COCOTB_*`` it
   computes, ``GPI_USERS``, ``PYTHONPATH``, ``test(extra_env=...)``, ...);
2. cocotb's own variables and others known to affect results or console
   output (:data:`RESULT_VARS`);
3. system and tool variables (:data:`SYSTEM_VARS`), passed but not in the key;
4. the user's ``build(..., cache_env_whitelist=[...])`` (names or globs).

The blocklist (:func:`blocked`) overrides everything, including a whitelist:
credentials, the cache's own settings and pytest bookkeeping never reach the
simulator or a manifest.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Mapping

from .paths import Normalizer

#: Passed to the simulator and part of the key (globs). cocotb's own
#: variables (all of COCOTB_*/GPI_*/PYGPI_*, so new ones are covered too),
#: build tools' variables and other result-affecting settings.
RESULT_VARS: tuple[str, ...] = (
    "COCOTB_*",  # except COCOTB_CACHE*, see blocked()
    "GPI_*",
    "PYGPI_*",
    "TOPLEVEL_LANG",
    "LIBPYTHON_LOC",
    "SIM_CMD_PREFIX",
    "SIM_CMD_SUFFIX",
    "COVERAGE_RCFILE",
    "NO_COLOR",
    "WAVES",
    "GUI",
    "PYTHONHASHSEED",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "CC",
    "CXX",
    "CPPFLAGS",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
)

#: Passed to the simulator (the tools need them) but not part of the key:
#: their values are machine-specific and the toolchain identity covers them.
SYSTEM_VARS: tuple[str, ...] = (
    "PATH",
    "LD_LIBRARY_PATH",
    "HOME",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "VERILATOR_ROOT",
    "LM_LICENSE_FILE",
    "*_LICENSE_FILE",
    "MAKEFLAGS",
    "MFLAGS",
    "MAKELEVEL",
    "CCACHE_*",  # compiler cache (Verilator compiles through ccache): speed only
    "OBJCACHE",  # Verilator's compiler-cache wrapper
)

#: Passed to the simulator but not part of the key: where result files go
#: (a restore writes them into the current run's places) and the viewer the
#: runner may start after the run.
LOCATION_VARS: tuple[str, ...] = ("COCOTB_RESULTS_FILE", "COCOTB_WAVEFORM_VIEWER")

#: Set (to a non-false value), these make a run interactive: never cached.
INTERACTIVE_VARS: tuple[str, ...] = ("COCOTB_ATTACH", "COCOTB_PDB_ON_EXCEPTION", "GUI")
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

_SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE")
_SECRET_SUFFIXES = ("_KEY", "_KEY_ID", "_APIKEY")
_PROJECT_PLACEHOLDERS = ("${ROOT}", "${BUILD_DIR}", "${TEST_DIR}")


def blocked(name: str) -> bool:
    """Whether *name* must never reach the simulator or a manifest."""
    upper = name.upper()
    if upper.startswith("AWS_"):
        return True  # S3 client credentials and settings
    if upper == "COCOTB_CACHE" or upper.startswith("COCOTB_CACHE_"):
        return True  # the cache's own settings do not affect results
    if upper.startswith("PYTEST_"):
        return True  # per-test bookkeeping (its effect shows in COCOTB_RESULTS_FILE)
    if any(word in upper for word in _SECRET_WORDS):
        return True
    return upper.endswith(_SECRET_SUFFIXES)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def simulator_env(
    env: Mapping[str, str],
    *,
    whitelist: Iterable[str] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The restricted environment the simulator gets (values unchanged)."""
    base = os.environ if base is None else base
    whitelist = tuple(whitelist)
    out: dict[str, str] = {}
    for name, value in env.items():
        if blocked(name):
            continue
        if (
            base.get(name) != value  # set or changed by the runner
            or _matches(name, RESULT_VARS)
            or _matches(name, SYSTEM_VARS)
            or _matches(name, LOCATION_VARS)
            or _matches(name, whitelist)
        ):
            out[name] = value
    return out


def key_env(
    sim_env: Mapping[str, str], *, normalizer: Normalizer, whitelist: Iterable[str] = ()
) -> dict[str, str]:
    """The part of :func:`simulator_env` that enters the cache key, normalised.

    A name the user whitelisted is always in the key, also if it is a system
    variable (declaring it says that it matters).
    """
    whitelist = tuple(whitelist)
    out: dict[str, str] = {}
    for name, value in sim_env.items():
        if _matches(name, LOCATION_VARS):
            continue
        if _matches(name, SYSTEM_VARS) and not _matches(name, RESULT_VARS) and not _matches(name, whitelist):
            continue
        if name == "PYTHONPATH":
            out[name] = _pythonpath(value, normalizer)
        else:
            out[name] = normalizer.text(value)
    return out


def bypass_reasons(env: Mapping[str, str]) -> list[str]:
    """Reasons not to cache this run (empty: caching allowed)."""
    return [
        f"{name} is set (interactive run)"
        for name in INTERACTIVE_VARS
        if env.get(name, "").strip().lower() not in _FALSE_VALUES
    ]


def _pythonpath(value: str, normalizer: Normalizer) -> str:
    kept: list[str] = []
    for entry in value.split(os.pathsep):
        norm = normalizer.text(entry)
        if norm.startswith(_PROJECT_PLACEHOLDERS) and norm not in kept:
            kept.append(norm)
    return os.pathsep.join(kept)
