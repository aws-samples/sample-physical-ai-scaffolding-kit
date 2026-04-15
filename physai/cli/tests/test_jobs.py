"""Tests for physai jobs."""

from unittest.mock import MagicMock

from physai.jobs import _parse_job_name, cancel_job, list_jobs


def test_parse_job_name():
    assert _parse_job_name("physai/build/leisaac-runtime") == (
        "build",
        "leisaac-runtime",
    )
    assert _parse_job_name("physai/train/so101") == ("train", "so101")
    assert _parse_job_name("other-job") == ("?", "other-job")


def test_list_jobs_empty(capsys):
    session = MagicMock()
    session.run.return_value = ""
    session.has_sacct = False
    list_jobs(session)
    assert "No physai jobs found." in capsys.readouterr().out


def test_list_jobs_with_active(capsys):
    session = MagicMock()
    session.run.return_value = '"123|physai/build/test|RUNNING|5:00|base=foo"'
    session.has_sacct = False
    list_jobs(session)
    out = capsys.readouterr().out
    assert "123" in out
    assert "build" in out
    assert "RUNNING" in out


def test_cancel_job(capsys):
    session = MagicMock()
    session.run.return_value = ""
    cancel_job(session, "123")
    session.run.assert_called_once_with("scancel 123")
    assert "Cancelled job 123" in capsys.readouterr().out
