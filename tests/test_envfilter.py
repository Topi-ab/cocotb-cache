"""Environment of a phase (docs/user-guide.md, "The environment the simulator sees")."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cocotb_cache.envfilter import blocked, bypass_reasons, key_env, simulator_env
from cocotb_cache.paths import Normalizer

N = Normalizer(root=Path("/work"), venvs=(Path("/work/.venv"),), build_dir=Path("/tmp/r/b"), test_dir=Path("/tmp/r/t"))

BASE = {
    "PATH": "/usr/bin",
    "HOME": "/home/me",
    "HOSTNAME": "box17",
    "TERM": "xterm",
    "AWS_ACCESS_KEY_ID": "AKIA-secret",
    "AWS_SECRET_ACCESS_KEY": "very-secret",
    "AWS_ENDPOINT_URL": "https://s3.example",
    "GITHUB_TOKEN": "ghp_x",
    "MY_API_KEY": "k",
    "DB_PASSWORD": "p",
    "COCOTB_CACHE": "rw",
    "COCOTB_CACHE_S3_BUCKET": "bucket",
    "PYTEST_CURRENT_TEST": "tests/x.py::t (call)",
    "PYTEST_XDIST_WORKER": "gw3",
    "COCOTB_LOG_LEVEL": "DEBUG",
    "LANG": "C.UTF-8",
    "LC_ALL": "C",
    "VERILATOR_ROOT": "/opt/verilator/share/verilator",
    "MY_TB_SETTING": "42",
    "MY_TB_OTHER": "x",
    "UNRELATED": "y",
    "PYTHONHASHSEED": "0",
    "CCACHE_DIR": "/work/.ccache",
    "OBJCACHE": "ccache",
}


def runner_env(**changes: str) -> dict[str, str]:
    """BASE plus what cocotb's runner adds or changes."""
    env = dict(BASE)
    env.update(
        PATH="/usr/bin:/work/.venv/lib/python3.12/site-packages/cocotb/libs",
        PYTHONPATH="/work/tests/hdl/counter:/usr/lib/python3.12:/work/.venv/lib/python3.12/site-packages:/work/tb",
        COCOTB_TOPLEVEL="counter",
        COCOTB_TEST_MODULES="tb_counter",
        TOPLEVEL_LANG="verilog",
        COCOTB_RESULTS_FILE="/tmp/r/t/results.xml",
        GPI_USERS="/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0;/work/.venv/lib/python3.12/site-packages/pygpi/entry.py",
        PYGPI_PYTHON_BIN="/work/.venv/bin/python",
        ADDER_WIDTH="16",  # test(extra_env=...)
    )
    env.update(changes)
    return env


@pytest.mark.parametrize(
    "name",
    ["AWS_ACCESS_KEY_ID", "aws_secret_access_key", "AWS_ENDPOINT_URL", "GITHUB_TOKEN", "MY_API_KEY", "SSH_KEY_ID",
     "DB_PASSWORD", "LDAP_PASSWD", "GOOGLE_APPLICATION_CREDENTIALS", "MY_PRIVATE_THING", "X_APIKEY",
     "COCOTB_CACHE", "COCOTB_CACHE_S3_BUCKET", "COCOTB_CACHE_TRACE", "PYTEST_CURRENT_TEST"],
)
def test_blocked(name: str) -> None:
    assert blocked(name)


@pytest.mark.parametrize("name", ["COCOTB_LOG_LEVEL", "COCOTB_RANDOM_SEED", "PATH", "HOME", "KEYBOARD", "MONKEY"])
def test_not_blocked(name: str) -> None:
    assert not blocked(name)


def test_simulator_env_groups() -> None:
    sim = simulator_env(runner_env(), base=BASE)
    # Runner-set, cocotb's own, system/tool, locale, hash seed: passed.
    for name in ("PATH", "PYTHONPATH", "COCOTB_TOPLEVEL", "COCOTB_TEST_MODULES", "TOPLEVEL_LANG",
                 "COCOTB_RESULTS_FILE", "GPI_USERS", "PYGPI_PYTHON_BIN", "ADDER_WIDTH", "COCOTB_LOG_LEVEL",
                 "LANG", "LC_ALL", "HOME", "VERILATOR_ROOT", "PYTHONHASHSEED", "CCACHE_DIR", "OBJCACHE"):
        assert name in sim, name
    # Undeclared, volatile, secrets, cache settings, pytest bookkeeping: not passed.
    for name in ("HOSTNAME", "TERM", "UNRELATED", "MY_TB_SETTING", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_ENDPOINT_URL", "GITHUB_TOKEN", "MY_API_KEY", "DB_PASSWORD", "COCOTB_CACHE",
                 "COCOTB_CACHE_S3_BUCKET", "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER"):
        assert name not in sim, name
    assert sim["PATH"] == runner_env()["PATH"]  # values unchanged for the simulator


def test_whitelist_names_and_globs() -> None:
    sim = simulator_env(runner_env(), base=BASE, whitelist=["MY_TB_*"])
    assert sim["MY_TB_SETTING"] == "42" and sim["MY_TB_OTHER"] == "x"
    assert "UNRELATED" not in sim


def test_blocklist_overrides_whitelist() -> None:
    sim = simulator_env(runner_env(), base=BASE, whitelist=["*", "AWS_*", "GITHUB_TOKEN"])
    assert not any(blocked(n) for n in sim)
    assert "UNRELATED" in sim  # the wildcard still works for the rest


def test_blocked_even_if_runner_sets_it() -> None:
    sim = simulator_env(runner_env(AWS_SESSION_TOKEN="t", COCOTB_CACHE_TRACE="/x"), base=BASE)
    assert "AWS_SESSION_TOKEN" not in sim and "COCOTB_CACHE_TRACE" not in sim


def test_key_env_contents_and_normalisation() -> None:
    sim = simulator_env(runner_env(), base=BASE, whitelist=["MY_TB_SETTING"])
    key = key_env(sim, normalizer=N, whitelist=["MY_TB_SETTING"])
    assert key == {
        "PYTHONPATH": "${ROOT}/tests/hdl/counter:${ROOT}/tb",  # project entries only
        "COCOTB_TOPLEVEL": "counter",
        "COCOTB_TEST_MODULES": "tb_counter",
        "TOPLEVEL_LANG": "verilog",
        "GPI_USERS": "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0;${VENV}/lib/python3.12/site-packages/pygpi/entry.py",
        "PYGPI_PYTHON_BIN": "${VENV}/bin/python",
        "ADDER_WIDTH": "16",
        "COCOTB_LOG_LEVEL": "DEBUG",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONHASHSEED": "0",
        "MY_TB_SETTING": "42",
    }
    # Passed to the simulator but not in the key: system/tool and result location.
    for name in ("PATH", "HOME", "VERILATOR_ROOT", "CCACHE_DIR", "OBJCACHE", "COCOTB_RESULTS_FILE"):
        assert name in sim and name not in key


def test_whitelisted_system_variable_enters_key() -> None:
    sim = simulator_env(runner_env(), base=BASE, whitelist=["HOME"])
    assert key_env(sim, normalizer=N, whitelist=["HOME"])["HOME"] == "/home/me"


def test_key_is_stable_across_machines_and_runs() -> None:
    other_base = {**BASE, "PATH": "/opt/bin:/usr/bin", "HOME": "/home/other", "HOSTNAME": "ci-7",
                  "AWS_ACCESS_KEY_ID": "AKIA-other"}
    other = runner_env(PATH="/opt/bin:/usr/bin:/work/.venv/lib/python3.12/site-packages/cocotb/libs")
    other.update({k: v for k, v in other_base.items() if k not in ("PATH",)})
    n2 = Normalizer(root=Path("/work"), venvs=(Path("/work/.venv"),), build_dir=Path("/tmp/r2/b"),
                    test_dir=Path("/tmp/r2/t"))
    other["COCOTB_RESULTS_FILE"] = "/tmp/r2/t/results.xml"
    k1 = key_env(simulator_env(runner_env(), base=BASE), normalizer=N)
    k2 = key_env(simulator_env(other, base=other_base), normalizer=n2)
    assert k1 == k2


@pytest.mark.parametrize("name", ["COCOTB_ATTACH", "COCOTB_PDB_ON_EXCEPTION", "GUI"])
def test_interactive_bypass(name: str) -> None:
    assert bypass_reasons({name: "1"})
    assert not bypass_reasons({name: "0"}) and not bypass_reasons({name: ""}) and not bypass_reasons({})


# --- the simulator really gets the restricted environment ---------------------------

TB = '''
import os
import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def env_view(dut):
    await Timer(1, unit="ns")
    assert not any(k.startswith("AWS_") for k in os.environ), "S3 credentials leaked"
    assert "COCOTB_CACHE_PROBE" not in os.environ
    assert os.environ.get("UNDECLARED_TB_VAR") is None
    assert (os.environ.get("DECLARED_TB_VAR") or "") == os.environ["EXPECT_DECLARED"]
'''


@pytest.mark.sim
@pytest.mark.parametrize("declared", [True, False])
def test_simulator_sees_only_the_restricted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    from cocotb_tools.runner import get_runner

    # This worker runs with the cache off: the shim is at level "env", which
    # restricts the environment exactly like the cache-on level does.
    import cocotb_tools.runner as cr

    assert getattr(cr.Runner, "_cocotb_cache_patched") == "env"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-fake-for-test")  # like the container's real ones
    (tmp_path / "tb_env.py").write_text(TB)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("UNDECLARED_TB_VAR", "leak")
    monkeypatch.setenv("DECLARED_TB_VAR", "visible")
    monkeypatch.setenv("COCOTB_CACHE_PROBE", "x")
    counter = Path(__file__).resolve().parent / "hdl" / "counter"
    build = tmp_path / "build"
    r = get_runner("icarus")
    r.build(sources=[counter / "counter.sv"], hdl_toplevel="counter", build_dir=build, timescale=("1ns", "1ps"),
            cache_env_whitelist=["DECLARED_TB_VAR"] if declared else None)
    r.test(test_module="tb_env", hdl_toplevel="counter", build_dir=build, results_xml=str(tmp_path / "r.xml"),
           extra_env={"EXPECT_DECLARED": "visible" if declared else ""})
    # Under pytest, cocotb's test() exits with SystemExit if the testbench fails.
    assert 'failures="0"' in (tmp_path / "r.xml").read_text()
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIA-fake-for-test"  # only the simulator's copy was restricted
