"""Shared parsers for ``physai`` CLI stdout and ``srun --label`` output.

These regexes and helpers are used by more than one check module. Kept in a
leading-underscore, non-``test_`` module so pytest's default collection globs
(``test_*.py`` / ``*_test.py``) skip it — it contributes no test items.
"""

import re

# `physai run`/`eval` prints "Run ID:   run-<...>".
_RUN_ID_RE = re.compile(r"Run ID:\s+(run-\S+)")
# `physai run -n` prints one line per stage: "  <stage>: job <ID>".
_STAGE_JOB_RE = re.compile(r"^\s*(\w+):\s+job\s+(\d+)\s*$", re.MULTILINE)
# Strips the "<task-id>: " prefix that `srun --label` prepends to each line.
_LABEL_RE = re.compile(r"^\s*\d+:\s*")


def _extract_run_id(stdout: str) -> str:
    """Return the ``run-<...>`` id from ``physai run``/``eval`` stdout."""
    m = _RUN_ID_RE.search(stdout)
    if not m:
        raise AssertionError(
            f"Could not find 'Run ID:' line in physai output:\n{stdout}"
        )
    return m.group(1)


def _extract_stage_jobs(stdout: str) -> dict[str, str]:
    """Return ``{stage_name: job_id}`` parsed from ``physai run -n`` output."""
    return {m.group(1): m.group(2) for m in _STAGE_JOB_RE.finditer(stdout)}
