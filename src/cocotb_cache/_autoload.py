"""Activation hook, imported at interpreter startup via ``cocotb_cache.pth``.

Runs in every Python interpreter of the venv, including the one embedded in
the simulator, so it must stay cheap and must never raise. It only registers
an import hook; cocotb is patched when (and if) ``cocotb_tools.runner`` is
imported. The hook is installed even with the cache off, because the
``build(cache_inputs=...)`` keyword must be removed in every mode.
"""


def _activate() -> None:
    try:
        from cocotb_cache import _hook

        _hook.install()
    except Exception as e:  # never break interpreter startup
        import sys

        print(f"cocotb-cache: activation failed ({type(e).__name__}: {e}); cache disabled", file=sys.stderr)


_activate()
del _activate
