"""The package is installed in the container and activated at startup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cocotb_cache


def test_version_is_single_sourced() -> None:
    from importlib.metadata import version

    assert cocotb_cache.__version__ == version("cocotb-cache")


def test_imported_from_project_source() -> None:
    # Editable install: the code under test is the working tree, not a copy.
    assert Path(cocotb_cache.__file__).is_relative_to("/work/src")


def test_pth_is_installed_by_the_package() -> None:
    # The activation hook belongs to the installed distribution (its RECORD),
    # not to something the container entrypoint writes.
    from importlib.metadata import distribution

    files = {str(f) for f in distribution("cocotb-cache").files or ()}
    assert "cocotb_cache.pth" in files


def test_autoload_runs_at_interpreter_startup() -> None:
    # A fresh interpreter must have imported the hook via cocotb_cache.pth
    # without any import in user code.
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print('cocotb_cache._autoload' in sys.modules)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out == "True"


def test_cocotb_is_the_pinned_pypi_release() -> None:
    # cocotb comes from the PyPI wheel at the version in cocotb.version, the
    # same as users install; never from the ext/cocotb reference checkout.
    from importlib.metadata import distribution

    import cocotb

    pinned = (Path(__file__).resolve().parents[1] / "cocotb.version").read_text().strip()
    assert cocotb.__version__ == pinned
    assert Path(cocotb.__file__).is_relative_to("/work/.venv")
    assert distribution("cocotb").read_text("direct_url.json") is None  # not editable/local


def test_parallel_by_default() -> None:
    # Guards the "all tests run in parallel" rule: plain `pytest` must use
    # xdist. Checked from the config so that debugging with -n0 still works.
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    addopts = tomllib.loads(pyproject.read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-n auto" in addopts


# --- user-style installs into fresh venvs ------------------------------

PROJECT = Path(__file__).resolve().parents[1]
WHEELS = "/opt/wheels"  # offline wheel set baked into the container image


def _copy_project(dst: Path) -> Path:
    # Build from a private copy: parallel tests (and the dev venv) must not
    # share /work/build or egg-info directories.
    import shutil

    dst.mkdir()
    for name in ("pyproject.toml", "setup.py"):
        shutil.copy2(PROJECT / name, dst / name)
    shutil.copytree(PROJECT / "src", dst / "src", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
    return dst


def _venv(path: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True)
    return path / "bin" / "python"


def _pip(*args: str) -> None:
    # The dev venv's pip installs into the target via --python.
    subprocess.run([sys.executable, "-m", "pip", "-q", *args], check=True)


def _autoload_active(python: Path) -> bool:
    out = subprocess.run(
        [str(python), "-c", "import sys; print('cocotb_cache._autoload' in sys.modules)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out == "True"


def test_wheel_install_activates_without_user_code(tmp_path: Path) -> None:
    proj = _copy_project(tmp_path / "proj")
    _pip("wheel", "--no-deps", "--no-build-isolation", "--no-index", "-w", str(tmp_path / "dist"), str(proj))
    (wheel,) = (tmp_path / "dist").glob("cocotb_cache-*.whl")
    py = _venv(tmp_path / "venv")
    _pip("--python", str(py), "install", "--no-deps", "--no-index", str(wheel))
    assert _autoload_active(py)


def test_editable_install_activates_without_user_code(tmp_path: Path) -> None:
    proj = _copy_project(tmp_path / "proj")
    py = _venv(tmp_path / "venv")
    # Default pip behaviour (isolated build), as a user would run it; the
    # build backend comes offline from the image's wheel directory.
    _pip("--python", str(py), "install", "--no-deps", "--no-index", "--find-links", WHEELS, "-e", str(proj))
    assert _autoload_active(py)
    # Editable: the hook imports the working copy, not an installed copy.
    loc = subprocess.run(
        [str(py), "-c", "import cocotb_cache; print(cocotb_cache.__file__)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(loc).is_relative_to(proj / "src")
