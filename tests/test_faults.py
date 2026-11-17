"""Fault tolerance: nothing in the cache may break or stall a run.

Faults are injected with COCOTB_CACHE_TEST_FAULT (see cache.fault) or by
pointing the client at a server that accepts connections but never answers.
Every run must complete with the normal result; the cache only degrades.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from cache_harness import Project, Run, ok
from s3_local import S3Endpoint

pytestmark = [pytest.mark.sim, pytest.mark.s3]


@pytest.fixture
def project(tmp_path: Path, local_s3: S3Endpoint, s3_prefix: str) -> Project:
    return Project(tmp_path, "counter", local_s3, s3_prefix)


def with_fault(p: Project, point: str, **kw) -> Run:
    env = {"COCOTB_CACHE_TEST_FAULT": point, **kw.pop("env", {})}
    return p.run("icarus", env=env, **kw)


def test_restrict_fault_runs_without_the_cache(project: Project) -> None:
    run = ok(with_fault(project, "restrict"))
    assert run.summary == (1, 0)
    assert "could not restrict the environment" in run.stderr
    assert run.phase_events("build") == [] and run.phase_events("test") == []


def test_prepare_fault_runs_without_the_cache(project: Project) -> None:
    run = ok(with_fault(project, "prepare"))
    assert run.summary == (1, 0)
    assert "internal error" in run.stderr
    assert project.stored_keys() == []


def test_tee_fault_runs_but_does_not_store(project: Project) -> None:
    run = ok(with_fault(project, "tee"))
    assert run.summary == (1, 0)
    assert "cannot capture the console" in run.stderr
    assert "stored" not in run.phase_events("build") + run.phase_events("test")
    assert project.stored_keys() == []
    assert ok(project.run("icarus")).phase_events("build") == ["miss", "stored"]  # nothing was cached


def test_sidecar_fault_loses_only_the_test_cache(project: Project) -> None:
    run = ok(with_fault(project, "sidecar"))
    assert run.summary == (1, 0)
    assert "cannot write" in run.stderr
    assert run.phase_events("build") == ["miss", "stored"]
    assert run.phase_events("test") == ["bypass"]


def test_publish_fault_disables_the_cache_for_the_process(project: Project) -> None:
    run = ok(with_fault(project, "publish"))
    assert run.summary == (1, 0)
    assert run.phase_events("build") == ["miss", "error"]
    assert run.phase_events("test") == ["bypass"]  # circuit breaker: no second attempt
    assert project.stored_keys() == []


class SilentServer:
    """Accepts TCP connections and never answers: a hanging S3."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self.conns.append(conn)

    def close(self) -> None:
        self.sock.close()
        for c in self.conns:
            c.close()


def test_hanging_s3_costs_one_timeout_not_minutes(project: Project) -> None:
    server = SilentServer()
    try:
        t0 = time.monotonic()
        run = ok(project.run("icarus", env={
            "AWS_ENDPOINT_URL": f"http://127.0.0.1:{server.port}",
            "COCOTB_CACHE_S3_READ_TIMEOUT": "1",
            "COCOTB_CACHE_S3_MAX_ATTEMPTS": "1",
        }))
        elapsed = time.monotonic() - t0
    finally:
        server.close()
    assert run.summary == (1, 0)
    assert "S3 lookup failed" in run.stderr
    assert run.phase_events("build") == ["error"]
    assert run.phase_events("test") == ["bypass"]  # the breaker skips the second lookup
    assert server.accepted == 1
    assert elapsed < 30, elapsed


def test_default_timeouts_are_short() -> None:
    from cocotb_cache.store import make_client

    cfg = make_client().meta.config
    assert cfg.connect_timeout == 5.0 and cfg.read_timeout == 30.0
    assert cfg.retries["total_max_attempts"] == 3
