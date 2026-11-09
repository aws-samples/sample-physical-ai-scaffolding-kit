"""Tests for physai jobs."""

from unittest.mock import MagicMock

import pytest

from physai.jobs import (
    _fmt_time,
    _parse_job_name,
    cancel_job,
    list_jobs,
    logs_job,
    status_job,
)


def test_fmt_time():
    assert _fmt_time("2026-04-22T10:00:00") == "04-22 10:00:00"
    assert _fmt_time("Unknown") == "Unknown"
    assert _fmt_time("N/A") == "N/A"
    assert _fmt_time("") == ""


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
    session.run.return_value = '"123|physai/build/test|RUNNING|2026-04-22T10:00:00|2026-04-22T10:00:05|5:00|base=foo"'
    session.has_sacct = False
    list_jobs(session)
    out = capsys.readouterr().out
    assert "123" in out
    assert "build" in out
    assert "RUNNING" in out
    assert "04-22 10:00:00" in out  # submit/start time formatted


def test_list_jobs_sorted_most_recent_first(capsys):
    """Rows should print in descending job-id order (most recent first)."""
    session = MagicMock()
    # Unique names per row so we can find their positions in the output.
    session.run.return_value = (
        '"5|physai/build/name_a|COMPLETED|2026-04-22T10:00:00|2026-04-22T10:00:05|1:00|"\n'
        '"20|physai/build/name_b|RUNNING|2026-04-22T12:00:00|2026-04-22T12:00:05|1:00|"\n'
        '"9|physai/build/name_c|COMPLETED|2026-04-22T11:00:00|2026-04-22T11:00:05|1:00|"'
    )
    session.has_sacct = False
    list_jobs(session)
    out = capsys.readouterr().out
    assert out.index("name_b") < out.index("name_c") < out.index("name_a")


def test_cancel_job(capsys):
    session = MagicMock()
    session.run.return_value = ""
    cancel_job(session, "123")
    session.run.assert_called_once_with("scancel 123")
    assert "Cancelled job 123" in capsys.readouterr().out


@pytest.mark.parametrize(
    "job_id", ["123; rm -rf ~", "$(id)", "12 34", "", "abc", "1_2_3"]
)
def test_cancel_job_rejects_invalid_job_id(job_id):
    session = MagicMock()
    with pytest.raises(SystemExit, match="Invalid job ID"):
        cancel_job(session, job_id)
    session.run.assert_not_called()


def test_status_job_rejects_invalid_job_id():
    session = MagicMock()
    with pytest.raises(SystemExit, match="Invalid job ID"):
        status_job(session, "1|whoami")
    session.run.assert_not_called()


def test_logs_job_rejects_invalid_job_id():
    session = MagicMock()
    with pytest.raises(SystemExit, match="Invalid job ID"):
        logs_job(session, "1`id`")
    session.stream_log.assert_not_called()


def test_cancel_job_accepts_array_job_id():
    session = MagicMock()
    session.run.return_value = ""
    cancel_job(session, "123_4")
    session.run.assert_called_once_with("scancel 123_4")
