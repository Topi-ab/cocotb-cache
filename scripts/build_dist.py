#!/usr/bin/env python3
"""Build the release files: the sdist, and the wheel built from that sdist.

Building the wheel from the unpacked sdist (as ``python -m build`` does)
checks that the sdist is complete. In the release copy of README.md (the
PyPI project description) the links to the repository point to the release
tag instead of ``main``, so every release links to its own documentation. The build uses setuptools' PEP 517 backend
directly, so it needs nothing beyond what the container's venv has.

Usage (in the container):
    ./drun python scripts/build_dist.py [--out dist]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BACKEND = """
import sys
from setuptools import build_meta
kind, out = sys.argv[1], sys.argv[2]
print((build_meta.build_sdist if kind == "sdist" else build_meta.build_wheel)(out))
"""


def _backend(kind: str, source: Path, out: Path) -> Path:
    # A fresh interpreter in the source tree, like a PEP 517 frontend.
    r = subprocess.run([sys.executable, "-c", _BACKEND, kind, str(out)], cwd=source, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"{kind} build failed (exit {r.returncode})")
    return out / r.stdout.strip().splitlines()[-1]


def _version() -> str:
    text = (ROOT / "src" / "cocotb_cache" / "__init__.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"$', text, re.M).group(1)


def release_readme(text: str, version: str) -> str:
    """README.md with its links to the repository's main branch pinned to the release tag."""
    return re.sub(r"(https://github\.com/[^/\s)]+/[^/\s)]+/blob/)main/", rf"\g<1>v{version}/", text)


def _clear(out: Path) -> None:
    """Empty the output directory, refusing anything that is not an old build."""
    if not out.exists():
        out.mkdir(parents=True)
        return
    if out == ROOT or ROOT.is_relative_to(out) or not out.is_dir():
        raise SystemExit(f"refusing to use {out} as the output directory")
    for p in out.iterdir():
        if not (p.is_file() and (p.name.endswith(".whl") or p.name.endswith(".tar.gz"))):
            raise SystemExit(f"refusing to clear {out}: {p.name} is not a release file")
    for p in out.iterdir():
        p.unlink()


def build(out: Path) -> tuple[Path, Path]:
    out = out.resolve()
    _clear(out)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Build the sdist from a copy, so the build leaves nothing in the tree.
        src = tmp / "src-tree"
        shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
            ".git", ".venv", ".venv.lock", ".ccache", "secrets", "ext", "GitHub", "dist", "build",
            "*.egg-info", "__pycache__", "sim_build"))
        readme = src / "README.md"
        readme.write_text(release_readme(readme.read_text(), _version()))
        sdist = _backend("sdist", src, out)
        with tarfile.open(sdist) as tar:
            tar.extractall(tmp / "unpacked", filter="data")
        (top,) = (tmp / "unpacked").iterdir()
        wheel = _backend("wheel", top, out)
    return sdist, wheel


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    for p in build(args.out):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
