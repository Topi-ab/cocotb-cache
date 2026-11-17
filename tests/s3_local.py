"""Local S3 endpoint for hermetic tests: versitygw as a subprocess.

Each pytest-xdist worker starts its own versitygw (own port, own data
directory), so workers never share state and no lock is needed. The binary is
pinned in docker/Dockerfile.

Safety: the process gets an environment without ``AWS_*`` variables, and the
clients built here always pass an explicit localhost endpoint and test
credentials, so a test can never reach the real bucket through the ambient
``AWS_*`` configuration of the container.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config

# Test-only root credentials of the local gateway; not secrets.
ACCESS_KEY = "cocotb-cache-test"
SECRET_KEY = "cocotb-cache-test-secret-not-a-real-key"
REGION = "us-east-1"
BUCKET = "cocotb-cache-test"

START_TIMEOUT_S = 20.0
START_ATTEMPTS = 3


@dataclass(frozen=True)
class S3Endpoint:
    """Connection details of an S3 endpoint used by tests."""

    url: str
    access_key: str
    secret_key: str
    region: str
    bucket: str

    def client(self):  # -> botocore S3 client (no stub types installed)
        return boto3.client(
            "s3",
            endpoint_url=self.url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}


class LocalVersityGW:
    """A versitygw process serving a POSIX directory on 127.0.0.1."""

    def __init__(self, data_dir: Path, log_path: Path) -> None:
        self.data_dir = data_dir
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self.endpoint: S3Endpoint | None = None

    def start(self) -> S3Endpoint:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        last_error = ""
        # The free port is only reserved until it is closed, so another
        # process may take it before versitygw binds; retry on a new port.
        for _ in range(START_ATTEMPTS):
            port = _free_port()
            env = _clean_env()
            env.update(
                ROOT_ACCESS_KEY=ACCESS_KEY,
                ROOT_SECRET_KEY=SECRET_KEY,
                VGW_PORT=f"127.0.0.1:{port}",
            )
            log = open(self.log_path, "ab")
            self.proc = subprocess.Popen(
                ["versitygw", "posix", str(self.data_dir)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            log.close()
            endpoint = S3Endpoint(f"http://127.0.0.1:{port}", ACCESS_KEY, SECRET_KEY, REGION, BUCKET)
            try:
                self._wait_ready(endpoint)
            except RuntimeError as e:
                last_error = str(e)
                self.stop()
                continue
            self.endpoint = endpoint
            return endpoint
        raise RuntimeError(f"versitygw did not start: {last_error}")

    def _wait_ready(self, endpoint: S3Endpoint) -> None:
        assert self.proc is not None
        client = endpoint.client()
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"exited with {self.proc.returncode}: {self._log_tail()}")
            try:
                client.list_buckets()
                return
            except Exception as e:  # not listening yet
                if time.monotonic() > deadline:
                    raise RuntimeError(f"not ready after {START_TIMEOUT_S}s ({e}): {self._log_tail()}")
                time.sleep(0.05)

    def _log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "<no log>"

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


def assert_local(endpoint: S3Endpoint) -> None:
    """Guard against a test fixture pointing anywhere but localhost."""
    host = urlparse(endpoint.url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise AssertionError(f"test S3 endpoint is not local: {endpoint.url}")
