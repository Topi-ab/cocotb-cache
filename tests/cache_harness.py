"""Harness for end-to-end cache tests.

A :class:`Project` is a private project root (its own ``.git``) holding a copy
of one test design, so a test may modify sources without affecting parallel
tests. :meth:`Project.run` runs the design the way a user would, in a
subprocess with the cache on, against the worker's local versitygw and the
test's own key prefix:

* ``style="script"``: a plain runner script (``python my_runner.py``);
* ``style="pytest"``: a pytest test file that calls the runner (cocotb then
  names the results file after the test and exits on failing tests).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from cocotb_tools.runner import get_results

from hdl.designs import DESIGNS
from s3_local import S3Endpoint

TESTS_DIR = Path(__file__).resolve().parent

_BODY = """
from pathlib import Path
from cocotb_tools.runner import get_runner
from hdl.designs import DESIGNS, SIMULATORS

def run_bench(cfg):
    d, sim = DESIGNS[cfg["design"]], cfg["sim"]
    lang = SIMULATORS[sim]
    ddir = Path(cfg["design_dir"])
    files = d.vhdl_files if lang == "vhdl" else d.sv_files
    incs = d.sv_include_dirs if lang != "vhdl" else ()
    params = cfg.get("parameters", d.parameters)
    build_dir = Path(cfg["build_dir"])
    test_dir = build_dir if sim == "ghdl" else Path(cfg["test_dir"])
    r = get_runner(sim)
    r.build(sources=[ddir / f for f in files], includes=[ddir / i for i in incs], hdl_toplevel=d.toplevel,
            parameters=params, build_dir=build_dir, **d.build_kwargs(lang), **cfg.get("build_kw", {}))
    return r.test(test_module=cfg.get("test_module", d.test_module), hdl_toplevel=d.toplevel, parameters=params, build_dir=build_dir,
                  test_dir=test_dir, seed=cfg.get("seed"), extra_env=cfg.get("extra_env", {}))
"""

SCRIPT = (
    "import json, sys\n"
    + _BODY
    + textwrap.dedent(
        """
        cfg = json.loads(sys.argv[1])
        results = run_bench(cfg)
        print("RESULTS " + str(results))
        """
    )
)

PYTEST_FILE = (
    "import json\nfrom pathlib import Path\n"
    + _BODY
    + textwrap.dedent(
        """
        def test_bench():
            cfg = json.loads((Path(__file__).parent / "cfg.json").read_text())
            run_bench(cfg)
        """
    )
)


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    events: list[dict]
    summary: tuple[int, int] | None
    results_xml: bytes | None

    def phase_events(self, phase: str) -> list[str]:
        return [e["event"] for e in self.events if e.get("phase") == phase and "event" in e]


class Project:
    """A private project root with a copy of one design, plus run helpers."""

    def __init__(self, tmp_path: Path, design: str, local_s3: S3Endpoint, prefix: str) -> None:
        self.root = tmp_path / "proj"
        self.design = design
        self.design_dir = self.root / "hdl" / design
        shutil.copytree(DESIGNS[design].directory, self.design_dir, ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / ".git").mkdir()  # makes it the project root
        self.tmp = tmp_path
        self.local_s3 = local_s3
        self.prefix = prefix
        self.runs = 0

    def env(self, mode: str = "rw", **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COCOTB_CACHE", "AWS_"))}
        env.update(
            COCOTB_CACHE=mode,
            COCOTB_CACHE_ROOT=str(self.root),
            COCOTB_CACHE_S3_BUCKET=self.local_s3.bucket,
            COCOTB_CACHE_S3_PREFIX=self.prefix,
            AWS_ENDPOINT_URL=self.local_s3.url,
            AWS_ACCESS_KEY_ID=self.local_s3.access_key,
            AWS_SECRET_ACCESS_KEY=self.local_s3.secret_key,
            AWS_DEFAULT_REGION=self.local_s3.region,
            PYTHONPATH=os.pathsep.join([str(TESTS_DIR), str(self.design_dir)]),
        )
        env.update(extra)
        return env

    def run(
        self,
        sim: str,
        *,
        style: str = "script",
        seed: int | None = 1,
        mode: str = "rw",
        parameters: dict | None = None,
        build_kw: dict | None = None,
        extra_env: dict | None = None,
        env: dict | None = None,
        test_module: str | None = None,
    ) -> Run:
        self.runs += 1
        work = self.tmp / f"run{self.runs}"
        work.mkdir()
        trace = work / "trace.jsonl"
        cfg = {
            "design": self.design,
            "sim": sim,
            "design_dir": str(self.design_dir),
            "build_dir": str(work / "build"),
            "test_dir": str(work / "test"),
            "seed": seed,
            "build_kw": build_kw or {},
            "extra_env": extra_env or {},
        }
        if parameters is not None:
            cfg["parameters"] = parameters
        if test_module is not None:
            cfg["test_module"] = test_module
        full_env = self.env(mode, COCOTB_CACHE_TRACE=str(trace), **(env or {}))
        if style == "script":
            script = work / "my_runner.py"
            script.write_text(SCRIPT)
            cmd = [sys.executable, str(script), json.dumps(cfg)]
        elif style == "pytest":
            (work / "cfg.json").write_text(json.dumps(cfg))
            test_file = work / "test_user_bench.py"
            test_file.write_text(PYTEST_FILE)
            cmd = [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider", "--rootdir", str(work),
                   str(test_file)]
        else:
            raise ValueError(style)
        res = subprocess.run(cmd, env=full_env, cwd=self.root, capture_output=True, text=True, timeout=600)
        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        results = self._results_file(res.stdout, style, sim, work)
        summary = tuple(get_results(results)) if results is not None and results.exists() else None
        xml = results.read_bytes() if results is not None and results.exists() else None
        return Run(res.returncode, res.stdout, res.stderr, events, summary, xml)

    @staticmethod
    def _results_file(stdout: str, style: str, sim: str, work: Path) -> Path | None:
        if style == "script":
            for line in stdout.splitlines():
                if line.startswith("RESULTS "):
                    return Path(line[8:])
            return None
        test_dir = work / "build" if sim == "ghdl" else work / "test"
        return test_dir / "test_bench.result.xml"  # cocotb's name under pytest

    def stored_keys(self) -> list[str]:
        s3 = self.local_s3.client()
        resp = s3.list_objects_v2(Bucket=self.local_s3.bucket, Prefix=self.prefix)
        return [o["Key"][len(self.prefix) :] for o in resp.get("Contents", [])]


def ok(run: Run) -> Run:
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    return run
