"""cache.run_phase safety rules without a simulator."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cocotb_cache import cache


class BrokenRunner:
    """Looks like a handled runner but lacks build_dir: _prepare must fail."""

    def __init__(self) -> None:
        self.env: dict[str, str] = {}
        self.log = logging.getLogger("test.broken")


def test_internal_error_runs_without_the_cache(tmp_path: Path, caplog) -> None:
    calls = []
    ctx = cache.Context(mode="rw", store=None, root=tmp_path)
    with caplog.at_level(logging.WARNING, logger="cocotb_cache"):
        cache.run_phase(BrokenRunner(), lambda r, c, w: calls.append(c), [["iverilog", "x.v"]], tmp_path,
                        phase="build", ctx=ctx)
    assert calls == [[["iverilog", "x.v"]]]
    assert "internal error" in caplog.text


def test_errors_of_the_run_itself_propagate(tmp_path: Path) -> None:
    def failing(runner, cmds, cwd):
        raise RuntimeError("Command failed with return code: 1")

    ctx = cache.Context(mode="rw", store=None, root=tmp_path)
    with pytest.raises(RuntimeError, match="return code: 1"):
        cache.run_phase(BrokenRunner(), failing, [["x"]], tmp_path, phase="build", ctx=ctx)


def test_empty_command_list_passes_through(tmp_path: Path) -> None:
    calls = []
    ctx = cache.Context(mode="rw", store=None, root=tmp_path)
    cache.run_phase(BrokenRunner(), lambda r, c, w: calls.append(c), [], tmp_path, phase="build", ctx=ctx)
    assert calls == [[]]


@pytest.mark.parametrize(
    "exc, infra",
    [
        (KeyboardInterrupt(), True),
        (RuntimeError("Command failed with return code: -9"), True),  # killed by a signal
        (RuntimeError("Command failed with return code: 1"), False),
        (RuntimeError("something else"), False),
    ],
)
def test_infra_failures_are_not_stored(exc: BaseException, infra: bool) -> None:
    assert cache._is_infra_failure(exc) is infra


def test_raise_like_recreates_builtin_exceptions() -> None:
    from cocotb_cache.manifest import Outcome

    with pytest.raises(RuntimeError, match="return code: 2"):
        cache._raise_like(Outcome(ok=False, exception="RuntimeError", message="Command failed with return code: 2"))
    with pytest.raises(RuntimeError, match="CustomError: boom"):
        cache._raise_like(Outcome(ok=False, exception="CustomError", message="boom"))


def test_derived_seed_range_and_stability() -> None:
    for key in ("0" * 64, "f" * 64, "7fffffff" + "0" * 56, "80000000" + "1" * 56):
        seed = cache._derive_seed(key)
        assert 1 <= seed <= 2**31 - 1 and seed == cache._derive_seed(key)
    assert cache._derive_seed("12345678" + "0" * 56) != cache._derive_seed("12345679" + "0" * 56)


def test_sidecar_roundtrip(tmp_path: Path) -> None:
    key = "a" * 64
    cache._write_sidecar(tmp_path, key, ["B", "A", "A"], fix_seed=True)
    assert cache.read_sidecar(tmp_path) == {"build": key, "cache_env_whitelist": ["A", "B"], "cache_fix_seed": True}
    (tmp_path / cache.SIDECAR).write_text("not json")
    assert cache.read_sidecar(tmp_path) is None
