# Setting up S3 storage for cocotb-cache

cocotb-cache stores results in an S3 bucket: Amazon S3 or any S3-compatible server. This page covers what the
storage must support, how to give writers and readers access, and how to keep the bucket from growing without
bound. For using the cache, see the [user guide](user-guide.md).

## What the storage must support

cocotb-cache uses a small part of the S3 API, always with **path-style** addressing
(`https://endpoint/bucket/key`), so S3-compatible servers work without DNS tricks:

| Operation | Used for |
|---|---|
| `GetObject` | reading results and files |
| `HeadObject` | checking whether a file is already stored before uploading it |
| `PutObject`, multipart upload (`CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`, `AbortMultipartUpload`) | storing; files over 8 MiB go as multipart uploads |
| `PutObject` with `If-None-Match: *` | publishing a result: the first of concurrent writers wins |
| `ListObjectsV2` | the `cocotb-cache` command (`inspect`, `stat`, `gc`) |
| `DeleteObject` | `cocotb-cache gc --delete` only |

**Conditional writes** (`If-None-Match: *`) are supported by Amazon S3 and by most current S3-compatible servers.
If a server rejects them as not implemented, cocotb-cache writes unconditionally, and if a server ignores the
header, the last writer wins. Both are safe: two results for the same key come from the same inputs.

**Checksums:** recent AWS SDKs send CRC checksums with uploads by default. Some older S3-compatible servers reject
these; if uploads fail with a checksum error, set `AWS_REQUEST_CHECKSUM_CALCULATION=when_required`. cocotb-cache
also verifies every file it reads against its own SHA-256.

## Creating the bucket

One bucket per project (or one shared bucket with a `COCOTB_CACHE_S3_PREFIX` per project). With the AWS CLI:

```sh
aws s3api create-bucket --bucket my-cocotb-cache --region eu-north-1 \
    --create-bucket-configuration LocationConstraint=eu-north-1
aws s3api put-public-access-block --bucket my-cocotb-cache \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Keep the bucket private: it holds compiled designs, simulator binaries and simulation logs, which reveal as much
as the sources do. Server-side encryption (on by default in Amazon S3) is recommended.

## Access: writers and readers

A typical setup has two kinds of credentials:

- **Writers** (CI): `COCOTB_CACHE=rw`, may read and write.
- **Readers** (developers): `COCOTB_CACHE=read`, may only read; they use the results CI produced.

**Readers need `s3:ListBucket` as well as `s3:GetObject`.** Without list permission, Amazon S3 answers a request for a
missing object with `403 Access Denied` instead of `404 Not Found`. cocotb-cache then cannot tell "not cached yet"
from a real error: it treats the first miss as an S3 failure and turns itself off for the rest of the process.

Example IAM policy for a **reader** (Amazon S3; replace the bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::my-cocotb-cache"},
    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::my-cocotb-cache/*"}
  ]
}
```

A **writer** additionally needs to upload:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::my-cocotb-cache"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
     "Resource": "arn:aws:s3:::my-cocotb-cache/*"}
  ]
}
```

and whoever runs garbage collection with `--delete` also `s3:DeleteObject`. With a shared bucket, restrict the
resources to the project's prefix (`arn:aws:s3:::my-cocotb-cache/myproject/*`, and an `s3:prefix` condition on
`ListBucket`).

On S3-compatible servers the same split is usually done with per-user policies or bucket ownership; see the
server's documentation. cocotb-cache never creates buckets, so the credentials do not need that right.

## Giving credentials to the cache

cocotb-cache uses the standard AWS client configuration, so any of these work:

- environment variables `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` for temporary
  credentials), `AWS_DEFAULT_REGION`;
- a profile in `~/.aws/credentials` and `~/.aws/config`, selected with `AWS_PROFILE`;
- an instance or container role, or OIDC federation in CI (for example GitHub Actions' `aws-actions/configure-aws-credentials`).

For an S3-compatible server, set `AWS_ENDPOINT_URL` to its address. Then point the cache at the bucket:

```sh
export COCOTB_CACHE=rw
export COCOTB_CACHE_S3_BUCKET=my-cocotb-cache
# export COCOTB_CACHE_S3_PREFIX=myproject/    # only for a shared bucket
```

Credentials are never written into the cache: `AWS_*` and other secret-looking variables are kept out of the stored
manifests and are not even passed to the simulator.

Check the setup with:

```sh
cocotb-cache stat
```

which lists the stored objects (none at first) or reports what is wrong with the access.

## Keeping the bucket in bounds

Build results can be large (a Verilator build directory, waveforms), and old results accumulate. Two mechanisms
work together:

1. **Expire old results with a lifecycle rule.** A result is the small JSON object under `v1/result/`; once it is
   gone, the next run of those inputs simply misses and stores a new one. For example, expire results after 30 days:

   ```json
   {
     "Rules": [
       {
         "ID": "expire-cocotb-cache-results",
         "Status": "Enabled",
         "Filter": {"Prefix": "v1/result/"},
         "Expiration": {"Days": 30}
       },
       {
         "ID": "abort-incomplete-uploads",
         "Status": "Enabled",
         "Filter": {},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
       }
     ]
   }
   ```

   ```sh
   aws s3api put-bucket-lifecycle-configuration --bucket my-cocotb-cache --lifecycle-configuration file://lifecycle.json
   ```

   With a prefix, put it in front: `"Prefix": "myproject/v1/result/"`.

2. **Remove what nothing refers to any more** with `cocotb-cache gc`. It keeps every file referenced by a remaining
   result and deletes the rest that is older than 24 hours (`--min-age`), plus runs that never completed:

   ```sh
   cocotb-cache gc            # report only
   cocotb-cache gc --delete   # delete
   ```

   Run it periodically, for example as a scheduled CI job with credentials that may delete.

Do not expire `v1/blobs/` with a lifecycle rule: a blob may still be referenced by a result, and a result whose
files are gone falls back to a normal run (so it is safe, but wastes the result).

## Timeouts

An unreachable S3 should cost a run seconds, not minutes, so cocotb-cache uses shorter timeouts than the AWS SDK
defaults: 5 s to connect, 30 s to read, 3 attempts in total per request (`COCOTB_CACHE_S3_CONNECT_TIMEOUT`,
`COCOTB_CACHE_S3_READ_TIMEOUT`, `COCOTB_CACHE_S3_MAX_ATTEMPTS`). After the first S3 error in a process the cache
stays off for the rest of it, and the runs continue normally.
