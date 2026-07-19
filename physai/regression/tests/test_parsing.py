"""Unit tests for the shared check-output parsers.

These interpret ``physai`` CLI stdout and ``srun --label`` output; no AWS or
SSH boundary is touched.
"""

import pytest

from physai_regression.checks._parsing import (
    _LABEL_RE,
    _extract_run_id,
    _extract_stage_jobs,
)


def test_extract_run_id_parses_line():
    assert _extract_run_id("...\nRun ID:   run-abc123\nmore") == "run-abc123"


def test_extract_run_id_raises_on_missing():
    with pytest.raises(AssertionError, match="Run ID:"):
        _extract_run_id("no run id here")


def test_extract_stage_jobs_parses_lines():
    stdout = "Run ID: run-x\n  train: job 42\n  eval: job 43\n"
    assert _extract_stage_jobs(stdout) == {"train": "42", "eval": "43"}


def test_extract_stage_jobs_empty_when_no_lines():
    assert _extract_stage_jobs("nothing to parse") == {}


def test_label_re_strips_srun_task_prefix():
    assert _LABEL_RE.sub("", "0: ip-10-0-1-1:OK") == "ip-10-0-1-1:OK"
    # No prefix → unchanged.
    assert _LABEL_RE.sub("", "ip-10-0-1-1:OK") == "ip-10-0-1-1:OK"
