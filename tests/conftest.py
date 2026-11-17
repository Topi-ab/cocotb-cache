"""Shared pytest configuration for the cocotb-cache test suite.

All tests run in parallel under pytest-xdist (see pyproject.toml), so fixtures
here must give every test its own resources (directories, S3 key prefixes).
pytest-xdist's built-in ``worker_id`` fixture ("gw0", ... or "master" with -n0)
is the basis for per-worker names.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from s3_local import LocalVersityGW, S3Endpoint, assert_local


@pytest.fixture(scope="session")
def local_s3(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Iterator[S3Endpoint]:
    """A local versitygw for this xdist worker, with an empty test bucket.

    Session scope under xdist means one instance per worker process, so
    workers never share an endpoint.
    """
    base = tmp_path_factory.mktemp(f"versitygw-{worker_id}")
    gw = LocalVersityGW(base / "data", base / "versitygw.log")
    endpoint = gw.start()
    try:
        assert_local(endpoint)
        endpoint.client().create_bucket(Bucket=endpoint.bucket)
        yield endpoint
    finally:
        gw.stop()


@pytest.fixture
def s3_prefix(worker_id: str) -> str:
    """A key prefix unique to the calling test (ends with "/")."""
    return f"t/{worker_id}/{uuid.uuid4().hex}/"


@pytest.fixture(autouse=True)
def no_real_s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may reach the real bucket through the container's environment.

    The dev container carries the project's S3 credentials and bucket (secrets/
    s3.env). Every test runs without AWS_* and COCOTB_CACHE_*; tests that need
    S3 use the local versitygw (local_s3) explicitly. The cache's per-process
    state is reset so a context from one test never leaks into another.
    """
    for name in list(os.environ):
        if name.startswith(("AWS_", "COCOTB_CACHE")):
            monkeypatch.delenv(name)
    from cocotb_cache import cache

    cache._context, cache._context_failed = None, False
    cache._warned.clear()
    yield
    cache._context, cache._context_failed = None, False


# --- sharding: split the suite between CI jobs ---------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only the K-th of N shards of the collected tests (1 <= K <= N); "
        "the N shards together run every test exactly once",
    )


def _shard(config: pytest.Config) -> tuple[int, int] | None:
    value = config.getoption("--shard")
    if value is None:
        return None
    try:
        k, n = (int(x) for x in value.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard must be K/N, got {value!r}") from None
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard needs 1 <= K <= N, got {value!r}")
    return k, n


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = _shard(config)
    if shard is None:
        return
    k, n = shard
    # Round robin over the sorted node IDs: deterministic (every xdist worker
    # selects the same tests) and spreads the parameters of one slow test over
    # the shards.
    order = {item.nodeid: i for i, item in enumerate(sorted(items, key=lambda it: it.nodeid))}
    keep = [it for it in items if order[it.nodeid] % n == k - 1]
    drop = [it for it in items if order[it.nodeid] % n != k - 1]
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
