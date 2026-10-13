"""Tests for physai data commands (ls, upload, rm)."""

from unittest.mock import MagicMock, patch

import pytest

from physai.data import ls, rm, save, upload


def test_ls_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        ls(MagicMock(), "enroot")


def test_ls_empty(capsys):
    session = MagicMock()
    session.run.return_value = ""
    ls(session, "datasets")
    assert "(empty)" in capsys.readouterr().out


def test_ls_with_path(capsys):
    session = MagicMock()
    session.run.return_value = "4.0K\tfoo\n1.2G\tbar.hdf5"
    ls(session, "raw", "subdir")
    out = capsys.readouterr().out
    assert "foo" in out
    assert "bar.hdf5" in out
    assert "1.2G" in out
    # Sanity: command uses du -sh, not ls -l
    cmd = session.run.call_args[0][0]
    assert "du -sh" in cmd
    assert "/fsx/raw/subdir" in cmd


def test_upload_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        upload(MagicMock(), "enroot", "/tmp")


def test_upload_missing_path(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        upload(MagicMock(), "datasets", str(tmp_path / "nope"))


def test_upload_dataset_rsyncs(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    session = MagicMock()
    upload(session, "datasets", str(d))
    session.rsync.assert_called_once_with(
        str(d.resolve()), "/fsx/datasets/", show_progress=True
    )


def test_upload_raw_prompts_and_aborts(tmp_path, capsys):
    f = tmp_path / "x.hdf5"
    f.write_text("x")
    session = MagicMock()
    with patch("builtins.input", return_value="n"):
        upload(session, "raw", str(f))
    assert "Aborted." in capsys.readouterr().out
    session.rsync.assert_not_called()


def test_upload_raw_prompts_and_proceeds(tmp_path):
    f = tmp_path / "x.hdf5"
    f.write_text("x")
    session = MagicMock()
    with patch("builtins.input", return_value="y"):
        upload(session, "raw", str(f))
    session.rsync.assert_called_once_with(
        str(f.resolve()), "/fsx/raw/", show_progress=True
    )


# ── rm ──


def _mock_session_with_kind(kind: str, size: str = "1.2G", active: str | None = None):
    """Build a Session mock that answers kind-probe, size-probe, and rm calls."""
    session = MagicMock()

    def run_side_effect(cmd: str) -> str:
        if "-d " in cmd and "-f " in cmd:  # kind probe
            return kind
        if "du -sh" in cmd:
            return size + "\t/fsx/x"  # `cut -f1` only keeps the first field
        if cmd.startswith("squeue"):  # active-job probe
            if active:
                return f"{active}|physai/run/x/convert|produces=/fsx/datasets/foo/"
            return ""
        if cmd.startswith("rm -rf"):
            return ""
        raise AssertionError(f"unexpected session.run call: {cmd!r}")

    session.run.side_effect = run_side_effect
    return session


def test_rm_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        rm(MagicMock(), "enroot", "foo")


def test_rm_rejects_slash_in_name():
    with pytest.raises(SystemExit, match="Invalid datasets name"):
        rm(MagicMock(), "datasets", "a/b")


def test_rm_rejects_dot_name():
    with pytest.raises(SystemExit, match="Invalid datasets name"):
        rm(MagicMock(), "datasets", "..")


def test_rm_missing_artifact():
    session = _mock_session_with_kind(kind="none")
    with pytest.raises(SystemExit, match="Nothing to remove"):
        rm(session, "datasets", "foo", force=True)


def test_rm_blocked_by_active_job():
    session = _mock_session_with_kind(kind="dir", active="123")
    with pytest.raises(SystemExit, match="Active job 123"):
        rm(session, "datasets", "foo", force=True)


def test_rm_prompts_and_aborts(capsys):
    session = _mock_session_with_kind(kind="dir")
    with patch("builtins.input", return_value="n"):
        rm(session, "datasets", "foo")
    out = capsys.readouterr().out
    assert "About to remove directory" in out
    assert "Aborted." in out
    # No rm -rf was issued
    for call in session.run.call_args_list:
        assert not call.args[0].startswith("rm -rf")


def test_rm_force_skips_prompt():
    session = _mock_session_with_kind(kind="dir")
    rm(session, "datasets", "foo", force=True)
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert len(rm_calls) == 1
    assert "/fsx/datasets/foo" in rm_calls[0].args[0]


def test_rm_raw_shows_dra_warning(capsys):
    session = _mock_session_with_kind(kind="dir")
    with patch("builtins.input", return_value="n"):
        rm(session, "raw", "foo")
    assert "DRA cache" in capsys.readouterr().out


def test_rm_evaluations_category():
    """evaluations is in RM_CATEGORIES even though it's not in upload's CATEGORIES."""
    session = _mock_session_with_kind(kind="dir")
    rm(session, "evaluations", "run-2026", force=True)
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert len(rm_calls) == 1
    assert "/fsx/evaluations/run-2026" in rm_calls[0].args[0]


def _mock_session_rm_permission_denied(category: str):
    session = MagicMock()

    def run_side_effect(cmd: str) -> str:
        if "-d " in cmd and "-f " in cmd:
            return "dir"
        if "du -sh" in cmd:
            return "1.2G\t/fsx/x"
        if cmd.startswith("squeue"):
            return ""
        if cmd.startswith("rm -rf"):
            raise RuntimeError(
                f"ssh host: rm: cannot remove '/fsx/{category}/foo/data.bin': "
                "Permission denied"
            )
        raise AssertionError(f"unexpected session.run call: {cmd!r}")

    session.run.side_effect = run_side_effect
    return session


def test_rm_permission_denied_prints_sudo_hint():
    session = _mock_session_rm_permission_denied("datasets")
    with pytest.raises(SystemExit) as excinfo:
        rm(session, "datasets", "foo", force=True)
    msg = str(excinfo.value)
    assert "Permission denied" in msg
    assert "sudo rm -rf /fsx/datasets/foo" in msg
    # Don't suggest deleting from S3 — that's the durable source of truth,
    # not what the user wants when removing a local FSx artifact.
    assert "aws s3" not in msg


def test_rm_raw_permission_denied_does_not_suggest_s3_rm():
    session = _mock_session_rm_permission_denied("raw")
    with pytest.raises(SystemExit) as excinfo:
        rm(session, "raw", "demo-set", force=True)
    msg = str(excinfo.value)
    assert "Permission denied" in msg
    assert "sudo rm -rf /fsx/raw/demo-set" in msg
    assert "aws s3" not in msg


# ── save ──


def _mock_session_for_save(
    kind: str = "dir",
    size: str = "12G",
    active: str | None = None,
    active_save: str | None = None,
    existing_listing: str = "",
    submitted_job_id: str = "9999",
):
    """Build a Session mock that answers every probe `save` performs.

    Handles both inline and --job paths (mkdir / write_file / sbatch submit).
    """
    session = MagicMock()

    def run_side_effect(cmd: str) -> str:
        if "-d " in cmd and "-f " in cmd:  # kind probe
            return kind
        if cmd.startswith('squeue') and 'physai/save/' in cmd:
            return active_save or ""
        if cmd.startswith("squeue"):
            if active:
                return f"{active}|physai/run/x/train|produces=/fsx/checkpoints/foo/"
            return ""
        if "du -sh" in cmd:
            return size + "\t/fsx/x"
        if cmd.startswith("aws s3 ls"):
            return existing_listing
        if cmd.startswith("mkdir -p /fsx/physai/"):
            return ""
        if cmd.startswith("sbatch --parsable "):
            return submitted_job_id
        if cmd.startswith("rm -rf "):
            return ""
        raise AssertionError(f"unexpected session.run call: {cmd!r}")

    session.run.side_effect = run_side_effect
    return session


def test_save_rejects_unsupported_category():
    with pytest.raises(SystemExit, match="Cannot save category"):
        save(MagicMock(), "datasets", "foo", data_bucket="b", force=True)


def test_save_rejects_slash_in_name():
    with pytest.raises(SystemExit, match="Invalid checkpoints name"):
        save(MagicMock(), "checkpoints", "a/b", data_bucket="b", force=True)


def test_save_requires_data_bucket():
    session = _mock_session_for_save()
    with pytest.raises(SystemExit, match="data_bucket"):
        save(session, "checkpoints", "foo", data_bucket=None, force=True)


def test_save_missing_artifact():
    session = _mock_session_for_save(kind="none")
    with pytest.raises(SystemExit, match="Nothing to save"):
        save(session, "checkpoints", "foo", data_bucket="b", force=True)


def test_save_rejects_file_kind():
    session = _mock_session_for_save(kind="file")
    with pytest.raises(SystemExit, match="not a directory"):
        save(session, "checkpoints", "foo", data_bucket="b", force=True)


def test_save_blocked_by_active_job():
    session = _mock_session_for_save(active="99")
    with pytest.raises(SystemExit, match="Active job 99"):
        save(session, "checkpoints", "foo", data_bucket="b", force=True)


def test_save_force_skips_prompt_and_streams(capsys):
    session = _mock_session_for_save()
    save(session, "checkpoints", "foo", data_bucket="my-bucket", force=True)
    session.run_streaming.assert_called_once()
    cmd = session.run_streaming.call_args[0][0]
    assert cmd == "aws s3 cp --recursive /fsx/checkpoints/foo/ s3://my-bucket/checkpoints/foo/"
    assert "Saved" in capsys.readouterr().out


def test_save_dry_run_passes_dryrun_flag():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        dry_run=True,
    )
    cmd = session.run_streaming.call_args[0][0]
    assert cmd.endswith(" --dryrun")


def test_save_prompts_and_aborts(capsys):
    session = _mock_session_for_save()
    with patch("builtins.input", return_value="n"):
        save(session, "checkpoints", "foo", data_bucket="my-bucket")
    assert "Aborted." in capsys.readouterr().out
    session.run_streaming.assert_not_called()


def test_save_prompt_shows_existing_objects(capsys):
    listing = "Total Objects: 3\n   Total Size: 1234"
    session = _mock_session_for_save(existing_listing=listing)
    with patch("builtins.input", return_value="n"):
        save(session, "checkpoints", "foo", data_bucket="my-bucket")
    out = capsys.readouterr().out
    assert "already contains objects" in out
    assert "Total Objects: 3" in out


def test_save_surfaces_transfer_failure():
    session = _mock_session_for_save()
    session.run_streaming.side_effect = RuntimeError("exit 1: AccessDenied")
    with pytest.raises(SystemExit, match="aws s3 cp failed"):
        save(session, "checkpoints", "foo", data_bucket="my-bucket", force=True)


def test_save_blocked_by_active_save_job():
    session = _mock_session_for_save(active_save="7777")
    with pytest.raises(SystemExit, match="Save job 7777"):
        save(session, "checkpoints", "foo", data_bucket="b", force=True)


def test_save_submit_job_writes_sbatch_and_submits():
    session = _mock_session_for_save(submitted_job_id="4242")
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        submit_job=True,
    )

    # sbatch content: correct paths, cpu partition, aws s3 cp command
    write_calls = session.write_file.call_args_list
    assert len(write_calls) == 1
    sbatch_path, content = write_calls[0].args
    assert sbatch_path == "/fsx/physai/jobs/save-checkpoints-foo.sbatch"
    assert "#SBATCH --job-name=physai/save/checkpoints/foo" in content
    assert "#SBATCH --partition=cpu" in content
    assert "aws s3 cp --recursive /fsx/checkpoints/foo/ s3://my-bucket/checkpoints/foo/" in content
    assert " --dryrun" not in content

    # sbatch --parsable was invoked
    sbatch_calls = [
        c for c in session.run.call_args_list if c.args[0].startswith("sbatch --parsable")
    ]
    assert len(sbatch_calls) == 1
    assert sbatch_path in sbatch_calls[0].args[0]

    # Streamed by default
    session.stream_log.assert_called_once_with("4242")
    # Did NOT run aws s3 cp inline
    session.run_streaming.assert_not_called()


def test_save_submit_job_no_stream():
    session = _mock_session_for_save(submitted_job_id="4242")
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        submit_job=True,
        stream=False,
    )
    session.stream_log.assert_not_called()


def test_save_submit_job_dry_run_in_sbatch():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        submit_job=True,
        dry_run=True,
        stream=False,
    )
    _, content = session.write_file.call_args.args
    assert "aws s3 cp --recursive --dryrun /fsx/checkpoints/foo/ s3://my-bucket/checkpoints/foo/" in content


def test_save_delete_inline_removes_source_after_copy():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        delete=True,
    )
    # aws s3 cp ran, then rm -rf on the source
    session.run_streaming.assert_called_once()
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert len(rm_calls) == 1
    assert rm_calls[0].args[0] == "rm -rf /fsx/checkpoints/foo"


def test_save_delete_inline_skipped_when_dry_run():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        dry_run=True,
        delete=True,
    )
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert rm_calls == []


def test_save_delete_inline_skipped_when_copy_fails():
    session = _mock_session_for_save()
    session.run_streaming.side_effect = RuntimeError("exit 1: AccessDenied")
    with pytest.raises(SystemExit, match="aws s3 cp failed"):
        save(
            session,
            "checkpoints",
            "foo",
            data_bucket="my-bucket",
            force=True,
            delete=True,
        )
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert rm_calls == []


def test_save_delete_inline_permission_denied_hint():
    session = _mock_session_for_save()

    def rm_denied(cmd: str) -> str:
        # Chain to the shared side_effect for probes, but fail rm -rf.
        if cmd.startswith("rm -rf"):
            raise RuntimeError("ssh host: rm: Permission denied")
        return session.run.side_effect(cmd)

    original = session.run.side_effect
    session.run.side_effect = (
        lambda cmd: rm_denied(cmd) if cmd.startswith("rm -rf") else original(cmd)
    )
    with pytest.raises(SystemExit) as excinfo:
        save(
            session,
            "checkpoints",
            "foo",
            data_bucket="my-bucket",
            force=True,
            delete=True,
        )
    msg = str(excinfo.value)
    assert "Permission denied" in msg
    assert "safe in S3" in msg
    assert "sudo rm -rf /fsx/checkpoints/foo" in msg


def test_save_delete_in_sbatch_appends_rm():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        submit_job=True,
        stream=False,
        delete=True,
    )
    _, content = session.write_file.call_args.args
    assert "aws s3 cp --recursive /fsx/checkpoints/foo/ s3://my-bucket/checkpoints/foo/" in content
    assert "rm -rf /fsx/checkpoints/foo" in content
    # rm sits AFTER the copy line (relies on `set -eo pipefail`)
    assert content.index("aws s3 cp") < content.index("rm -rf")


def test_save_delete_in_sbatch_suppressed_by_dry_run():
    session = _mock_session_for_save()
    save(
        session,
        "checkpoints",
        "foo",
        data_bucket="my-bucket",
        force=True,
        submit_job=True,
        stream=False,
        delete=True,
        dry_run=True,
    )
    _, content = session.write_file.call_args.args
    assert "rm -rf" not in content


def test_rm_propagates_non_permission_runtime_errors():
    session = MagicMock()

    def run_side_effect(cmd: str) -> str:
        if "-d " in cmd and "-f " in cmd:
            return "dir"
        if "du -sh" in cmd:
            return "1.2G\t/fsx/x"
        if cmd.startswith("squeue"):
            return ""
        if cmd.startswith("rm -rf"):
            raise RuntimeError("ssh host: connection lost")
        raise AssertionError(f"unexpected session.run call: {cmd!r}")

    session.run.side_effect = run_side_effect
    with pytest.raises(RuntimeError, match="connection lost"):
        rm(session, "datasets", "foo", force=True)
