"""Project root detection and root-relative paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cocotb_cache.paths import find_git_root, project_root, relative_to_root


@pytest.fixture(autouse=True)
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COCOTB_CACHE_ROOT", raising=False)


def test_git_directory_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert project_root(sub) == tmp_path.resolve()


def test_git_file_root_for_worktrees_and_submodules(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    (tmp_path / "x").mkdir()
    assert project_root(tmp_path / "x") == tmp_path.resolve()


def test_nearest_git_root_wins(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "vendor" / "lib"
    (inner / ".git").mkdir(parents=True)
    assert project_root(inner / ".git") == inner.resolve()


def test_without_git_the_directory_itself(tmp_path: Path) -> None:
    # pytest's tmp_path is not inside a git work tree.
    d = tmp_path / "plain"
    d.mkdir()
    assert find_git_root(d) is None
    assert project_root(d) == d.resolve()


def test_default_is_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "s").mkdir()
    monkeypatch.chdir(tmp_path / "s")
    assert project_root() == tmp_path.resolve()


def test_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("COCOTB_CACHE_ROOT", str(other))
    assert project_root(tmp_path) == other.resolve()


def test_container_project_root() -> None:
    # In the dev container the project is /work, a git work tree.
    assert project_root(Path(__file__).parent) == Path("/work")


def test_relative_to_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "rtl").mkdir()
    f = root / "rtl" / "a.sv"
    f.write_text("")
    assert relative_to_root(f, root) == "rtl/a.sv"
    assert relative_to_root(root / "rtl" / ".." / "rtl" / "a.sv", root) == "rtl/a.sv"
    assert relative_to_root(root, root) is None
    assert relative_to_root("/usr/bin/env", root) is None


def test_symlink_out_of_root_is_outside(tmp_path: Path) -> None:
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = root / "link.txt"
    os.symlink(outside, link)
    assert relative_to_root(link, root) is None


# --- Normalizer ---------------------------------------------------------------------

from cocotb_cache.paths import Normalizer  # noqa: E402


def make_normalizer() -> Normalizer:
    return Normalizer(
        root=Path("/work"),
        venvs=(Path("/work/.venv"),),
        build_dir=Path("/tmp/run1/build"),
        test_dir=Path("/tmp/run1/test"),
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/work/rtl/a.sv", "${ROOT}/rtl/a.sv"),
        ("/work", "${ROOT}"),
        ("/work/.venv/bin/python", "${VENV}/bin/python"),
        ("/tmp/run1/build/sim.vvp", "${BUILD_DIR}/sim.vvp"),
        ("/tmp/run1/test/results.xml", "${TEST_DIR}/results.xml"),
        ("-I/work/inc", "-I${ROOT}/inc"),
        ("--vpi=/work/.venv/lib/x.so", "--vpi=${VENV}/lib/x.so"),
        ("/work/a:/work/.venv/lib:/usr/lib", "${ROOT}/a:${VENV}/lib:/usr/lib"),
        ("a=/work/x,b=/tmp/run1/build", "a=${ROOT}/x,b=${BUILD_DIR}"),
        # Not at a path boundary: left alone.
        ("/workspace/a", "/workspace/a"),
        ("/home/x/work/a", "/home/x/work/a"),
        ("/tmp/run10/build", "/tmp/run10/build"),
        ("prefix/work/a", "prefix/work/a"),
    ],
)
def test_normalizer(value: str, expected: str) -> None:
    assert make_normalizer().text(value) == expected


def test_normalizer_build_dir_inside_root_is_more_specific() -> None:
    n = Normalizer(root=Path("/work"), build_dir=Path("/work/sim_build"))
    assert n.text("/work/sim_build/x") == "${BUILD_DIR}/x"
    assert n.text("/work/sim_buildx") == "${ROOT}/sim_buildx"


def test_normalizer_same_test_and_build_dir() -> None:
    n = Normalizer(root=Path("/work"), build_dir=Path("/tmp/b"), test_dir=Path("/tmp/b"))
    assert n.text("/tmp/b/r.xml") == "${BUILD_DIR}/r.xml"
