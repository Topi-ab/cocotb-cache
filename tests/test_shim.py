"""The runner hook: activation, patching, version guard, phases."""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

from cocotb_cache import shim

COUNTER = Path(__file__).resolve().parent / "hdl" / "counter"


# --- in-process: patch_runner on a stand-in module ---------------------------------


def make_module() -> ModuleType:
    """A minimal stand-in for cocotb_tools.runner with the same method shapes."""
    mod = ModuleType("fake_runner")

    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, list, str]] = []
            self.env: dict[str, str] = {}
            self.env_seen: list[dict[str, str]] = []

        def build(self, sources: list | None = None, *, always: bool = False) -> None:
            """Build docstring."""
            self._execute([["build-cmd"]], cwd="b")

        def test(self, test_module: str, hdl_toplevel: str, fail: bool = False) -> str:
            self._execute([["test-cmd"], ["second"]], cwd="t")
            if fail:
                raise RuntimeError("Command failed with return code: 1")
            return "results.xml"

        def _execute(self, cmds: list, cwd: str) -> None:
            self.calls.append((getattr(self, shim.PHASE_ATTR, None), cmds, cwd))
            self.env_seen.append(dict(self.env))

    class Icarus(Runner):
        pass

    class Questa(Runner):  # not a supported runner
        pass

    class Verilator(Runner):  # supported name, but overrides a method
        def test(self, *a, **k):  # type: ignore[override]
            return Runner.test(self, *a, **k)

    mod.Runner, mod.Icarus, mod.Questa, mod.Verilator = Runner, Icarus, Questa, Verilator
    return mod


@pytest.fixture
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shim, "_cocotb_version", lambda: "2.1.0")
    monkeypatch.setenv("COCOTB_CACHE", "rw")


def test_patch_preserves_behaviour_and_records_phase(supported: None) -> None:
    mod = make_module()
    assert shim.patch_runner(mod)
    r = mod.Icarus()
    r.build(["a.sv"])
    assert r.test("tb", "top") == "results.xml"
    assert r.calls == [("build", [["build-cmd"]], "b"), ("test", [["test-cmd"], ["second"]], "t")]
    assert getattr(r, shim.PHASE_ATTR) is None  # reset after each phase


def test_exceptions_propagate_unchanged(supported: None) -> None:
    mod = make_module()
    shim.patch_runner(mod)
    with pytest.raises(RuntimeError, match="return code: 1"):
        mod.Icarus().test("tb", "top", fail=True)


def test_signatures_and_docs_preserved(supported: None) -> None:
    mod = make_module()
    before = {n: inspect.signature(getattr(mod.Runner, n)) for n in ("build", "test", "_execute")}
    shim.patch_runner(mod)
    for name in ("test", "_execute"):
        assert inspect.signature(getattr(mod.Runner, name)) == before[name]
    build_sig = inspect.signature(mod.Runner.build)
    extras = {"cache_inputs": None, "cache_env_whitelist": None, "cache_fix_seed": False}
    assert list(build_sig.parameters) == [*before["build"].parameters, *extras]
    for name, default in extras.items():
        assert build_sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert build_sig.parameters[name].default is default
    assert mod.Runner.build.__doc__ == "Build docstring."


def test_patch_is_idempotent(supported: None) -> None:
    mod = make_module()
    assert shim.patch_runner(mod)
    wrapped = mod.Runner._execute
    assert shim.patch_runner(mod)
    assert mod.Runner._execute is wrapped
    r = mod.Icarus()
    r.build()
    assert len(r.calls) == 1


def test_unsupported_cocotb_version_is_only_stripped(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(shim, "_cocotb_version", lambda: "2.2.0")
    monkeypatch.setenv("COCOTB_CACHE", "rw")
    mod = make_module()
    original_execute, original_test = mod.Runner._execute, mod.Runner.test
    with caplog.at_level(logging.WARNING, logger="cocotb_cache"):
        assert not shim.patch_runner(mod)
    assert mod.Runner._execute is original_execute and mod.Runner.test is original_test
    assert getattr(mod.Runner, shim.PATCHED_ATTR) == "strip"
    assert "2.2.0 is not a supported version" in caplog.text
    r = mod.Icarus()
    r.build(cache_inputs=["x"])  # still accepted
    assert r.calls == [(None, [["build-cmd"]], "b")]  # no phase: cache inactive


def test_cache_off_restricts_env_silently(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(shim, "_cocotb_version", lambda: "2.1.0")
    monkeypatch.setenv("COCOTB_CACHE", "off")
    mod = make_module()
    with caplog.at_level(logging.WARNING, logger="cocotb_cache"):
        assert not shim.patch_runner(mod)
    assert caplog.text == ""
    assert getattr(mod.Runner, shim.PATCHED_ATTR) == "env"
    mod.Icarus().build(["a.sv"], cache_inputs=["csrc/*.cpp"], cache_env_whitelist=["X"])


def test_execute_restricts_and_restores_env(supported: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {"AWS_SECRET_ACCESS_KEY": "s", "MY_VAR": "1", "OTHER": "2"}.items():
        monkeypatch.setenv(name, value)
    mod = make_module()
    shim.patch_runner(mod)
    r = mod.Icarus()
    r.env = {**os.environ, "COCOTB_TOPLEVEL": "t"}  # what cocotb's runner builds
    before = dict(r.env)
    r.build(cache_env_whitelist=["MY_VAR"])
    seen = r.env_seen[-1]
    assert "AWS_SECRET_ACCESS_KEY" not in seen and "OTHER" not in seen
    assert seen["MY_VAR"] == "1" and seen["COCOTB_TOPLEVEL"] == "t" and seen["PATH"] == os.environ["PATH"]
    assert r.env == before  # the runner's own env is restored after the call


def test_cache_inputs_recorded_and_not_passed_to_cocotb(supported: None) -> None:
    mod = make_module()
    shim.patch_runner(mod)
    r = mod.Icarus()
    r.build(["a.sv"], cache_inputs=["csrc/**/*.cpp", Path("mem/a.hex")])
    assert getattr(r, shim.INPUTS_ATTR) == ("csrc/**/*.cpp", "mem/a.hex")
    r.build(["a.sv"])
    assert getattr(r, shim.INPUTS_ATTR) == ()


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_cache_fix_seed_must_be_bool(supported: None, bad: object) -> None:
    mod = make_module()
    shim.patch_runner(mod)
    with pytest.raises(TypeError, match="cache_fix_seed"):
        mod.Icarus().build(cache_fix_seed=bad)
    r = mod.Icarus()
    r.build(cache_fix_seed=True)
    assert getattr(r, shim.FIX_SEED_ATTR) is True


@pytest.mark.parametrize("param", ["cache_inputs", "cache_env_whitelist"])
@pytest.mark.parametrize("bad", ["single/string.cpp", 42, [1], b"bytes"])
def test_cache_list_params_type_errors(supported: None, bad: object, param: str) -> None:
    mod = make_module()
    shim.patch_runner(mod)
    with pytest.raises(TypeError, match=param):
        mod.Icarus().build(**{param: bad})


def test_unexpected_execute_signature_is_only_stripped(supported: None, caplog) -> None:
    mod = make_module()
    mod.Runner._execute = lambda self, cmds, cwd, extra=None: None
    with caplog.at_level(logging.WARNING, logger="cocotb_cache"):
        assert not shim.patch_runner(mod)
    assert "unexpected signature" in caplog.text


def test_internal_error_never_breaks_the_run(supported: None, caplog) -> None:
    broken = ModuleType("broken")  # no Runner attribute at all
    with caplog.at_level(logging.WARNING, logger="cocotb_cache"):
        shim._safe_patch(broken)
    assert "cache disabled" in caplog.text


def test_handles_only_supported_unmodified_runners(supported: None) -> None:
    mod = make_module()
    shim.patch_runner(mod)
    assert shim.handles(mod.Icarus())
    assert not shim.handles(mod.Questa())  # not a supported simulator
    assert not shim.handles(mod.Verilator())  # overrides test()


@pytest.mark.parametrize("value, expected", [("off", "off"), ("", "off"), ("RW", "rw"), ("read", "read"), ("on", "off")])
def test_mode(monkeypatch: pytest.MonkeyPatch, value: str, expected: str) -> None:
    monkeypatch.setenv("COCOTB_CACHE", value)
    assert shim.mode() == expected


def test_trace_records_calls(supported: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("COCOTB_CACHE_TRACE", str(trace))
    mod = make_module()
    shim.patch_runner(mod)
    r = mod.Icarus()
    r.build()
    r.test("tb", "top")
    mod.Questa().build()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(x["runner"], x["phase"], x["active"], x["ncmds"]) for x in records] == [
        ("Icarus", "build", True, 1),
        ("Icarus", "test", True, 2),
        ("Questa", "build", False, 1),
    ]


# --- subprocess: the real hook, activated by the .pth at interpreter start -------


def run_python(code: str, mode: str | None, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COCOTB_CACHE"))}
    if mode is not None:
        env["COCOTB_CACHE"] = mode
    env.update(extra_env)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)


PROBE = textwrap.dedent(
    """
    import sys
    import cocotb_tools.runner as r
    print(getattr(r.Runner, "_cocotb_cache_patched", None))
    print(any(type(f).__name__ == "_RunnerFinder" for f in sys.meta_path))
    """
)


@pytest.mark.parametrize("mode", ["read", "rw"])
def test_real_runner_fully_patched_on_import(mode: str) -> None:
    out = run_python(PROBE, mode).stdout.split()
    assert out == ["full", "False"]  # patched, and the finder removed itself


@pytest.mark.parametrize("mode", [None, "off"])
def test_off_restricts_env_without_cache(mode: str | None) -> None:
    code = PROBE + "\nprint(hasattr(r.Runner._execute, '__wrapped__'))\n"
    assert run_python(code, mode).stdout.split() == ["env", "False", "True"]


def test_invalid_mode_warns_and_stays_off() -> None:
    res = run_python(PROBE, "yes-please")
    assert res.stdout.split() == ["env", "False"]
    assert "COCOTB_CACHE='yes-please'" in res.stderr


def test_unrelated_imports_do_not_trigger_patching() -> None:
    code = "import sys, json, cocotb\nprint('cocotb_tools.runner' in sys.modules)"
    assert run_python(code, "rw").stdout.strip() == "False"


# --- end to end with a simulator: plain script and pytest -------------------------


RUNNER_SCRIPT = textwrap.dedent(
    """
    from pathlib import Path
    from cocotb_tools.runner import get_runner

    counter = Path({counter!r})
    build_dir = Path({build_dir!r})
    r = get_runner("icarus")
    r.build(sources=[counter / "counter.sv"], hdl_toplevel="counter", build_dir=build_dir,
            timescale=("1ns", "1ps"), always=True)
    r.test(hdl_toplevel="counter", test_module="tb_counter", build_dir=build_dir)
    """
)


def user_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Environment of a user's process: cache on, tracing on, no pytest leftovers.

    PYTEST_* must not leak in: cocotb names its results file after
    PYTEST_CURRENT_TEST when it is set.
    """
    trace = tmp_path / "trace.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COCOTB_CACHE"))}
    env.update(COCOTB_CACHE="rw", COCOTB_CACHE_TRACE=str(trace), PYTHONPATH=str(COUNTER))
    return env, trace


def _trace_records(path: Path) -> list[tuple[str, str, bool]]:
    records = map(json.loads, path.read_text().splitlines())
    return [(x["runner"], x["phase"], x["active"]) for x in records if "runner" in x]


@pytest.mark.sim
def test_plain_runner_script(tmp_path: Path) -> None:
    script = tmp_path / "my_runner.py"
    script.write_text(RUNNER_SCRIPT.format(counter=str(COUNTER), build_dir=str(tmp_path / "build")))
    env, trace = user_env(tmp_path)
    subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path, check=True, capture_output=True)
    assert _trace_records(trace) == [("Icarus", "build", True), ("Icarus", "test", True)]
    assert 'failures="0"' in (tmp_path / "build" / "results.xml").read_text()


@pytest.mark.sim
@pytest.mark.parametrize("mode", ["off", "rw"])
def test_cache_inputs_accepted_by_real_cocotb(tmp_path: Path, mode: str) -> None:
    # User code passing cache_inputs must run in every mode.
    script = tmp_path / "my_runner.py"
    body = RUNNER_SCRIPT.format(counter=str(COUNTER), build_dir=str(tmp_path / "build"))
    script.write_text(body.replace("always=True)", "always=True, cache_inputs=['hdl/counter/*.sv'])"))
    env, _ = user_env(tmp_path)
    env["COCOTB_CACHE"] = mode
    subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path, check=True, capture_output=True)
    assert 'failures="0"' in (tmp_path / "build" / "results.xml").read_text()


@pytest.mark.sim
def test_under_pytest(tmp_path: Path) -> None:
    test_file = tmp_path / "test_user_bench.py"
    body = RUNNER_SCRIPT.format(counter=str(COUNTER), build_dir=str(tmp_path / "build"))
    test_file.write_text("def test_bench():\n" + textwrap.indent(body, "    "))
    env, trace = user_env(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), str(test_file)],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert _trace_records(trace) == [("Icarus", "build", True), ("Icarus", "test", True)]
