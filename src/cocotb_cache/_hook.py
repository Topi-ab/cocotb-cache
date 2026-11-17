"""The import hook installed at every interpreter start (see ``_autoload``).

Kept tiny on purpose: it runs in every Python process of the venv, including
the simulator's embedded interpreter, and must only depend on ``sys``. (Using
``importlib.abc`` base classes would pull in ``importlib.resources`` and cost
~25 ms per interpreter start.) The heavy part, :mod:`cocotb_cache.shim`, is
imported only when ``cocotb_tools.runner`` is actually imported.
"""

from __future__ import annotations

import sys

TARGET = "cocotb_tools.runner"


class _PatchingLoader:
    """Delegates to the real loader, then patches the executed module."""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        uninstall()
        patch(module)

    def __getattr__(self, name):
        # get_source(), get_filename(), ... for tracebacks and introspection.
        return getattr(self._inner, name)


class _RunnerFinder:
    """Meta-path finder: finds cocotb_tools.runner through the other finders."""

    def find_spec(self, fullname, path, target=None):
        if fullname != TARGET:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _PatchingLoader(spec.loader)
                return spec
        return None


_finder = None


def patch(module) -> None:
    """Patch the runner module; never raises."""
    try:
        from cocotb_cache import shim

        shim._safe_patch(module)
    except Exception as e:  # never break the user's import
        print(f"cocotb-cache: could not hook into cocotb ({type(e).__name__}: {e}); cache disabled", file=sys.stderr)


def install() -> None:
    """Register the finder (idempotent); patch at once if the runner is loaded."""
    global _finder
    existing = sys.modules.get(TARGET)
    if existing is not None:
        patch(existing)
        return
    if _finder is None:
        _finder = _RunnerFinder()
        sys.meta_path.insert(0, _finder)


def uninstall() -> None:
    global _finder
    if _finder is not None:
        try:
            sys.meta_path.remove(_finder)
        except ValueError:
            pass
        _finder = None
