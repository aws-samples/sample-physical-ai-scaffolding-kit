"""Tests for physai jobs."""

from unittest.mock import MagicMock

from physai.jobs import _fmt_time, _parse_job_name, cancel_job, list_jobs


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
