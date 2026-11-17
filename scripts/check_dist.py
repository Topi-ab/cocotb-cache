#!/usr/bin/env python3
"""Check the release files built by scripts/build_dist.py before publishing.

- exactly one sdist and one wheel, both for the version in
  ``cocotb_cache.__version__``; with ``--tag``, the tag names that version and
  CHANGELOG.md has a dated entry for it;
- the wheel has ``cocotb_cache.pth`` at its root, identical to the source and
  listed in RECORD, and every module of the package; its metadata has the
  license, the dependencies (never cocotb itself), the console script and the
  README, whose repository links point to the release tag;
- the sdist has what a wheel build needs and nothing private;
- with ``--install``: the wheel installs into a fresh venv together with the
  pinned cocotb, the shim activates there and the CLI runs (in the container:
  the dependencies come from the image's offline wheels).

Usage (in the container):
    ./drun python scripts/check_dist.py [--dist dist] [--tag v0.1.0] [--install]
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEELS = "/opt/wheels"

# Every module of the package, and the typing marker.
PACKAGE_FILES = {p.name for p in (ROOT / "src" / "cocotb_cache").glob("*.py")} | {"py.typed"}
SDIST_REQUIRED = {"pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE", "PKG-INFO",
                  "src/cocotb_cache.pth", "src/cocotb_cache/__init__.py"}
# Never in a release file (matched against each path component).
PRIVATE = re.compile(r"^(?:secrets|\.env|\.venv|\.ccache|ext|GitHub|\.git|\.claude|tests)$|\.md$")
PUBLIC_MD = {"README.md"}


def source_version() -> str:
    text = (ROOT / "src" / "cocotb_cache" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not m:
        raise SystemExit("no __version__ in src/cocotb_cache/__init__.py")
    return m.group(1)


def changelog_errors(version: str, path: Path = ROOT / "CHANGELOG.md") -> list[str]:
    """A release needs a dated CHANGELOG.md entry ("## [X.Y.Z] - YYYY-MM-DD") with content."""
    if not path.exists():
        return ["CHANGELOG.md missing"]
    text = path.read_text()
    m = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$", text, re.M)
    if not m:
        return [f"CHANGELOG.md has no dated entry '## [{version}] - YYYY-MM-DD'"]
    body = text[m.end():].split("\n## ", 1)[0]
    if not re.search(r"^\s*-\s+\S", body, re.M):
        return [f"CHANGELOG.md entry for {version} lists no changes"]
    return []


def check(dist: Path, tag: str | None) -> list[str]:
    errors: list[str] = []
    version = source_version()
    if tag is not None and tag.removeprefix("v") != version:
        errors.append(f"tag {tag} does not match __version__ {version}")
    if tag is not None:
        errors += changelog_errors(version)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    others = sorted(p.name for p in dist.iterdir() if p not in wheels + sdists)
    if len(wheels) != 1 or len(sdists) != 1 or others:
        return errors + [f"expected one wheel and one sdist in {dist}, found "
                         f"{[p.name for p in wheels + sdists] + others}"]
    wheel, sdist = wheels[0], sdists[0]
    if wheel.name != f"cocotb_cache-{version}-py3-none-any.whl":
        errors.append(f"wheel name {wheel.name} (expected version {version}, pure Python)")
    if sdist.name != f"cocotb_cache-{version}.tar.gz":
        errors.append(f"sdist name {sdist.name} (expected version {version})")
    errors += _check_wheel(wheel, version)
    errors += _check_sdist(sdist, version)
    return errors


def _check_wheel(wheel: Path, version: str) -> list[str]:
    errors = []
    info = f"cocotb_cache-{version}.dist-info"
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        pth = z.read("cocotb_cache.pth") if "cocotb_cache.pth" in names else None
        record = z.read(f"{info}/RECORD").decode() if f"{info}/RECORD" in names else ""
        meta_text = z.read(f"{info}/METADATA").decode() if f"{info}/METADATA" in names else ""
        entry = z.read(f"{info}/entry_points.txt").decode() if f"{info}/entry_points.txt" in names else ""
    if pth is None:
        errors.append("wheel: cocotb_cache.pth missing from the wheel root")
    elif pth != (ROOT / "src" / "cocotb_cache.pth").read_bytes():
        errors.append("wheel: cocotb_cache.pth differs from src/cocotb_cache.pth")
    recorded = {row[0] for row in csv.reader(io.StringIO(record)) if row}
    if "cocotb_cache.pth" not in recorded:
        errors.append("wheel: cocotb_cache.pth not listed in RECORD (pip would not uninstall it)")
    for f in sorted(PACKAGE_FILES - {n.removeprefix("cocotb_cache/") for n in names}):
        errors.append(f"wheel: cocotb_cache/{f} missing")
    for n in sorted(names):
        if any(PRIVATE.search(part) for part in n.split("/")):
            errors.append(f"wheel: unexpected file {n}")
    if f"{info}/licenses/LICENSE" not in names:
        errors.append("wheel: LICENSE missing")
    meta = Parser().parsestr(meta_text)
    if meta.get("Name") != "cocotb-cache" or meta.get("Version") != version:
        errors.append(f"wheel metadata: name/version {meta.get('Name')} {meta.get('Version')}")
    if meta.get("License-Expression") != "BSD-3-Clause":
        errors.append(f"wheel metadata: License-Expression {meta.get('License-Expression')!r}")
    requires = [r.split(";")[0].strip() for r in meta.get_all("Requires-Dist") or [] if "extra ==" not in r]
    names_req = {re.split(r"[ <>=!~\[(]", r, maxsplit=1)[0].lower() for r in requires}
    if not {"boto3", "pyslang"} <= names_req:
        errors.append(f"wheel metadata: dependencies {requires}")
    if "cocotb" in names_req:
        errors.append("wheel metadata: depends on cocotb (the user chooses the cocotb release)")
    if not meta.get("Description-Content-Type", "").startswith("text/markdown") or "cocotb-cache" not in (
        meta.get_payload() or ""
    ):
        errors.append("wheel metadata: README missing from the long description")
    # The description is frozen into the release: its links to the repository
    # must point to this release's tag, not to main (scripts/build_dist.py).
    links = re.findall(r"https://github\.com/[^/\s)]+/[^/\s)]+/blob/([^/\s)]+)/", meta.get_payload() or "")
    if [ref for ref in links if ref != f"v{version}"]:
        errors.append(f"wheel metadata: description links to {sorted(set(links))}, not to v{version}")
    if "cocotb-cache = cocotb_cache.cli:main" not in entry:
        errors.append("wheel: console script cocotb-cache missing")
    return errors


def _check_sdist(sdist: Path, version: str) -> list[str]:
    errors = []
    top = f"cocotb_cache-{version}/"
    with tarfile.open(sdist) as tar:
        members = [m.name for m in tar.getmembers()]
    if not all(m == top.rstrip("/") or m.startswith(top) for m in members):
        errors.append(f"sdist: files outside {top}")
    rel = {m.removeprefix(top) for m in members}
    for f in sorted(SDIST_REQUIRED - rel):
        errors.append(f"sdist: {f} missing")
    for f in sorted(rel):
        parts = f.split("/")
        if any(PRIVATE.search(p) for p in parts) and f not in PUBLIC_MD:
            errors.append(f"sdist: unexpected file {f}")
    return errors


def install_check(dist: Path) -> list[str]:
    """Install the wheel like a user and check that the shim activates."""
    wheel = next(dist.glob("*.whl"))
    cocotb = (ROOT / "cocotb.version").read_text().strip()
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = str(venv / "bin" / "python")
        subprocess.run([py, "-m", "pip", "install", "-q", "--no-index", "--find-links", WHEELS,
                        f"cocotb=={cocotb}", str(wheel)], check=True)
        probe = (
            "import cocotb_cache, cocotb_tools.runner as r; "
            "print(cocotb_cache.__file__); print(getattr(r.Runner, '_cocotb_cache_patched', None))"
        )
        env = {"PATH": f"{venv}/bin:/usr/bin:/bin", "HOME": tmp}
        out = subprocess.run([py, "-c", probe], env=env, cwd=tmp, check=True, capture_output=True, text=True)
        where, level = out.stdout.split()
        errors = []
        if not where.startswith(str(venv)):
            errors.append(f"install: cocotb_cache imported from {where}, not from the fresh venv")
        if level != "env":  # cache off by default: environment restriction only
            errors.append(f"install: shim level {level!r} (expected 'env' with the cache off)")
        cli = subprocess.run([str(venv / "bin" / "cocotb-cache"), "--help"], env=env, cwd=tmp,
                             capture_output=True, text=True)
        if cli.returncode != 0:
            errors.append(f"install: cocotb-cache --help failed: {cli.stderr.strip()}")
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", help="release tag (vX.Y.Z) that must match the version")
    parser.add_argument("--install", action="store_true", help="also install the wheel into a fresh venv")
    args = parser.parse_args(argv)
    errors = check(args.dist, args.tag)
    if not errors and args.install:
        errors = install_check(args.dist)
    for e in errors:
        print("error: " + e)
    if errors:
        return 1
    print(f"ok: {', '.join(sorted(p.name for p in args.dist.iterdir()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
