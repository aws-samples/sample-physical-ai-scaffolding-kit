"""DCV-related platform checks.

See ``docs/en/PIPELINE_DESIGN.md`` §5 for the visual-eval architecture.
"""

import pytest

from physai_regression.checks._parsing import _LABEL_RE


@pytest.mark.platform
def test_dcvagent_process_running(physai_session) -> None:
    """Assert every GPU node has a ``dcvagent --session-id console`` process.

    DCV state can differ per node, so the check fans out across the entire
    GPU partition (one task per node) rather than allocating a single node
    and trusting it to be representative.
    """
    # `-t idle,mix,alloc` skips DOWN/DRAINED nodes, which can't satisfy
    # an allocation and would block srun.
    nodelist = physai_session.run(
        "sinfo -h -p gpu -N -t idle,mix,alloc -o '%N' | sort -u | paste -sd,"
    ).strip()
    assert nodelist, "No schedulable GPU nodes in `gpu` partition."
    node_count = len(nodelist.split(","))

    # `--immediate=30` makes srun fail-fast if any node in the list is no
    # longer schedulable by the time the allocator runs (the gap between
    # sinfo and srun is small but non-zero). Slurm allocates atomically:
    # one unavailable node fails the whole request rather than silently
    # dropping that node from the run.
    remote = (
        f"srun --partition gpu --nodelist={nodelist} --ntasks-per-node=1 "
        f"--immediate=30 --label "
        "bash -c '"
        "h=$(hostname); "
        'if pgrep -f "dcvagent.*--session-id console" >/dev/null; '
        'then echo "$h:OK"; else echo "$h:MISSING"; fi'
        "'"
    )
    output = physai_session.run(remote)

    statuses: dict[str, str] = {}
    for line in output.splitlines():
        line = _LABEL_RE.sub("", line).strip()
        if not line or ":" not in line:
            continue
        host, _, status = line.partition(":")
        statuses[host] = status

    missing = sorted(h for h, st in statuses.items() if st != "OK")
    reported = len(statuses)
    assert reported == node_count, (
        f"Expected {node_count} GPU node reports, got {reported}. Raw output:\n{output}"
    )
    assert not missing, (
        f"dcvagent --session-id console is not running on {len(missing)} "
        f"of {node_count} GPU node(s): {', '.join(missing)}"
    )
