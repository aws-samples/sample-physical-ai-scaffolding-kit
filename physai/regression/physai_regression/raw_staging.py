"""Stage a Layer 2 raw fixture into ``/fsx/raw/<name>/`` on the cluster.

Three URI schemes are accepted:

- ``file:///abs/path`` — local source on the laptop. Path must be an
  absolute, existing directory; its contents are rsynced (over the
  regression's tempfile SSH config) into ``/fsx/raw/<name>/``.

- ``s3://bucket[/prefix/]`` — S3 source. The cluster's IAM role usually
  only has access to the project's own data bucket, so the cluster first
  probes the source with ``aws s3 ls``; on permission errors
  (``AccessDenied`` / ``NoSuchBucket`` / ``403`` / ``404``) it falls back
  to a laptop-driven path: list keys with the user's ``--profile`` /
  ``--region``, presign each key (presigned URLs are per-object —
  there is no "presign a prefix"), and have the cluster ``curl`` each
  URL into ``/fsx/raw/<name>/``. Other probe failures (network, region
  mismatch) propagate so we don't mask real problems.

- ``hf://owner/repo[@revision]`` — HuggingFace dataset. The cluster
  ephemerally installs ``huggingface_hub`` to ``/tmp/regression-hf-pkgs``
  via ``pip install --target`` (no virtualenv, no user-site pollution)
  and runs ``huggingface-cli download`` with ``PYTHONPATH=`` prepended.
  Public datasets only — no auth-token plumbing.

Every stager wipes ``/fsx/raw/<name>/`` before downloading so a partial
prior run can't contaminate the result. Caller-side cleanup (e.g.
removing ``/fsx/raw/<name>/`` after a successful test) is the test's
responsibility, not this module's.
"""

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from .orchestration.aws import s3_client

# Substrings in an `aws s3 ls` error that mean "the cluster's IAM role
# can't see this bucket/prefix" — i.e. fall back to laptop-driven
# presigning. Any other error (network, endpoint, region mismatch)
# propagates so a real platform problem isn't silently routed through the
# slower fallback path. These match only the parenthesized structured forms
# the AWS CLI emits ("An error occurred (AccessDenied) when calling ..." /
# "(403)") — not bare tokens like "403", "404", or "Forbidden", which also
# appear in bucket/prefix names (e.g. `my-dataset-404`) or in unrelated
# network/proxy error bodies the CLI echoes, and would wrongly divert a real
# platform failure into the slow presign fallback.
_S3_PERMISSION_HINTS = (
    "(AccessDenied)",
    "(NoSuchBucket)",
    "(403)",
    "(404)",
)

_HF_PKG_DIR = "/tmp/regression-hf-pkgs"


class RawSourceError(ValueError):
    """A ``--raw-source`` URI is malformed or its stager failed."""


# A parsed ``--raw-source`` URI, modelled as a sum type: one frozen
# dataclass per scheme, each carrying exactly the fields that scheme
# needs and no nullable placeholders for the others. ``parse_raw_source``
# is the sole constructor; ``stage_raw`` dispatches on the variant with
# an exhaustive ``match``. Every variant keeps ``raw`` (the original URI)
# so error messages can quote what the user typed.


@dataclass(frozen=True)
class FileSource:
    raw: str
    local_dir: Path


@dataclass(frozen=True)
class S3Source:
    raw: str
    bucket: str
    prefix: str  # "" or a trailing-slash-terminated key prefix


@dataclass(frozen=True)
class HFSource:
    raw: str
    owner: str
    repo: str
    revision: str | None = None


ParsedRawSource = FileSource | S3Source | HFSource


def parse_raw_source(uri: str) -> ParsedRawSource:
    """Parse ``uri`` into a :class:`ParsedRawSource` or raise :class:`RawSourceError`.

    Pure: no filesystem checks, no network calls. ``_file_stage`` later
    verifies the local directory exists; ``_s3_stage`` later probes IAM.
    """
    if not uri:
        raise RawSourceError("raw source URI is empty")
    if any(c.isspace() for c in uri):
        raise RawSourceError(f"raw source URI must not contain whitespace: {uri!r}")

    if uri.startswith("hf://"):
        return _parse_hf(uri)

    parts = urlsplit(uri)
    if parts.query or parts.fragment:
        raise RawSourceError(
            f"raw source URI must not contain query or fragment: {uri!r}"
        )
    if parts.scheme == "file":
        return _parse_file(uri, parts)
    if parts.scheme == "s3":
        return _parse_s3(uri, parts)
    raise RawSourceError(
        f"unsupported raw source scheme {parts.scheme!r} in {uri!r}; "
        "expected file://, s3://, or hf://"
    )


def _parse_file(uri: str, parts) -> ParsedRawSource:
    # Enforce the three-slash form `file:///abs/path` as the documented
    # canonical shape. urlsplit also accepts `file:/abs/path` (single
    # slash, empty netloc) and `file://host/path` (host as netloc), but
    # both forms confuse readers and the single-slash one is too easy
    # to mistype the protocol entirely.
    if not uri.startswith("file:///"):
        raise RawSourceError(
            f"file:// URIs must use the form file:///abs/path (got {uri!r})"
        )
    path = parts.path
    if not path or not path.startswith("/"):
        raise RawSourceError(f"file:// path must be absolute: {uri!r}")
    # Strip trailing slashes so `_file_stage` can append exactly one when
    # invoking rsync (rsync's "copy contents" semantics require the
    # trailing slash on src).
    return FileSource(raw=uri, local_dir=Path(path.rstrip("/")))


def _parse_s3(uri: str, parts) -> ParsedRawSource:
    bucket = parts.netloc
    if not bucket:
        raise RawSourceError(f"s3:// URI must include a bucket: {uri!r}")
    prefix = parts.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    return S3Source(raw=uri, bucket=bucket, prefix=prefix)


def _parse_hf(uri: str) -> ParsedRawSource:
    body = uri[len("hf://") :]
    if "/" not in body:
        raise RawSourceError(
            f"hf:// URI must be hf://owner/repo[@revision] (got {uri!r})"
        )
    owner, _, repo_with_rev = body.partition("/")
    if not owner or "/" in owner:
        raise RawSourceError(f"hf:// owner must be a single path segment: {uri!r}")
    if not repo_with_rev:
        raise RawSourceError(f"hf:// repo must not be empty: {uri!r}")
    revision: str | None = None
    if "@" in repo_with_rev:
        repo, _, revision = repo_with_rev.partition("@")
        if not revision:
            raise RawSourceError(f"hf:// revision after '@' must not be empty: {uri!r}")
    else:
        repo = repo_with_rev
    if not repo or "/" in repo:
        raise RawSourceError(f"hf:// repo must be a single path segment: {uri!r}")
    return HFSource(raw=uri, owner=owner, repo=repo, revision=revision)


def _dest(name: str) -> str:
    """The on-cluster destination directory for a staged raw with this ``name``."""
    return f"/fsx/raw/{name}/"


def _validate_name(name: str) -> None:
    """Reject names that would let a caller traverse out of /fsx/raw/."""
    if not name or "/" in name or ".." in Path(name).parts:
        raise RawSourceError(f"raw destination name is invalid: {name!r}")


def _reset_dest(session, name: str) -> None:
    """``rm -rf`` the destination then re-create it. Idempotent."""
    dest = shlex.quote(_dest(name))
    session.run(f"rm -rf {dest} && mkdir -p {dest}")


def stage_raw(
    session,
    uri: str,
    name: str,
    *,
    aws_profile: str | None,
    aws_region: str | None,
) -> None:
    """Stage ``uri`` into ``/fsx/raw/<name>/`` on the cluster.

    Wraps any underlying ``RuntimeError`` from ``Session.run`` /
    ``Session.rsync`` in :class:`RawSourceError` so callers see a single
    error type and can quote the URI in their failure message.
    """
    _validate_name(name)
    parsed = parse_raw_source(uri)
    try:
        match parsed:
            case FileSource():
                _file_stage(session, parsed, name)
            case S3Source():
                _s3_stage(session, parsed, name, aws_profile, aws_region)
            case HFSource():
                _hf_stage(session, parsed, name)
            case _:
                assert_never(parsed)
    except RawSourceError:
        raise
    except RuntimeError as e:
        raise RawSourceError(f"failed to stage {uri} at {_dest(name)}: {e}") from e

    # Backstop against silent staging failures for every scheme: each stager
    # wiped and re-populated /fsx/raw/<name>/, so an empty dest here means
    # nothing landed (an empty hf download, a sync that copied only zero-byte
    # dir markers, a wrong-region presign whose bodies were discarded, etc.).
    # `ls -A` lists dotfiles too, so empty stdout == empty dir.
    dest = shlex.quote(_dest(name))
    try:
        listing = session.run(f"ls -A {dest}")
    except RuntimeError as e:
        raise RawSourceError(
            f"failed to verify staged content at {_dest(name)} for {uri}: {e}"
        ) from e
    if not listing.strip():
        raise RawSourceError(
            f"staging {uri} produced no files at {_dest(name)} — the source may "
            f"be empty or the download silently failed"
        )


def _file_stage(session, parsed: FileSource, name: str) -> None:
    """``rsync`` a local directory's contents into ``/fsx/raw/<name>/``."""
    if not parsed.local_dir.is_dir():
        raise RawSourceError(f"file:// source is not a directory: {parsed.local_dir}")
    _reset_dest(session, name)
    # Trailing slash on src so rsync copies the directory's *contents*
    # into the destination rather than nesting the directory itself.
    session.rsync(f"{parsed.local_dir}/", _dest(name), show_progress=True)


def _s3_stage(
    session,
    parsed: S3Source,
    name: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> None:
    """Probe cluster IAM, then ``aws s3 sync`` or fall back to presigned URLs."""
    # Quote the S3 URI and destination before they reach the remote login
    # shell via ``session.run``: ``parse_raw_source`` rejects whitespace but
    # not other shell metacharacters, which are legal in S3 keys.
    src = shlex.quote(f"s3://{parsed.bucket}/{parsed.prefix}")
    dest = shlex.quote(_dest(name))
    try:
        listing = session.run(f"aws s3 ls {src}")
    except RuntimeError as e:
        msg = str(e)
        if not any(hint in msg for hint in _S3_PERMISSION_HINTS):
            raise RawSourceError(
                f"failed to probe {parsed.raw} from the cluster (not a "
                f"permission issue — check IAM/network/region): {e}"
            ) from e
        _s3_stage_via_presign(session, parsed, name, aws_profile, aws_region)
        return
    # `aws s3 ls` on a prefix with zero objects and zero common-prefixes
    # exits 0 with empty stdout; a subsequent `aws s3 sync` would copy nothing
    # and leave an empty /fsx/raw/<name>/. Reject that here — symmetric with
    # the presign path's "S3 prefix is empty" guard — so a mistyped or empty
    # prefix fails loudly instead of silently staging nothing.
    if not listing.strip():
        raise RawSourceError(f"S3 prefix is empty (no objects to fetch): {parsed.raw}")
    _reset_dest(session, name)
    session.run(f"aws s3 sync {src} {dest}")


def _s3_stage_via_presign(
    session,
    parsed: S3Source,
    name: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> None:
    """Laptop-driven fallback for buckets the cluster IAM can't read.

    Presigned URLs are per-object (S3 has no API to presign a prefix), so
    this lists every key under the prefix, presigns each one with the
    user's ``--profile``/``--region``, and has the cluster ``curl`` each
    URL into the matching relative path under ``/fsx/raw/<name>/``.
    """
    keys = _list_s3_keys(parsed.bucket, parsed.prefix, aws_profile, aws_region)
    # Skip directory-marker keys — S3 has no real directories; these are
    # zero-byte console artifacts whose key ends in "/".
    keys = [k for k in keys if not k.endswith("/")]
    if not keys:
        raise RawSourceError(f"S3 prefix is empty (no objects to fetch): {parsed.raw}")
    # A presigned URL binds its signing region into the signature, so a URL
    # signed for the wrong region gets a PermanentRedirect from S3 — and curl
    # writes the redirect's XML body to the output file. Resolve the bucket's
    # real region and presign against that, not the run region.
    bucket_region, region_fell_back = _bucket_region(
        parsed.bucket, aws_profile, aws_region
    )
    if region_fell_back:
        # The region couldn't be resolved; we're guessing with the run region.
        # If that guess is wrong, S3 returns a PermanentRedirect and curl
        # writes the redirect XML into each file — a silent corruption the
        # post-stage content check can't catch (the files are non-empty).
        # Warn loudly so the operator inspects.
        print(
            f"WARNING: could not resolve the home region of bucket "
            f"{parsed.bucket!r}; presigning against fallback region "
            f"{bucket_region!r}. If this is wrong, S3 returns a "
            f"PermanentRedirect and curl writes the redirect XML into each "
            f"downloaded file — inspect {_dest(name)} before trusting it.",
            file=sys.stderr,
        )
    urls = [_presign_s3_key(parsed.bucket, k, aws_profile, bucket_region) for k in keys]
    _reset_dest(session, name)
    dest = _dest(name)
    for key, url in zip(keys, urls, strict=True):
        relpath = key[len(parsed.prefix) :] if parsed.prefix else key
        target = f"{dest}{relpath}"
        # Single-quote the URL so its `&`, `?`, `=` don't break the
        # remote shell parse. Use shlex.quote on the *path* only as a
        # belt-and-braces guard against future relpath shapes.
        quoted_target = shlex.quote(target)
        session.run(
            f"mkdir -p $(dirname {quoted_target}) && "
            f"curl -fsSL '{url}' -o {quoted_target}"
        )


def _list_s3_keys(
    bucket: str, prefix: str, aws_profile: str | None, aws_region: str | None
) -> list[str]:
    """Return every key under ``s3://bucket/prefix/``.

    Uses the boto3 ``list_objects_v2`` paginator, which pages server-side, so
    prefixes with more than 1000 objects are handled without explicit
    continuation. A prefix with no objects yields pages with no ``Contents``
    member and returns an empty list.
    """
    client = s3_client(aws_profile, aws_region)
    keys: list[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except ClientError as e:
        raise RawSourceError(
            f"list-objects-v2 failed for s3://{bucket}/{prefix}: {e}"
        ) from e
    return keys


def _bucket_region(
    bucket: str, aws_profile: str | None, aws_region: str | None
) -> tuple[str | None, bool]:
    """Resolve ``bucket``'s home region for region-correct presigning.

    Returns ``(region, used_fallback)``. ``used_fallback`` is True when the
    region could not be resolved and the run region was returned as a last
    resort — the caller can then warn, since a wrong region makes every
    presigned URL redirect and ``curl`` write the redirect body to disk.

    Uses boto3 ``head_bucket``, whose response carries ``BucketRegion`` at
    the top level, resolves cross-region regardless of the client's region,
    and returns ``us-east-1`` explicitly (unlike the CLI's
    ``get-bucket-location``, which returns null for the legacy default and
    needed special-casing). ``head_bucket`` is authorized by ``s3:ListBucket``
    — already exercised by ``list_objects_v2`` on the way to this call. On any
    failure fall back to the run region rather than hard-failing: presign then
    surfaces the redirect error with a clearer message than this helper could.
    """
    client = s3_client(aws_profile, aws_region)
    try:
        resp = client.head_bucket(Bucket=bucket)
    except ClientError:
        return aws_region, True
    region = resp.get("BucketRegion")
    if not region:
        return aws_region, True
    return region, False


def _presign_s3_key(
    bucket: str, key: str, aws_profile: str | None, aws_region: str | None
) -> str:
    """Generate a 1-hour presigned GET URL for ``s3://bucket/key``.

    The client is built ``for_presign`` (SigV4 + virtual-hosted addressing)
    so the URL is region-correct: ``aws_region`` here is the bucket's home
    region (from :func:`_bucket_region`), and it binds into the signature. A
    default client would emit a region-less legacy SigV2 URL that
    PermanentRedirects when the bucket is not in the client's region.
    """
    client = s3_client(aws_profile, aws_region, for_presign=True)
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
    except ClientError as e:
        raise RawSourceError(f"presign failed for s3://{bucket}/{key}: {e}") from e


def _hf_stage(session, parsed: HFSource, name: str) -> None:
    """Ephemerally install ``huggingface_hub`` and download a public dataset."""
    # Fresh install so a half-installed prior run can't poison the
    # PYTHONPATH-prepended import lookup.
    session.run(
        f"rm -rf {_HF_PKG_DIR} && pip install --target {_HF_PKG_DIR} huggingface_hub"
    )
    _reset_dest(session, name)
    # Invoke the `hf` console script that pip drops in <target>/bin/ rather
    # than a `python -m <module>` path: the internal module layout has
    # moved across versions (the old huggingface_hub.commands.huggingface_cli
    # no longer exists), but the console-script entrypoint is the package's
    # stable public surface. PYTHONPATH points the script's imports at the
    # --target install.
    # Quote each operator-derived segment before it reaches the remote
    # shell. ``_parse_hf`` forbids '/' and whitespace in owner/repo but not
    # other shell metacharacters; quoting owner and repo separately keeps
    # the ``owner/repo`` repo-id intact.
    repo_id = f"{shlex.quote(parsed.owner)}/{shlex.quote(parsed.repo)}"
    download = (
        f"PYTHONPATH={_HF_PKG_DIR} {_HF_PKG_DIR}/bin/hf "
        f"download {repo_id} --repo-type dataset"
    )
    if parsed.revision is not None:
        download += f" --revision {shlex.quote(parsed.revision)}"
    download += f" --local-dir {shlex.quote(_dest(name))}"
    session.run(download)
