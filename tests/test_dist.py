"""The release build and its checks (scripts/build_dist.py, scripts/check_dist.py)."""

from __future__ import annotations

import importlib.util
import io
import shutil
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# One worker builds the release files once for all of these tests.
pytestmark = pytest.mark.xdist_group("dist")

build_dist = _load("build_dist")
check_dist = _load("check_dist")
VERSION = check_dist.source_version()


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    build_dist.build(out)
    return out


def copy_dist(built: Path, tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    shutil.copytree(built, dist)
    return dist


def test_release_files_pass(built: Path) -> None:
    assert check_dist.check(built, f"v{VERSION}") == []
    assert sorted(p.name for p in built.iterdir()) == [
        f"cocotb_cache-{VERSION}-py3-none-any.whl", f"cocotb_cache-{VERSION}.tar.gz"]


def test_sdist_has_the_pth_and_no_tests(built: Path) -> None:
    with tarfile.open(next(built.glob("*.tar.gz"))) as tar:
        names = tar.getnames()
    assert f"cocotb_cache-{VERSION}/src/cocotb_cache.pth" in names
    assert not any("/tests/" in n for n in names)


def test_sdist_has_no_build_leftovers(built: Path) -> None:
    # The sdist is built from a clean copy of the tree, without caches or old builds.
    with tarfile.open(next(built.glob("*.tar.gz"))) as tar:
        assert not [n for n in tar.getnames() if "__pycache__" in n or "/build/" in n]


def test_wrong_tag_is_rejected(built: Path) -> None:
    assert check_dist.check(built, "v99.0.0") == [f"tag v99.0.0 does not match __version__ {VERSION}"]


def _rewrite_wheel(wheel: Path, drop: str | None = None, replace: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(wheel) as z:
        items = [(n, z.read(n)) for n in z.namelist()]
    with zipfile.ZipFile(wheel, "w") as z:
        for n, data in items:
            if n == drop:
                continue
            z.writestr(n, (replace or {}).get(n, data))


def test_wheel_without_pth_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    _rewrite_wheel(next(dist.glob("*.whl")), drop="cocotb_cache.pth")
    assert "wheel: cocotb_cache.pth missing from the wheel root" in check_dist.check(dist, None)


def test_pth_missing_from_record_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    wheel = next(dist.glob("*.whl"))
    record = f"cocotb_cache-{VERSION}.dist-info/RECORD"
    with zipfile.ZipFile(wheel) as z:
        text = z.read(record).decode()
    stripped = "".join(l for l in text.splitlines(keepends=True) if not l.startswith("cocotb_cache.pth,"))
    _rewrite_wheel(wheel, replace={record: stripped.encode()})
    assert any("not listed in RECORD" in e for e in check_dist.check(dist, None))


def test_cocotb_dependency_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    wheel = next(dist.glob("*.whl"))
    meta = f"cocotb_cache-{VERSION}.dist-info/METADATA"
    with zipfile.ZipFile(wheel) as z:
        text = z.read(meta).decode()
    text = text.replace("Requires-Dist: boto3", "Requires-Dist: cocotb>=2\nRequires-Dist: boto3", 1)
    _rewrite_wheel(wheel, replace={meta: text.encode()})
    assert any("depends on cocotb" in e for e in check_dist.check(dist, None))


def test_private_file_in_sdist_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    sdist = next(dist.glob("*.tar.gz"))
    with tarfile.open(sdist) as tar:
        members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
    with tarfile.open(sdist, "w:gz") as tar:
        for m, data in members:
            tar.addfile(m, io.BytesIO(data) if data is not None else None)
        extra = tarfile.TarInfo(f"cocotb_cache-{VERSION}/secrets/s3.env")
        extra.size = 1
        tar.addfile(extra, io.BytesIO(b"x"))
    assert "sdist: unexpected file secrets/s3.env" in check_dist.check(dist, None)


def test_extra_file_in_dist_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    (dist / "old.whl.bak").write_text("")
    assert check_dist.check(dist, None)[0].startswith("expected one wheel and one sdist")


def test_wheel_installs_and_activates(built: Path) -> None:
    assert check_dist.install_check(built) == []


def test_missing_module_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    _rewrite_wheel(next(dist.glob("*.whl")), drop="cocotb_cache/manifest.py")
    assert "wheel: cocotb_cache/manifest.py missing" in check_dist.check(dist, None)


def test_private_file_in_wheel_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as z:
        z.writestr("cocotb_cache/.env", "x")
    assert "wheel: unexpected file cocotb_cache/.env" in check_dist.check(dist, None)


def test_description_links_point_to_the_release(built: Path) -> None:
    with zipfile.ZipFile(next(built.glob("*.whl"))) as z:
        meta = z.read(f"cocotb_cache-{VERSION}.dist-info/METADATA").decode()
    description = Parser().parsestr(meta).get_payload()  # Project-URL: Documentation stays on main
    assert f"/blob/v{VERSION}/docs/user-guide.md" in description
    assert "/blob/main/" not in description
    # The repository's own README keeps linking to main.
    assert "/blob/main/docs/user-guide.md" in (ROOT / "README.md").read_text()


def test_description_linking_to_main_is_rejected(built: Path, tmp_path: Path) -> None:
    dist = copy_dist(built, tmp_path)
    wheel = next(dist.glob("*.whl"))
    meta = f"cocotb_cache-{VERSION}.dist-info/METADATA"
    with zipfile.ZipFile(wheel) as z:
        text = z.read(meta).decode()
    _rewrite_wheel(wheel, replace={meta: text.replace(f"/blob/v{VERSION}/", "/blob/main/").encode()})
    assert any("description links to" in e for e in check_dist.check(dist, None))


def test_release_readme_pins_only_repository_links() -> None:
    text = ("[a](https://github.com/o/r/blob/main/docs/x.md) [b](https://www.cocotb.org/) "
            "[c](https://github.com/o/r/blob/dev/y.md)")
    assert build_dist.release_readme(text, "1.2.3") == (
        "[a](https://github.com/o/r/blob/v1.2.3/docs/x.md) [b](https://www.cocotb.org/) "
        "[c](https://github.com/o/r/blob/dev/y.md)")


@pytest.mark.parametrize("where", ["root", "parent", "foreign"])
def test_output_directory_is_guarded(tmp_path: Path, where: str) -> None:
    if where == "root":
        out = build_dist.ROOT
    elif where == "parent":
        out = build_dist.ROOT.parent
    else:
        out = tmp_path / "out"
        out.mkdir()
        (out / "notes.txt").write_text("keep me")
    with pytest.raises(SystemExit, match="refusing"):
        build_dist._clear(out)
    if where == "foreign":
        assert (out / "notes.txt").read_text() == "keep me"


def test_output_directory_with_old_builds_is_cleared(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    out.mkdir()
    (out / "cocotb_cache-0.0.1.tar.gz").write_text("")
    (out / "cocotb_cache-0.0.1-py3-none-any.whl").write_text("")
    build_dist._clear(out)
    assert list(out.iterdir()) == []


def test_changelog_has_an_entry_for_this_version() -> None:
    assert check_dist.changelog_errors(VERSION) == []


@pytest.mark.parametrize(
    "text, error",
    [
        ("# Changelog\n\n## [Unreleased]\n\n- x\n", "no dated entry"),
        ("# Changelog\n\n## [9.9.9] - 2026-01-01\n\n- x\n", "no dated entry"),
        ("# Changelog\n\n## [1.2.3]\n\n- x\n", "no dated entry"),
        ("# Changelog\n\n## [1.2.3] - 2026-01-01\n\n## [1.2.2] - 2025-01-01\n\n- x\n", "lists no changes"),
    ],
)
def test_changelog_without_a_release_entry_is_rejected(tmp_path: Path, text: str, error: str) -> None:
    (tmp_path / "CHANGELOG.md").write_text(text)
    errors = check_dist.changelog_errors("1.2.3", tmp_path / "CHANGELOG.md")
    assert len(errors) == 1 and error in errors[0]


def test_changelog_entry_found_among_others(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.3.0] - 2026-02-01\n\n- y\n\n## [1.2.3] - 2026-01-01\n\n### Fixed\n\n- x\n")
    assert check_dist.changelog_errors("1.2.3", tmp_path / "CHANGELOG.md") == []


def test_missing_changelog_is_rejected(tmp_path: Path) -> None:
    assert check_dist.changelog_errors("1.2.3", tmp_path / "CHANGELOG.md") == ["CHANGELOG.md missing"]
