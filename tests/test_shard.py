"""The --shard option of the test configuration (used by CI to split the suite)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
TARGET = ["test_hashing.py", "test_envfilter.py"]


def collect(*args: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-n0", "-p", "no:cacheprovider", *args, *TARGET],
        cwd=TESTS, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if "::" in line]


def test_shards_partition_the_suite() -> None:
    full = collect()
    shards = [collect(f"--shard={k}/3") for k in (1, 2, 3)]
    assert all(shards)
    assert sorted(sum(shards, [])) == sorted(full)  # every test exactly once
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


@pytest.mark.parametrize("bad", ["0/3", "4/3", "x", "1-3"])
def test_bad_shard_is_a_usage_error(bad: str) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-n0", "-p", "no:cacheprovider", f"--shard={bad}",
         *TARGET],
        cwd=TESTS, capture_output=True, text=True,
    )
    assert r.returncode == 4, r.stdout + r.stderr  # pytest's usage error
