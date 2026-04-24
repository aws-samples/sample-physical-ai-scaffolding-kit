"""Tests for physai clean."""

from unittest.mock import MagicMock

from physai.clean import run_clean


def test_clean_dry_run(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue (no active jobs)
        "/fsx/physai/builds/old-build",  # find builds
        "/fsx/physai/logs/100.out",  # find logs
        "",  # find sync
    ]
    run_clean(session, older_than=0, dry_run=True, force=False, enroot=False)
    out = capsys.readouterr().out
    assert "Would remove" in out
    assert "old-build" in out
    assert "100.out" in out
    # Should not have called rm
    assert not any("rm" in str(c) for c in session.run.call_args_list)


def test_clean_force(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue
        "/fsx/physai/builds/old-build",  # find builds
        "/fsx/physai/logs/100.out",  # find logs
        "",  # find sync
        "",  # rm builds
        "",  # rm logs
    ]
    run_clean(session, older_than=0, dry_run=False, force=True, enroot=False)
    out = capsys.readouterr().out
    assert "Removed 2 items." in out


def test_clean_nothing(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue
        "",  # find builds
        "",  # find logs
        "",  # find sync
    ]
    run_clean(session, older_than=7, dry_run=False, force=False, enroot=False)
    assert "Nothing to clean." in capsys.readouterr().out
