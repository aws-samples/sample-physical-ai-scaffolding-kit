"""Tests for ``physai_regression.raw_staging``.

The stagers reach two boundaries: ``Session.run`` / ``Session.rsync`` for
cluster-side commands (over SSH), and the boto3 client factory
(``raw_staging.s3_client``) for laptop-side S3 calls. Tests mock both. No
network, no SSH, no filesystem beyond ``tmp_path`` for the file:// stager.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from physai_regression import raw_staging
from physai_regression.raw_staging import (
    FileSource,
    HFSource,
    RawSourceError,
    S3Source,
    parse_raw_source,
    stage_raw,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _s3_client_error(code: str, message: str = "boom") -> ClientError:
    """Build a botocore ClientError for the laptop-side S3 client mocks."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "S3Op")


# ── parse_raw_source ──────────────────────────────────────────────────────


def test_parse_file_absolute_path():
    parsed = parse_raw_source("file:///abs/path/to/raw")
    assert isinstance(parsed, FileSource)
    assert parsed.local_dir == Path("/abs/path/to/raw")


def test_parse_file_strips_trailing_slash():
    parsed = parse_raw_source("file:///abs/path/")
    assert isinstance(parsed, FileSource)
    assert parsed.local_dir == Path("/abs/path")


def test_parse_file_rejects_relative_via_netloc():
    # `file://relative/path` parses with netloc=`relative`, path=`/path`;
    # we reject because file:// must use file:///abs/path form.
    with pytest.raises(RawSourceError, match="form file:///abs/path"):
        parse_raw_source("file://relative/path")


def test_parse_file_rejects_single_slash():
    # `file:/abs/path` has empty netloc but the parser still rejects
    # because it doesn't match the expected three-slash form.
    with pytest.raises(RawSourceError):
        parse_raw_source("file:/abs/path")


def test_parse_s3_with_prefix():
    parsed = parse_raw_source("s3://bucket/prefix/")
    assert isinstance(parsed, S3Source)
    assert parsed.bucket == "bucket"
    assert parsed.prefix == "prefix/"


def test_parse_s3_adds_trailing_slash():
    parsed = parse_raw_source("s3://bucket/prefix")
    assert isinstance(parsed, S3Source)
    assert parsed.prefix == "prefix/"


def test_parse_s3_empty_prefix_form():
    parsed = parse_raw_source("s3://bucket")
    assert isinstance(parsed, S3Source)
    assert parsed.bucket == "bucket"
    assert parsed.prefix == ""

    parsed = parse_raw_source("s3://bucket/")
    assert isinstance(parsed, S3Source)
    assert parsed.bucket == "bucket"
    assert parsed.prefix == ""


def test_parse_s3_nested_prefix():
    parsed = parse_raw_source("s3://bucket/a/b/c/")
    assert isinstance(parsed, S3Source)
    assert parsed.prefix == "a/b/c/"


def test_parse_s3_rejects_no_bucket():
    with pytest.raises(RawSourceError, match="must include a bucket"):
        parse_raw_source("s3:///prefix/")


def test_parse_s3_rejects_query_or_fragment():
    with pytest.raises(RawSourceError, match="query or fragment"):
        parse_raw_source("s3://bucket/prefix/?foo=bar")
    with pytest.raises(RawSourceError, match="query or fragment"):
        parse_raw_source("s3://bucket/prefix/#frag")


def test_parse_hf_no_revision():
    parsed = parse_raw_source("hf://owner/repo")
    assert isinstance(parsed, HFSource)
    assert parsed.owner == "owner"
    assert parsed.repo == "repo"
    assert parsed.revision is None


def test_parse_hf_with_revision():
    parsed = parse_raw_source("hf://owner/repo@v1.2.3")
    assert isinstance(parsed, HFSource)
    assert parsed.owner == "owner"
    assert parsed.repo == "repo"
    assert parsed.revision == "v1.2.3"


def test_parse_hf_with_branch_revision():
    parsed = parse_raw_source("hf://owner/repo@main")
    assert isinstance(parsed, HFSource)
    assert parsed.revision == "main"


def test_parse_hf_rejects_owner_only():
    with pytest.raises(RawSourceError, match="hf://owner/repo"):
        parse_raw_source("hf://just-owner")


def test_parse_hf_rejects_empty_repo():
    with pytest.raises(RawSourceError, match="repo must not be empty"):
        parse_raw_source("hf://owner/")


def test_parse_hf_rejects_empty_revision_after_at():
    with pytest.raises(RawSourceError, match="revision after '@' must not be empty"):
        parse_raw_source("hf://owner/repo@")


def test_parse_hf_rejects_extra_path_segments():
    with pytest.raises(RawSourceError, match="single path segment"):
        parse_raw_source("hf://owner/repo/extra")


def test_parse_rejects_unknown_scheme():
    with pytest.raises(RawSourceError, match="unsupported raw source scheme"):
        parse_raw_source("gcs://bucket/key")


def test_parse_rejects_bare_path():
    with pytest.raises(RawSourceError, match="unsupported raw source scheme"):
        parse_raw_source("/abs/path")


def test_parse_rejects_empty():
    with pytest.raises(RawSourceError, match="empty"):
        parse_raw_source("")


def test_parse_rejects_whitespace():
    with pytest.raises(RawSourceError, match="whitespace"):
        parse_raw_source("file:///abs/has space/path")


# ── _file_stage ───────────────────────────────────────────────────────────


def test_file_stage_rsyncs_with_trailing_slash(tmp_path: Path):
    src = tmp_path / "raw"
    src.mkdir()
    session = MagicMock()
    parsed = FileSource(raw=f"file://{src}", local_dir=src)
    raw_staging._file_stage(session, parsed, "name-x")
    # Reset, then rsync.
    assert session.run.call_count == 1
    assert "rm -rf /fsx/raw/name-x/" in session.run.call_args.args[0]
    assert "mkdir -p /fsx/raw/name-x/" in session.run.call_args.args[0]
    session.rsync.assert_called_once()
    src_arg, dst_arg = session.rsync.call_args.args[:2]
    assert src_arg == f"{src}/"  # trailing slash on src for "copy contents"
    assert dst_arg == "/fsx/raw/name-x/"


def test_file_stage_rejects_non_directory(tmp_path: Path):
    not_a_dir = tmp_path / "missing"
    session = MagicMock()
    parsed = FileSource(raw=f"file://{not_a_dir}", local_dir=not_a_dir)
    with pytest.raises(RawSourceError, match="not a directory"):
        raw_staging._file_stage(session, parsed, "name")
    session.run.assert_not_called()
    session.rsync.assert_not_called()


# ── _s3_stage cluster path ────────────────────────────────────────────────


def test_s3_stage_uses_cluster_sync_when_iam_allows():
    """Probe succeeds → the cluster does ``aws s3 sync`` itself."""
    session = MagicMock()
    # Probe succeeds; subsequent calls (reset + sync) also succeed.
    session.run.side_effect = ["object-listing-line", "", ""]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with patch.object(raw_staging, "s3_client") as laptop_s3:
        raw_staging._s3_stage(session, parsed, "name", None, None)
    # No laptop S3 calls — cluster handled everything.
    laptop_s3.assert_not_called()
    cluster_cmds = [c.args[0] for c in session.run.call_args_list]
    assert cluster_cmds[0] == "aws s3 ls s3://b/p/"
    assert "rm -rf /fsx/raw/name/" in cluster_cmds[1]
    assert cluster_cmds[2] == "aws s3 sync s3://b/p/ /fsx/raw/name/"


def test_s3_stage_empty_prefix_on_sync_path_raises():
    """Probe succeeds but lists nothing → error, symmetric with presign path.

    An empty listing means `aws s3 sync` would copy nothing and leave an
    empty dest; reject before the pointless sync/reset.
    """
    session = MagicMock()
    session.run.side_effect = [""]  # probe returns an empty listing
    parsed = S3Source(raw="s3://b/empty/", bucket="b", prefix="empty/")
    with patch.object(raw_staging, "s3_client") as laptop_s3:
        with pytest.raises(RawSourceError, match="empty"):
            raw_staging._s3_stage(session, parsed, "name", None, None)
    laptop_s3.assert_not_called()
    # Only the probe ran; reset + sync never happened.
    cluster_cmds = [c.args[0] for c in session.run.call_args_list]
    assert cluster_cmds == ["aws s3 ls s3://b/empty/"]


def test_s3_stage_whitespace_only_listing_treated_as_empty():
    session = MagicMock()
    session.run.side_effect = ["   \n  "]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with pytest.raises(RawSourceError, match="empty"):
        raw_staging._s3_stage(session, parsed, "name", None, None)


# ── _s3_stage presign fallback ────────────────────────────────────────────
#
# These integration tests drive the fallback path by patching the laptop-side
# helper functions (_list_s3_keys / _bucket_region / _presign_s3_key), which
# keeps them independent of whether those helpers use the AWS CLI or boto3.
# Focused unit tests for each helper's own S3-client wiring live further down.


def test_s3_stage_falls_back_to_presigned_urls_on_access_denied():
    """Probe AccessDenied → laptop lists+presigns; cluster curls each URL."""
    session = MagicMock()
    # Probe denies; subsequent reset + 2 curls all succeed.
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"),
        "",  # reset
        "",  # curl 1
        "",  # curl 2
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")

    with (
        patch.object(
            raw_staging,
            "_list_s3_keys",
            return_value=["p/file.hdf5", "p/sub/nested.bin"],
        ),
        patch.object(raw_staging, "_bucket_region", return_value=("us-west-2", False)),
        patch.object(
            raw_staging,
            "_presign_s3_key",
            side_effect=lambda bucket, key, *a, **k: (
                f"https://example.com/{key}?sig=xyz"
            ),
        ) as presign,
    ):
        raw_staging._s3_stage(session, parsed, "name", "prof", "us-west-2")

    assert presign.call_count == 2  # one per object

    # Cluster: probe (raised), reset, then 2 curls (one per key).
    cluster_cmds = [c.args[0] for c in session.run.call_args_list]
    assert cluster_cmds[0] == "aws s3 ls s3://b/p/"
    assert "rm -rf /fsx/raw/name/" in cluster_cmds[1]
    # Two curl commands, one each for top-level and sub/ key. The
    # subdirectory key's mkdir must resolve to a non-trivial path.
    curl_cmds = cluster_cmds[2:]
    assert len(curl_cmds) == 2
    assert any("/fsx/raw/name/file.hdf5" in c for c in curl_cmds)
    assert any("/fsx/raw/name/sub/nested.bin" in c for c in curl_cmds)
    # Presigned URLs are single-quoted so `&`/`?` survive shell parse.
    assert all("curl -fsSL '" in c for c in curl_cmds)


def test_s3_stage_fallback_skips_directory_marker_keys():
    """Zero-byte console-artifact keys ending in ``/`` are dropped."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"),
        "",  # reset
        "",  # curl 1
        "",  # curl 2
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")

    with (
        patch.object(
            raw_staging,
            "_list_s3_keys",
            return_value=["p/file.hdf5", "p/dir/", "p/other.bin"],
        ),
        patch.object(raw_staging, "_bucket_region", return_value=("us-west-2", False)),
        patch.object(
            raw_staging, "_presign_s3_key", return_value="https://example.com/x?sig=xyz"
        ) as presign,
    ):
        raw_staging._s3_stage(session, parsed, "name", None, None)

    assert presign.call_count == 2  # the "p/dir/" marker was skipped
    cluster_cmds = [c.args[0] for c in session.run.call_args_list]
    curl_cmds = [c for c in cluster_cmds if "curl -fsSL" in c]
    assert len(curl_cmds) == 2


def test_s3_stage_fallback_presigns_against_bucket_region():
    """Presign uses the bucket's home region, not the run region.

    A presigned URL signed for the wrong region gets a PermanentRedirect
    from S3. The bucket region comes from ``_bucket_region``; the run region
    (here us-west-2) must NOT be what the presign client is given when the
    bucket lives elsewhere (here ap-northeast-1).
    """
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"),
        "",  # reset
        "",  # curl 1
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")

    with (
        patch.object(raw_staging, "_list_s3_keys", return_value=["p/file.hdf5"]),
        patch.object(
            raw_staging, "_bucket_region", return_value=("ap-northeast-1", False)
        ),
        patch.object(
            raw_staging, "_presign_s3_key", return_value="https://example.com/x?sig=xyz"
        ) as presign,
    ):
        raw_staging._s3_stage(session, parsed, "name", "prof", "us-west-2")

    # The presign helper is given the bucket's region (ap-northeast-1), not
    # the run region (us-west-2).
    assert presign.call_count == 1
    assert presign.call_args.args[3] == "ap-northeast-1"


def test_s3_stage_fallback_empty_prefix_raises():
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2")
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with patch.object(raw_staging, "_list_s3_keys", return_value=[]):
        with pytest.raises(RawSourceError, match="empty"):
            raw_staging._s3_stage(session, parsed, "name", None, None)


def test_s3_stage_propagates_non_auth_probe_errors():
    """Non-permission probe errors must NOT trigger the fallback path."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("Could not connect to the endpoint URL"),
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with patch.object(raw_staging, "_list_s3_keys") as list_keys:
        with pytest.raises(RawSourceError, match="not a permission issue"):
            raw_staging._s3_stage(session, parsed, "name", None, None)
    list_keys.assert_not_called()


def test_s3_stage_bare_forbidden_no_longer_triggers_fallback():
    """A non-structured error merely containing 'Forbidden' (e.g. a proxy
    body) is NOT an S3 permission signal and must propagate, not route to
    the laptop presign fallback."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("407 Proxy Authentication Required: Forbidden by upstream"),
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with patch.object(raw_staging, "_list_s3_keys") as list_keys:
        with pytest.raises(RawSourceError, match="not a permission issue"):
            raw_staging._s3_stage(session, parsed, "name", None, None)
    list_keys.assert_not_called()


def test_s3_stage_structured_403_still_triggers_fallback():
    """Dropping bare 'Forbidden' must not regress real, structured 403s."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError(
            "An error occurred (403) when calling the ListObjectsV2 operation"
        ),
        "",  # reset
        "",  # curl 1
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with (
        patch.object(raw_staging, "_list_s3_keys", return_value=["p/f.bin"]),
        patch.object(raw_staging, "_bucket_region", return_value=("us-west-2", False)),
        patch.object(
            raw_staging, "_presign_s3_key", return_value="https://example.com/x?sig=y"
        ),
    ):
        raw_staging._s3_stage(session, parsed, "name", None, None)
    curl_cmds = [
        c.args[0] for c in session.run.call_args_list if "curl -fsSL" in c.args[0]
    ]
    assert len(curl_cmds) == 1


def test_s3_stage_does_not_misclassify_bucket_named_with_404():
    """A bucket whose name contains '404' echoed in a non-permission error
    must propagate, not route to the presign fallback."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError(
            "Could not connect to the endpoint URL: "
            "https://my-dataset-404.s3.amazonaws.com/"
        ),
    ]
    parsed = S3Source(
        raw="s3://my-dataset-404/p/", bucket="my-dataset-404", prefix="p/"
    )
    with patch.object(raw_staging, "_list_s3_keys") as list_keys:
        with pytest.raises(RawSourceError, match="not a permission issue"):
            raw_staging._s3_stage(session, parsed, "name", None, None)
    list_keys.assert_not_called()


# ── _list_s3_keys (boto3 paginator) ───────────────────────────────────────


def _paginator_client(pages: list[dict]) -> MagicMock:
    """A mock S3 client whose list_objects_v2 paginator yields ``pages``."""
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    client.get_paginator.return_value = paginator
    return client


def test_list_s3_keys_paginates():
    """Keys from every page are concatenated in order (>1000-object path)."""
    client = _paginator_client(
        [
            {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]},
            {"Contents": [{"Key": "p/c"}]},
        ]
    )
    with patch.object(raw_staging, "s3_client", return_value=client):
        keys = raw_staging._list_s3_keys("b", "p/", None, None)
    assert keys == ["p/a", "p/b", "p/c"]
    client.get_paginator.assert_called_once_with("list_objects_v2")
    paginator = client.get_paginator.return_value
    paginator.paginate.assert_called_once_with(Bucket="b", Prefix="p/")


def test_list_s3_keys_empty_prefix_returns_empty():
    """A prefix with no objects yields a page with no Contents → []."""
    client = _paginator_client([{"KeyCount": 0}])
    with patch.object(raw_staging, "s3_client", return_value=client):
        assert raw_staging._list_s3_keys("b", "empty/", None, None) == []


def test_list_s3_keys_wraps_client_error():
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.side_effect = _s3_client_error("AccessDenied")
    client.get_paginator.return_value = paginator
    with patch.object(raw_staging, "s3_client", return_value=client):
        with pytest.raises(RawSourceError, match="list-objects-v2 failed"):
            raw_staging._list_s3_keys("b", "p/", None, None)


# ── _bucket_region (boto3 head_bucket) ────────────────────────────────────


def test_bucket_region_returns_head_bucket_region():
    """head_bucket's BucketRegion flows through; us-east-1 is explicit now."""
    client = MagicMock()
    client.head_bucket.return_value = {"BucketRegion": "us-east-1"}
    with patch.object(raw_staging, "s3_client", return_value=client):
        assert raw_staging._bucket_region("b", None, None) == ("us-east-1", False)


def test_bucket_region_returns_cross_region():
    """The resolved region (not the run region) is returned for presigning."""
    client = MagicMock()
    client.head_bucket.return_value = {"BucketRegion": "ap-northeast-1"}
    with patch.object(raw_staging, "s3_client", return_value=client):
        region, fell_back = raw_staging._bucket_region("b", None, "us-west-2")
    assert region == "ap-northeast-1"
    assert fell_back is False


def test_bucket_region_falls_back_to_run_region_on_error():
    client = MagicMock()
    client.head_bucket.side_effect = _s3_client_error("AccessDenied")
    with patch.object(raw_staging, "s3_client", return_value=client):
        assert raw_staging._bucket_region("b", None, "eu-west-1") == ("eu-west-1", True)


def test_bucket_region_falls_back_when_region_absent():
    """A response missing BucketRegion is treated as a fallback."""
    client = MagicMock()
    client.head_bucket.return_value = {}
    with patch.object(raw_staging, "s3_client", return_value=client):
        assert raw_staging._bucket_region("b", None, "eu-west-1") == ("eu-west-1", True)


# ── _presign_s3_key (boto3) ───────────────────────────────────────────────


def test_presign_uses_sigv4_virtual_client():
    """The presign client is built for_presign (SigV4 + virtual addressing).

    Locks in the region-correctness fix: a default client emits a region-less
    legacy SigV2 URL, so a future refactor dropping for_presign would silently
    regress cross-region buckets to corrupt (redirect-body) downloads.
    """
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://example.com/x?sig=y"
    with patch.object(raw_staging, "s3_client", return_value=client) as factory:
        url = raw_staging._presign_s3_key("b", "p/key.bin", "prof", "ap-northeast-1")
    assert url == "https://example.com/x?sig=y"
    # The client is built for_presign, bound to the (bucket) region passed in.
    assert factory.call_args.kwargs["for_presign"] is True
    assert factory.call_args.args == ("prof", "ap-northeast-1")
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "b", "Key": "p/key.bin"},
        ExpiresIn=3600,
    )


def test_presign_wraps_client_error():
    client = MagicMock()
    client.generate_presigned_url.side_effect = _s3_client_error("AccessDenied")
    with patch.object(raw_staging, "s3_client", return_value=client):
        with pytest.raises(RawSourceError, match="presign failed"):
            raw_staging._presign_s3_key("b", "p/key.bin", None, None)


# ── region-fallback warning (presign path) ────────────────────────────────


def test_s3_presign_warns_on_region_fallback(capsys):
    """When _bucket_region falls back, warn about redirect-body corruption."""
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"),
        "",  # reset
        "",  # curl 1
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with (
        patch.object(raw_staging, "_list_s3_keys", return_value=["p/f.bin"]),
        patch.object(raw_staging, "_bucket_region", return_value=("us-west-2", True)),
        patch.object(
            raw_staging, "_presign_s3_key", return_value="https://example.com/x?sig=y"
        ),
    ):
        raw_staging._s3_stage(session, parsed, "name", "prof", "us-west-2")
    err = capsys.readouterr().err
    assert "fallback region" in err
    assert "b" in err


def test_s3_presign_no_warning_when_region_resolved(capsys):
    session = MagicMock()
    session.run.side_effect = [
        RuntimeError("An error occurred (AccessDenied) when calling ListObjectsV2"),
        "",  # reset
        "",  # curl 1
    ]
    parsed = S3Source(raw="s3://b/p/", bucket="b", prefix="p/")
    with (
        patch.object(raw_staging, "_list_s3_keys", return_value=["p/f.bin"]),
        patch.object(raw_staging, "_bucket_region", return_value=("us-west-2", False)),
        patch.object(
            raw_staging, "_presign_s3_key", return_value="https://example.com/x?sig=y"
        ),
    ):
        raw_staging._s3_stage(session, parsed, "name", "prof", "us-west-2")
    assert "fallback region" not in capsys.readouterr().err


# ── _hf_stage ─────────────────────────────────────────────────────────────


def test_hf_stage_no_revision_emits_three_commands():
    session = MagicMock()
    session.run.side_effect = ["", "", ""]
    parsed = HFSource(raw="hf://owner/repo", owner="owner", repo="repo")
    raw_staging._hf_stage(session, parsed, "name")
    cmds = [c.args[0] for c in session.run.call_args_list]
    assert len(cmds) == 3
    # 1: ephemeral install via --target.
    assert "rm -rf /tmp/regression-hf-pkgs" in cmds[0]
    assert "pip install --target /tmp/regression-hf-pkgs huggingface_hub" in cmds[0]
    # 2: reset dest.
    assert "rm -rf /fsx/raw/name/" in cmds[1]
    # 3: download via the `hf` console script with PYTHONPATH prepended.
    assert "PYTHONPATH=/tmp/regression-hf-pkgs" in cmds[2]
    assert "/tmp/regression-hf-pkgs/bin/hf download owner/repo" in cmds[2]
    assert "--repo-type dataset" in cmds[2]
    assert "--local-dir /fsx/raw/name/" in cmds[2]
    assert "--revision" not in cmds[2]


def test_hf_stage_with_revision_appends_flag():
    session = MagicMock()
    session.run.side_effect = ["", "", ""]
    parsed = HFSource(
        raw="hf://owner/repo@v1", owner="owner", repo="repo", revision="v1"
    )
    raw_staging._hf_stage(session, parsed, "name")
    cmds = [c.args[0] for c in session.run.call_args_list]
    assert "--revision v1" in cmds[2]


# ── stage_raw dispatcher ──────────────────────────────────────────────────


def test_stage_raw_rejects_invalid_name(tmp_path: Path):
    session = MagicMock()
    src = tmp_path / "raw"
    src.mkdir()
    with pytest.raises(RawSourceError, match="invalid"):
        stage_raw(
            session,
            f"file://{src}",
            "../escape",
            aws_profile=None,
            aws_region=None,
        )
    session.run.assert_not_called()


def test_stage_raw_rejects_name_with_slash(tmp_path: Path):
    session = MagicMock()
    src = tmp_path / "raw"
    src.mkdir()
    with pytest.raises(RawSourceError, match="invalid"):
        stage_raw(
            session,
            f"file://{src}",
            "foo/bar",
            aws_profile=None,
            aws_region=None,
        )


def test_stage_raw_wraps_runtime_error_with_uri(tmp_path: Path):
    """Mid-stage Session failures surface as RawSourceError naming the URI."""
    src = tmp_path / "raw"
    src.mkdir()
    session = MagicMock()
    # Reset succeeds; rsync fails.
    session.rsync.side_effect = RuntimeError("disk full")
    with pytest.raises(RawSourceError) as excinfo:
        stage_raw(
            session,
            f"file://{src}",
            "name-x",
            aws_profile=None,
            aws_region=None,
        )
    msg = str(excinfo.value)
    assert "name-x" in msg
    assert "disk full" in msg
    assert str(src) in msg


def test_stage_raw_propagates_parse_error_unwrapped(tmp_path: Path):
    """Parser errors stay as RawSourceError without the stage-prefix wrapping."""
    session = MagicMock()
    with pytest.raises(RawSourceError, match="unsupported raw source scheme"):
        stage_raw(
            session,
            "gcs://bucket/key",
            "name-x",
            aws_profile=None,
            aws_region=None,
        )
    session.run.assert_not_called()


# ── stage_raw post-stage content check ────────────────────────────────────


def test_stage_raw_content_check_passes_when_dest_nonempty(tmp_path: Path):
    """The trailing `ls -A` reports files → stage_raw returns cleanly."""
    src = tmp_path / "raw"
    src.mkdir()
    session = MagicMock()
    # reset run, rsync (no run), then the trailing `ls -A` returns a file.
    session.run.return_value = "marker.txt"
    stage_raw(session, f"file://{src}", "name-x", aws_profile=None, aws_region=None)
    last = session.run.call_args_list[-1].args[0]
    assert last == "ls -A /fsx/raw/name-x/"


def test_stage_raw_content_check_fails_on_empty_dest(tmp_path: Path):
    """An empty dest listing after staging → RawSourceError."""
    src = tmp_path / "raw"
    src.mkdir()
    session = MagicMock()
    session.run.return_value = ""  # every run incl. the final `ls -A` is empty
    with pytest.raises(RawSourceError, match="produced no files"):
        stage_raw(session, f"file://{src}", "name-x", aws_profile=None, aws_region=None)


def test_stage_raw_content_check_verify_failure_wraps(tmp_path: Path):
    """If the `ls -A` verification itself fails, wrap it as RawSourceError."""
    src = tmp_path / "raw"
    src.mkdir()
    session = MagicMock()

    def run(cmd, *a, **k):
        if cmd.startswith("ls -A"):
            raise RuntimeError("No such file or directory")
        return ""

    session.run.side_effect = run
    with pytest.raises(RawSourceError, match="failed to verify staged content"):
        stage_raw(session, f"file://{src}", "name-x", aws_profile=None, aws_region=None)
