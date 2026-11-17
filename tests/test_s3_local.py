"""The local S3 fixture works, is isolated, and behaves like a real S3 server."""

from __future__ import annotations

import os

import pytest
from botocore.exceptions import ClientError

from s3_local import S3Endpoint

pytestmark = pytest.mark.s3


def test_roundtrip(local_s3: S3Endpoint, s3_prefix: str) -> None:
    s3 = local_s3.client()
    key = s3_prefix + "hello.txt"
    s3.put_object(Bucket=local_s3.bucket, Key=key, Body=b"hello\n")
    assert s3.get_object(Bucket=local_s3.bucket, Key=key)["Body"].read() == b"hello\n"
    s3.delete_object(Bucket=local_s3.bucket, Key=key)
    assert s3.list_objects_v2(Bucket=local_s3.bucket, Prefix=s3_prefix).get("KeyCount", 0) == 0


def test_conditional_put_is_enforced(local_s3: S3Endpoint, s3_prefix: str) -> None:
    # The store relies on this for RESULT manifests (docs/format.md, "Flow"):
    # S3 servers return 412 here, and the local gateway must too.
    s3 = local_s3.client()
    key = s3_prefix + "result.json.gz"
    s3.put_object(Bucket=local_s3.bucket, Key=key, Body=b"first", IfNoneMatch="*")
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket=local_s3.bucket, Key=key, Body=b"second", IfNoneMatch="*")
    assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412
    assert s3.get_object(Bucket=local_s3.bucket, Key=key)["Body"].read() == b"first"


def test_prefixes_are_unique(s3_prefix: str) -> None:
    # A second request of the fixture in the same test is the same value;
    # uniqueness across tests is by uuid, checked structurally here.
    worker, unique = s3_prefix.split("/")[1:3]
    assert worker == os.environ.get("PYTEST_XDIST_WORKER", "master")
    assert len(unique) == 32


def test_client_ignores_ambient_aws_env(local_s3: S3Endpoint) -> None:
    # The container env points AWS_* at the real bucket; the fixture client
    # must not pick that up.
    client = local_s3.client()
    assert client.meta.endpoint_url.startswith("http://127.0.0.1:")
    creds = client._request_signer._credentials
    assert creds.access_key == local_s3.access_key
