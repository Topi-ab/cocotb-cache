"""The user's way of installing and running works end to end.

A clean venv, nothing from the dev setup:

    python -m venv venv
    pip install cocotb==<pinned>
    pip install cocotb-cache          # here: the wheel built from this project
    python my_runner.py               # twice: miss, then hit

Packages come from the image's offline wheel directory (/opt/wheels), as a
PyPI install would, plus the freshly built cocotb-cache wheel. The runs see
no trace of the dev venv (no PATH, PYTHONPATH or VIRTUAL_ENV pointing at it).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from s3_local import S3Endpoint

pytestmark = [pytest.mark.sim, pytest.mark.s3]

PROJECT = Path(__file__).resolve().parents[1]
WHEELS = "/opt/wheels"
COUNTER = PROJECT / "tests" / "hdl" / "counter"

USER_RUNNER = textwrap.dedent(
    """
    # A user's runner script: nothing about the cache in it.
    import sys
    from pathlib import Path
    from cocotb_tools.runner import get_runner

    here = Path(__file__).parent
    build_dir = Path(sys.argv[1])
    runner = get_runner("icarus")
    runner.build(sources=[here / "counter.sv"], hdl_toplevel="counter", build_dir=build_dir,
                 timescale=("1ns", "1ps"))
    runner.test(test_module="tb_counter", hdl_toplevel="counter", build_dir=build_dir, seed=11)
    """
)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw)
    assert res.returncode == 0, f"{cmd}\n{res.stdout[-3000:]}\n{res.stderr[-3000:]}"
    return res


@pytest.fixture(scope="module")
def user_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("user-install")
    # Build the wheel from a private copy (parallel tests share /work).
    src = base / "src-copy"
    src.mkdir()
    for name in ("pyproject.toml", "setup.py"):
        shutil.copy2(PROJECT / name, src / name)
    shutil.copytree(PROJECT / "src", src / "src", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
    _run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "--no-build-isolation", "--no-index",
          "-w", str(base / "dist"), str(src)])

    venv = base / "venv"
    # venv bases the new environment on the base interpreter, not on the dev venv.
    _run([sys.executable, "-m", "venv", str(venv)])
    pip = [str(venv / "bin" / "python"), "-m", "pip", "-q", "install", "--no-index", "--find-links", WHEELS]
    version = (PROJECT / "cocotb.version").read_text().strip()
    _run([*pip, f"cocotb=={version}"])
    _run([*pip, "--find-links", str(base / "dist"), "cocotb-cache"])
    return venv


def _clean_env(venv: Path, **extra: str) -> dict[str, str]:
    dev_venv = str(Path(sys.prefix))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_", "COCOTB_CACHE", "AWS_", "VIRTUAL_ENV", "PYTHON"))}
    env["PATH"] = os.pathsep.join([str(venv / "bin"), *[p for p in env.get("PATH", "").split(os.pathsep)
                                                        if p and not p.startswith(dev_venv)]])
    env["VIRTUAL_ENV"] = str(venv)
    env.update(extra)
    return env


def test_install_is_a_plain_user_install(user_venv: Path) -> None:
    py = str(user_venv / "bin" / "python")
    code = (
        "import json, sys, cocotb, cocotb_cache\n"
        "from importlib.metadata import distribution\n"
        "d = distribution('cocotb-cache')\n"
        "print(json.dumps({'cocotb': cocotb.__file__, 'cache': cocotb_cache.__file__,"
        " 'editable': d.read_text('direct_url.json'), 'pth': 'cocotb_cache.pth' in {str(f) for f in d.files},"
        " 'autoload': 'cocotb_cache._autoload' in sys.modules}))"
    )
    info = json.loads(_run([py, "-c", code], env=_clean_env(user_venv)).stdout)
    assert info["cocotb"].startswith(str(user_venv)) and info["cache"].startswith(str(user_venv))
    assert info["editable"] is None or '"editable": true' not in info["editable"]
    assert info["pth"] and info["autoload"]


def test_user_runner_miss_then_hit(user_venv: Path, tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str) -> None:
    project = tmp_path / "my_project"
    project.mkdir()
    (project / ".git").mkdir()
    for name in ("counter.sv", "tb_counter.py"):
        shutil.copy2(COUNTER / name, project / name)
    (project / "my_runner.py").write_text(USER_RUNNER)

    def run(n: int) -> list[tuple[str, str]]:
        trace = tmp_path / f"trace{n}.jsonl"
        env = _clean_env(
            user_venv,
            COCOTB_CACHE="rw",
            COCOTB_CACHE_S3_BUCKET=local_s3.bucket,
            COCOTB_CACHE_S3_PREFIX=s3_prefix,
            COCOTB_CACHE_TRACE=str(trace),
            AWS_ENDPOINT_URL=local_s3.url,
            AWS_ACCESS_KEY_ID=local_s3.access_key,
            AWS_SECRET_ACCESS_KEY=local_s3.secret_key,
            AWS_DEFAULT_REGION=local_s3.region,
            PYTHONPATH=str(project),  # the user's own test module directory
        )
        _run([str(user_venv / "bin" / "python"), str(project / "my_runner.py"), str(tmp_path / f"build{n}")],
             env=env, cwd=project)
        assert 'failures="0"' in (tmp_path / f"build{n}" / "results.xml").read_text()
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        return [(e["phase"], e["event"]) for e in events if "event" in e]

    assert run(1) == [("build", "miss"), ("build", "stored"), ("test", "miss"), ("test", "stored")]
    assert run(2) == [("build", "hit"), ("test", "hit")]

    stat = _run([str(user_venv / "bin" / "cocotb-cache"), "--prefix", s3_prefix, "stat"],
                env=_clean_env(user_venv, COCOTB_CACHE_S3_BUCKET=local_s3.bucket, AWS_ENDPOINT_URL=local_s3.url,
                               AWS_ACCESS_KEY_ID=local_s3.access_key, AWS_SECRET_ACCESS_KEY=local_s3.secret_key,
                               AWS_DEFAULT_REGION=local_s3.region)).stdout
    counts = {line.split()[0]: int(line.split()[1]) for line in stat.splitlines()[1:]}
    assert counts["src"] == 2 and counts["result"] == 2 and counts["blobs"] > 0
