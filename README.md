# s3-compat-tester

A Python CLI that runs the same S3 API operations against several S3-compatible
endpoints (AWS S3, Wasabi, MinIO, LocalStack, or anything you define) and reports
where their behaviour differs: status codes, error codes, response headers, ETag
format, latency, and higher-level behaviour such as pagination and versioning.

```
s3compat run -t aws,wasabi
```

## What it does

For every endpoint it creates a fresh bucket and runs:

| Operation | What is exercised |
|---|---|
| `create_bucket` | CreateBucket (with LocationConstraint outside us-east-1), HeadBucket, GetBucketLocation, re-creating a bucket you already own, HEAD on a missing bucket |
| `put_get` | PutObject, GetObject with SHA-256 verification, HeadObject, ranged GET, GET/HEAD on a missing key, unsatisfiable range |
| `multipart` | Multipart upload of a 100 MiB object (configurable) built on the fly in 8 MiB parts, ListParts, Complete, full download with SHA-256 verification, ETag checked against MD5-of-part-MD5s `-N`, HEAD with `partNumber=1`, completing with undersized parts (expects `EntityTooSmall`) |
| `presigned` | Presigned GET fetched with `requests`, the same URL with a tampered signature, presigned PUT, and a URL fetched after it expired |
| `versioning` | Enable versioning, upload the same key twice, ListObjectVersions, GET an old version, delete (delete marker), GET after delete, GET with a bogus version id |
| `list_objects` | ListObjectsV2 pagination with continuation tokens (MaxKeys configurable), ListObjects v1 with markers, delimiter / CommonPrefixes, StartAfter, `MaxKeys=0` |
| `cleanup` | Always runs, even if an earlier step crashed: aborts open multipart uploads, deletes every version and delete marker, tries DeleteBucket on a non-empty bucket first (expects `BucketNotEmpty`), deletes the bucket |

Every API call is captured through botocore event hooks (`before-call`,
`after-call`, `after-call-error` in `s3compat/client.py`): HTTP status, error code,
latency, and key headers (`x-amz-request-id`, `x-amz-id-2`, `ETag`, `Server`,
`x-amz-version-id`, `Content-Length`, `Last-Modified`, `x-amz-server-side-encryption`,
and others), plus the full list of header names. Presigned requests made with
`requests` are recorded the same way. Retries are turned off (`max_attempts=1`)
so the tool sees what the server actually returned.

Calls are then aligned across endpoints by (operation step, API call, nth
occurrence) and compared. Differences are graded:

- **error**: HTTP status mismatch, error code mismatch, ETag format mismatch
  (`md5` vs `multipart-N` vs other), an operation passing on one endpoint and failing on another
- **warning**: a tracked header returned by one endpoint but not another (aggregated
  across calls), ETag quoting, behaviour observations that differ (e.g. whether
  `NextMarker` appears, version-id length, probe outcomes), calls never made because
  an earlier call failed
- **info**: `Server` header values

Output: a side-by-side table in the terminal (status, latency, ETag format and
error code per endpoint), a differences table, per-call latency p50/p95, and a
JSON and Markdown report in `reports/`.

Latency is measured from `before-call` to `after-call`, so it covers signing and
the HTTP round trip. For `GetObject` it is time to response headers; the body is
read afterwards (multipart download throughput is reported separately).

## Install

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Configuration

Credentials only ever come from environment variables.

| Endpoint | Variables | Defaults |
|---|---|---|
| `aws` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_ENDPOINT_URL_S3` | region `us-east-1`, AWS's own endpoint |
| `wasabi` | `WASABI_ACCESS_KEY_ID`, `WASABI_SECRET_ACCESS_KEY`, `WASABI_ENDPOINT`, `WASABI_REGION` | `https://s3.wasabisys.com`, `us-east-1` |
| `minio` | `MINIO_ACCESS_KEY_ID`, `MINIO_SECRET_ACCESS_KEY`, `MINIO_ENDPOINT`, `MINIO_REGION` | `http://localhost:9000`, `minioadmin`/`minioadmin`, path-style |
| `localstack` | `LOCALSTACK_ACCESS_KEY_ID`, `LOCALSTACK_SECRET_ACCESS_KEY`, `LOCALSTACK_ENDPOINT` | `http://localhost:4566`, `test`/`test`, path-style |

For a Wasabi region other than us-east-1 set both, e.g.
`WASABI_ENDPOINT=https://s3.eu-central-1.wasabisys.com WASABI_REGION=eu-central-1`.

More endpoints (Ceph, R2, Backblaze, ...) go in a TOML file. The file names env
vars; it never holds secrets:

```toml
[endpoints.ceph]
endpoint_url = "https://ceph.internal:7480"
region = "default"
addressing_style = "path"
# defaults to CEPH_ACCESS_KEY_ID / CEPH_SECRET_ACCESS_KEY if omitted
access_key_env = "CEPH_ACCESS_KEY_ID"
secret_key_env = "CEPH_SECRET_ACCESS_KEY"
```

`s3compat endpoints [-c file.toml]` shows what is configured and which
credentials are missing.

## Usage

```bash
# AWS vs Wasabi (needs both sets of credentials)
s3compat run -t aws,wasabi

# three-way, smaller multipart object, only some operations
s3compat run -t aws,wasabi,minio --multipart-size-mb 32 --ops multipart,presigned

# no cloud account: an in-process moto server stands in for "aws"
docker compose up -d localstack
s3compat run --moto -t aws,localstack --checksums when_required
```

Useful flags: `--multipart-size-mb` (default 100), `--part-size-mb` (default 8,
minimum 5), `--objects` and `--page-size` for the listing test, `--checksums
when_supported|when_required` (botocore's flexible checksum mode),
`--no-expiry-probe`, `--all-calls` to print every call rather than only the
mismatching ones, `--report-dir`, `--no-report`, `--fail-on-diff` to exit 1 on
any error-level difference (handy in CI).

The multipart object is built part by part from a seeded PRNG, hashed as it
is uploaded, and never written to disk.

### Docker

```bash
docker build -t s3compat .
docker run --rm -e WASABI_ACCESS_KEY_ID -e WASABI_SECRET_ACCESS_KEY \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -v "$PWD/reports:/work/reports" s3compat run -t aws,wasabi
```

`docker-compose.yml` has a LocalStack service, a MinIO service (profile `minio`,
image overridable with `MINIO_IMAGE`), and the CLI itself (profile `cli`).

## Tests

```bash
pytest                       # offline: moto (mock_aws) plus a threaded moto server
pytest --live                # also runs tests/test_live.py against real endpoints
```

The offline suite covers every operation under `mock_aws`, the header-capture
hooks (including transport failures and real HTTP through a moto server), the
alignment and diff logic, report rendering and the CLI. Live tests are marked
`live`, are skipped without `--live`, and each endpoint is skipped unless its
credentials are in the environment (or, for MinIO and LocalStack, its port is
reachable). `S3COMPAT_CHECKSUMS` and `S3COMPAT_LIVE_MULTIPART_MB` tune live runs.

CI (`.github/workflows/ci.yml`) runs the offline tests on 3.11 and 3.12, then
starts LocalStack as a service container, runs the moto-vs-LocalStack
comparison and the live tests against it, and uploads the reports.

## Results actually measured

**Wasabi has been run (2026-10-13), AWS has not.** There were no AWS
credentials, so real AWS S3 is still untested. Wasabi was compared against both
emulators in a three-way run (`s3compat run --moto -t aws,wasabi,localstack`, where
"aws" is the moto stand-in) in both checksum modes. `pytest --live` passed for
Wasabi and LocalStack. All test buckets were deleted afterwards, and the Wasabi
account was left with 0 buckets. Reports:
[`docs/sample-report/wasabi-default/`](docs/sample-report/wasabi-default/) and
[`docs/sample-report/wasabi-when-required/`](docs/sample-report/wasabi-when-required/).

### Wasabi (real service) vs the emulators

The CLI ran on the Windows host (Python 3.11, boto3 1.43) against `s3.wasabisys.com`
(us-east-1, server `WasabiS3/8.1.359`) over a home connection, with the default
100 MiB / 8 MiB multipart (13 parts).

| Finding | moto ("aws") | Wasabi | LocalStack 3.8 |
|---|---|---|---|
| All 7 operations, botocore **default** checksums (CRC32) | pass | **pass** | `put_get` and `multipart` fail (see 1. below) |
| All 7 operations, `--checksums when_required` | pass | pass | pass |
| Presigned GET with one signature character changed | 200 | **403 `SignatureDoesNotMatch`** | 200 |
| Presigned GET 2.5 s after a 1 s expiry | 200 | **403 `TimeExpired`** | 200 (403 `AccessDenied` only with validation turned on) |
| HEAD `?partNumber=1` on the 13-part object | 200 | **206** | 206 |
| Multipart ETag | `9e6f8d17…-13` | same, equal to MD5-of-part-MD5s | same |
| ListObjects v1, no delimiter, truncated page | `NextMarker` present | **absent** | absent |
| Version id | 36-char UUID | 32 chars, e.g. `001791942113484424095-dIG2y2SAne` | 32-char opaque |
| `x-amz-server-side-encryption` on object calls | never | **never** | `AES256` |
| `Content-Length` on bodyless responses (CompleteMultipartUpload, DeleteObjects, GetBucketVersioning, ...) | sent | **missing on 32 calls** | sent |
| `Content-Type` on bodyless responses | sent | missing on 54 calls | missing on 54 calls |
| `Last-Modified` on PutObject / UploadPart | sent | missing | missing |
| `x-amz-version-id` on CompleteMultipartUpload | `null` | absent | absent |

What matched on all three: the error codes for missing keys (`NoSuchKey`), bogus
versions (`NoSuchVersion`), undersized parts (`EntityTooSmall`) and the delete
marker flow; v2 pagination of 10/10/8 keys; `MaxKeys=0`; and the SHA-256 of the
100 MiB download.

The main thing this shows is that **neither emulator checks presigned signatures
or expiry by default, and Wasabi does.** A test suite that passes against
moto or LocalStack can still hide a broken presign flow.

Wasabi latency, per call (ms, `when_required` run, the round trip from this laptop):

| API call | n | p50 | p95 |
|---|---|---|---|
| PutObject (256 KiB) | 32 | 30.5 | 57.6 |
| GetObject | 9 | 33.9 | 70.6 |
| HeadObject | 4 | 30.2 | 32.7 |
| ListObjectsV2 | 6 | 37.2 | 75.2 |
| UploadPart (8 MiB) | 15 | 2,440 | 2,692 |
| CompleteMultipartUpload | 2 | 69.3 | 74.6 |

The multipart moved at 2.9–3.6 MiB/s up and 22–25 MiB/s down. That is this home
connection's bandwidth, not a Wasabi limit. moto and LocalStack were also reached
from the host in this run, so their latencies here aren't comparable (see 4. below).

**MinIO was not run either.** The plan was to compare against `minio/minio`, but
on 2026-10-12 Docker Hub answered `pull access denied for minio/minio, repository
does not exist` (and `quay.io/minio/minio` returned 401), so no MinIO image was
available. The `minio` endpoint and compose service are in place for when an image
is.

What did run, on 2026-10-12/13, was **moto server 5.2.4 standing in for AWS**
versus **LocalStack 3.8** (`localstack/localstack:3.8`, build 2024-10-08), both
over real HTTP. Both are emulators, so this exercises the tool and shows real
emulator discrepancies; it says nothing about how AWS itself behaves. The CLI ran
inside the project's Docker image (Python 3.11, boto3/botocore 1.43.112) on the same
Docker network as LocalStack, with the default 100 MiB / 8 MiB multipart (13 parts),
25+3 objects listed in pages of 10. Full reports are in
[`docs/sample-report/`](docs/sample-report/).

### 1. botocore's default checksums break LocalStack 3.8

With botocore's default `request_checksum_calculation=when_supported` (CRC32 on
uploads, response validation on downloads):

| Operation | moto | LocalStack 3.8 |
|---|---|---|
| `put_get` | passed | error: `FlexibleChecksumError: Expected checksum l/bm2g== did not match calculated checksum: c8q7fQ==` (on GetObject) |
| `multipart` | passed | error: first UploadPart returned **400 InvalidRequest**, "Checksum Type mismatch occurred, expected checksum Type: null, actual checksum Type: crc32" |

With `--checksums when_required` all seven operations pass on both. Report:
`docs/sample-report/default-checksums/`.

### 2. Differences with checksums set to `when_required`

All operations passed on both endpoints. Report: `docs/sample-report/when-required/`.

| Finding | moto | LocalStack |
|---|---|---|
| HEAD `?partNumber=1` on a 13-part object | **200**, ETag of part 1 (`md5`) | **206**, whole-object ETag `…-13` |
| Both return `x-amz-mp-parts-count` | `13` | `13` |
| ListObjects v1 without delimiter, truncated page | `NextMarker` present | `NextMarker` absent |
| Version id format | 36-char UUID | 32-char opaque string |
| `x-amz-request-id`, `x-amz-id-2` | never sent (sends `x-amzn-requestid` instead) on 96 calls | always sent |
| `x-amz-server-side-encryption` | never sent | `AES256` on object calls |
| GET on a key whose latest version is a delete marker (404) | no `x-amz-delete-marker` / `x-amz-version-id` | both headers present |
| CompleteMultipartUpload on an unversioned bucket | `x-amz-version-id: null` | header absent |
| `Content-Type` on bodyless responses (PutObject, DeleteObject, CreateBucket, ...) | sent (`text/html; charset=utf-8` on PutObject) | absent on 54 calls |
| `Last-Modified` on PutObject / UploadPart | sent | absent on 48 calls |
| `Server` | `Werkzeug/3.1.9 Python/3.11.17` (plus `, amazon.com` on XML responses) | `TwistedWeb/24.3.0` |

Behaviour that matched on both: multipart ETag `9e6f8d17b41990d1fe85a1892c623a23-13`,
identical on both and equal to MD5-of-part-MD5s; part ETags equal to part MD5s;
SHA-256 of the 100 MiB download matched; re-creating your own bucket in us-east-1
returns 200; GET of a missing key 404 `NoSuchKey`; HEAD of missing key/bucket
404 with no body (botocore reports code `404`); unsatisfiable range 416
`InvalidRange`; undersized parts 400 `EntityTooSmall`; non-empty DeleteBucket 409
`BucketNotEmpty`; bogus version id 404 `NoSuchVersion`; `MaxKeys=0` 200;
v2 pagination returned 3 pages (10/10/8 keys) on both.

### 3. Presigned URL validation

Out of the box, neither emulator checks presigned signatures or expiry: a URL with
one signature character flipped, and a URL fetched 2.5 s after a 1 s expiry, both
returned **200** with the object on moto and on LocalStack.

Restarting LocalStack with `S3_SKIP_SIGNATURE_VALIDATION=0`
(`docs/sample-report/signature-validation/`):

| Request | moto | LocalStack (validation on) |
|---|---|---|
| Tampered signature | 200 | **403 `SignatureDoesNotMatch`** |
| Expired URL | 200 | **403 `AccessDenied`** |

### 4. Latency

Per-call latency from the `when-required` run (ms, both servers reached over the
Docker network from the same container):

| API call | n | moto p50 | moto p95 | LocalStack p50 | LocalStack p95 |
|---|---|---|---|---|---|
| PutObject | 32 | 5.9 | 8.7 | 43.7 | 47.8 |
| GetObject | 9 | 4.7 | 37.6 | 3.0 | 6.2 |
| HeadObject | 4 | 4.4 | 5.1 | 4.0 | 7.0 |
| UploadPart (8 MiB) | 15 | 83.7 | 248.2 | 60.1 | 129.6 |
| CompleteMultipartUpload | 2 | 1578.1 | 2992.4 | 152.6 | 286.6 |
| ListObjectsV2 | 6 | 10.8 | 12.7 | 4.4 | 6.8 |
| ListObjectVersions | 3 | 874.5 | 1351.6 | 4.8 | 7.7 |
| PresignedGet | 1 | 7.0 | 7.0 | 4.7 | 4.7 |

CompleteMultipartUpload n=2 is the 100 MiB upload plus the tiny rejected one, so its
p95 is roughly the big completion. Multipart throughput varied a lot between runs
on the same machine:

| Run | moto upload / download MiB/s | LocalStack upload / download MiB/s |
|---|---|---|
| default checksums | 7.3 / 249.4 | n/a (failed) |
| when_required | 40.9 / 326.0 | 62.7 / 224.2 |
| signature validation | 66.9 / 325.0 | 76.6 / 270.2 |

Running the same suite from the Windows host against moto on 127.0.0.1 gave
~100-160 ms per call even for ListBuckets, against ~5 ms inside Docker, so host
latencies from that setup aren't comparable and aren't used above.

## Layout

```
s3compat/
  config.py      endpoint specs, env-var resolution, TOML overrides
  client.py      boto3 client factory, CallRecorder (botocore event hooks)
  operations.py  the operation suite, checks and cleanup
  compare.py     alignment of calls, difference detection, latency stats
  report.py      terminal tables, JSON and Markdown reports
  cli.py         argparse entry point (`s3compat run`, `s3compat endpoints`)
tests/           offline tests (moto) and opt-in live tests
docs/sample-report/  reports from the runs described above
```
