"""The documentation's examples run as documented.

docs/examples/* are real projects; README.md and docs/user-guide.md show
their files verbatim, which test_docs_show_the_tested_files checks, so the
documentation cannot drift from the code that is tested here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from s3_local import S3Endpoint

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "examples"


def _code_blocks(markdown: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"```[a-z]*\n(.*?)```", markdown, re.S)]


@pytest.mark.parametrize(
    "doc, files",
    [
        ("README.md", ["quickstart/runner.py"]),
        ("docs/user-guide.md", ["quickstart/runner.py", "params/runner.py", "params/test_params.py"]),
    ],
)
def test_docs_show_the_tested_files(doc: str, files: list[str]) -> None:
    blocks = [b.strip() for b in _code_blocks((ROOT / doc).read_text())]
    for f in files:
        assert (EXAMPLES / f).read_text().strip() in blocks, f"{doc} does not show {f} verbatim"


# --- running the examples ------------------------------------------------------------------


class Example:
    def __init__(self, name: str, tmp_path: Path, local_s3: S3Endpoint, prefix: str) -> None:
        self.root = tmp_path / "repo"
        self.dir = self.root / name
        shutil.copytree(EXAMPLES / name, self.dir)
        (self.root / ".git").mkdir()
        self.tmp, self.s3, self.prefix, self.n = tmp_path, local_s3, prefix, 0

    def run(self, **env: str) -> list[tuple[str, str]]:
        """Run the example's runner.py from scratch; return the cache events."""
        self.n += 1
        shutil.rmtree(self.dir / "sim_build", ignore_errors=True)
        trace = self.tmp / f"trace{self.n}.jsonl"
        full = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COCOTB_CACHE", "AWS_"))}
        full.update(
            COCOTB_CACHE="rw",
            COCOTB_CACHE_S3_BUCKET=self.s3.bucket,
            COCOTB_CACHE_S3_PREFIX=self.prefix,
            COCOTB_CACHE_TRACE=str(trace),
            AWS_ENDPOINT_URL=self.s3.url,
            AWS_ACCESS_KEY_ID=self.s3.access_key,
            AWS_SECRET_ACCESS_KEY=self.s3.secret_key,
            AWS_DEFAULT_REGION=self.s3.region,
            **env,
        )
        res = subprocess.run([sys.executable, "runner.py"], cwd=self.dir, env=full, capture_output=True, text=True,
                             timeout=300)
        assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
        self.output = res.stdout + res.stderr
        return [(e["phase"], e["event"]) for e in map(json.loads, trace.read_text().splitlines()) if "event" in e]


MISS = [("build", "miss"), ("build", "stored"), ("test", "miss"), ("test", "stored")]
HIT = [("build", "hit"), ("test", "hit")]


@pytest.mark.sim
@pytest.mark.s3
def test_quickstart_example(tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str) -> None:
    ex = Example("quickstart", tmp_path, local_s3, s3_prefix)
    assert ex.run() == MISS
    assert ex.run() == HIT
    assert "cocotb-cache: hit" in ex.output  # visible because the script enables INFO logging
    ex.run()
    assert "cocotb-cache: hit" in ex.output and "cocotb-cache: miss" not in ex.output
    (ex.dir / "test_counter.py").write_text((ex.dir / "test_counter.py").read_text() + "\n# changed\n")
    assert ex.run() == [("build", "hit"), ("test", "miss"), ("test", "stored")]
    assert "cocotb-cache: miss" in ex.output


@pytest.mark.sim
@pytest.mark.s3
def test_params_example(tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str) -> None:
    ex = Example("params", tmp_path, local_s3, s3_prefix)
    assert ex.run(TB_MODE="fast") == MISS
    assert ex.run(TB_MODE="fast") == HIT  # no seed given, yet cached (cache_fix_seed)
    assert ex.run(TB_MODE="fast", UNRELATED="x") == HIT  # undeclared variables are not seen
    assert ex.run(TB_MODE="slow") == MISS  # a whitelisted variable is in the key
    (ex.dir / "data" / "cycles.txt").write_text("9\n")
    assert ex.run(TB_MODE="slow") == MISS  # a declared input file is in the key


# --- format.md and s3-setup.md ---------------------------------------------------------------


def _json_blocks(markdown: str) -> list[object]:
    return [json.loads(m.group(1)) for m in re.finditer(r"```json\n(.*?)```", markdown, re.S)]


def test_format_examples_are_valid_and_consistent_manifests() -> None:
    from cocotb_cache.hashing import canonical_hash
    from cocotb_cache.manifest import ResultManifest, SrcManifest

    blocks = _json_blocks((ROOT / "docs" / "format.md").read_text())
    srcs = [SrcManifest.from_json(b) for b in blocks if b["schema"] == "cocotb-cache/src/v1"]
    results = [ResultManifest.from_json(b) for b in blocks if b["schema"] == "cocotb-cache/result/v1"]
    build, test = sorted(srcs, key=lambda s: s.phase)  # "build" < "test"
    by_src = {r.src: r for r in results}
    assert set(by_src) == {build.key(), test.key()}  # each result is keyed by its SRC's hash
    assert test.build == build.key()  # the test links to the build
    assert by_src[build.key()].phase == "build" and by_src[test.key()].phase == "test"
    for s in srcs:  # canonical hash of the shown form
        assert canonical_hash(s.to_json()) == s.key()


def test_format_console_example_decodes() -> None:
    import base64

    text = (ROOT / "docs" / "format.md").read_text()
    m = re.search(r'\{"fd":1,"b64":"([A-Za-z0-9+/=]+)"\}', text)
    assert m and base64.b64decode(m.group(1)).decode().strip()


def test_s3_setup_json_examples_parse() -> None:
    blocks = _json_blocks((ROOT / "docs" / "s3-setup.md").read_text())
    assert len(blocks) >= 3


@pytest.mark.parametrize("doc", ["README.md", "docs/user-guide.md", "docs/s3-setup.md", "docs/format.md"])
def test_relative_links_resolve(doc: str) -> None:
    path = ROOT / doc
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", path.read_text()):
        if "://" in target:
            continue
        assert (path.parent / target).exists(), f"{doc}: broken link {target}"


def _repo_url() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["urls"]["Homepage"]


def test_readme_links_work_on_pypi() -> None:
    # PyPI shows README.md as the project description, where relative links
    # break: the README links to the repository with absolute URLs, and those
    # must point to files that exist.
    blob = _repo_url() + "/blob/main/"
    targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", (ROOT / "README.md").read_text())
    assert targets
    for target in targets:
        assert "://" in target, f"README.md: relative link {target} (breaks on PyPI)"
        if target.startswith(blob):
            assert (ROOT / target.removeprefix(blob)).exists(), f"README.md: broken link {target}"
