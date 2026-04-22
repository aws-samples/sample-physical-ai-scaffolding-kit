"""Tests for `physai doctor`."""

from unittest.mock import MagicMock

import pytest

from physai import doctor

# ── check_fsx_dirs ──


def _stat_out(*rows: tuple[str, str, str]) -> str:
    """Build fake `stat -c '%a %F %n'` output, one line per row."""
    return "\n".join(f"{mode} {ftype} {path}" for mode, ftype, path in rows)


def _all_good_stat() -> str:
    """Build stat output with each dir at its expected mode."""
    return _stat_out(
        *[(mode, "directory", f"/fsx/{d}") for d, mode in doctor.FSX_DIRS.items()]
    )


def test_check_fsx_dirs_all_good():
    session = MagicMock()
    session.run.return_value = _all_good_stat()
    assert doctor.check_fsx_dirs(session).status == "PASS"


def test_check_fsx_dirs_enroot_must_be_sticky():
    """/fsx/enroot must be 1777 (not plain 777) per the enroot setup."""
    session = MagicMock()
    rows = [(mode, "directory", f"/fsx/{d}") for d, mode in doctor.FSX_DIRS.items()]
    idx = list(doctor.FSX_DIRS).index("enroot")
    rows[idx] = ("777", "directory", "/fsx/enroot")
    session.run.return_value = _stat_out(*rows)
    result = doctor.check_fsx_dirs(session)
    assert result.status == "FAIL"
    assert "/fsx/enroot" in result.message
    assert "expected 1777" in result.message


def test_check_fsx_dirs_missing():
    session = MagicMock()
    first_key = next(iter(doctor.FSX_DIRS))
    lines = [
        f"stat: cannot statx '/fsx/{first_key}': No such file or directory",
        *[
            f"{mode} directory /fsx/{d}"
            for d, mode in list(doctor.FSX_DIRS.items())[1:]
        ],
    ]
    session.run.return_value = "\n".join(lines)
    result = doctor.check_fsx_dirs(session)
    assert result.status == "FAIL"
    assert "missing" in result.message


def test_check_fsx_dirs_wrong_mode():
    session = MagicMock()
    rows = [(mode, "directory", f"/fsx/{d}") for d, mode in doctor.FSX_DIRS.items()]
    # Break the first non-enroot entry: raw expected 777 → provide 755.
    rows[0] = ("755", "directory", f"/fsx/{next(iter(doctor.FSX_DIRS))}")
    session.run.return_value = _stat_out(*rows)
    result = doctor.check_fsx_dirs(session)
    assert result.status == "FAIL"
    assert "mode is 755" in result.message


def test_fix_fsx_dirs_issues_commands():
    session = MagicMock()
    doctor.fix_fsx_dirs(session)
    cmd = session.run.call_args[0][0]
    for d, mode in doctor.FSX_DIRS.items():
        assert f"mkdir -p /fsx/{d}" in cmd
        assert f"chmod 0{mode} /fsx/{d}" in cmd


# ── check_slurm_conf_drift ──


def _hash_block(files: list[str], hashes: list[str]) -> str:
    return "\n".join(
        f"{h}  /var/spool/slurmd/conf-cache/{f}" for f, h in zip(files, hashes)
    )


def _matching_hashes() -> list[str]:
    return [f"hash_{i}" for i in range(len(doctor.CONF_CACHE_FILES))]


def test_check_slurm_drift_all_match():
    session = MagicMock()
    hashes = _matching_hashes()
    block = _hash_block(doctor.CONF_CACHE_FILES, hashes)

    def fake_run(cmd: str) -> str:
        if cmd.startswith("sinfo"):
            return "n1\nn2\nn1\n"  # duplicates → dedup
        if "srun" in cmd:
            return block
        raise AssertionError(f"unexpected cmd: {cmd}")

    session.run.side_effect = fake_run
    assert doctor.check_slurm_conf_drift(session).status == "PASS"


def test_check_slurm_drift_detected():
    session = MagicMock()
    good = _matching_hashes()
    bad = list(good)
    bad[0] = "DIFFERENT"

    def fake_run(cmd: str) -> str:
        if cmd.startswith("sinfo"):
            return "n1\nn2\nn3\n"
        if "srun -N1 -w n3" in cmd:
            return _hash_block(doctor.CONF_CACHE_FILES, bad)
        if "srun" in cmd:
            return _hash_block(doctor.CONF_CACHE_FILES, good)
        raise AssertionError(cmd)

    session.run.side_effect = fake_run
    result = doctor.check_slurm_conf_drift(session)
    assert result.status == "FAIL"
    assert "n3" in result.message


def test_check_slurm_drift_unreachable_all():
    session = MagicMock()

    def fake_run(cmd: str) -> str:
        if cmd.startswith("sinfo"):
            return "n1\n"
        raise RuntimeError("srun failed")

    session.run.side_effect = fake_run
    result = doctor.check_slurm_conf_drift(session)
    assert result.status == "WARN"


def test_check_slurm_drift_some_unreachable_rest_match():
    session = MagicMock()
    good = _matching_hashes()

    def fake_run(cmd: str) -> str:
        if cmd.startswith("sinfo"):
            return "n1\nn2\n"
        if "n2" in cmd:
            raise RuntimeError("srun timed out")
        return _hash_block(doctor.CONF_CACHE_FILES, good)

    session.run.side_effect = fake_run
    result = doctor.check_slurm_conf_drift(session)
    assert result.status == "WARN"
    assert "n2" in result.message


def test_fix_slurm_reconfigure():
    session = MagicMock()
    doctor.fix_slurm_reconfigure(session)
    session.run.assert_called_once_with("scontrol reconfigure")


# ── check_slurmdbd ──


def test_check_slurmdbd_ok():
    session = MagicMock()
    session.run.return_value = ""
    assert doctor.check_slurmdbd(session).status == "PASS"


def test_check_slurmdbd_fail():
    session = MagicMock()
    session.run.side_effect = RuntimeError("Slurm accounting storage is disabled")
    result = doctor.check_slurmdbd(session)
    assert result.status == "FAIL"
    assert "accounting storage" in result.message
    assert "AWS console" in result.message


# ── run_doctor ──


def _pass_check(name: str) -> doctor.Check:
    return doctor.Check(name, lambda s: doctor.CheckResult("PASS"))


def _fail_check(name: str, fix=None) -> doctor.Check:
    return doctor.Check(name, lambda s: doctor.CheckResult("FAIL", "oops"), fix)


def test_run_doctor_all_pass(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [_pass_check("a"), _pass_check("b")])
    # All-pass: returns cleanly.
    doctor.run_doctor(MagicMock())


def test_run_doctor_fail_exits_1(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [_fail_check("bad")])
    monkeypatch.setattr(doctor, "_prompt_yes_no", lambda q: False)
    with pytest.raises(SystemExit) as exc:
        doctor.run_doctor(MagicMock())
    assert exc.value.code == 1


def test_run_doctor_applies_fix_on_yes(monkeypatch):
    fix_calls: list[int] = []
    runs: list[str] = []

    def check_run(session):
        runs.append("run")
        # First call fails, second (after fix) passes.
        if len(fix_calls) == 0:
            return doctor.CheckResult("FAIL", "broken")
        return doctor.CheckResult("PASS")

    def fix(session):
        fix_calls.append(1)

    monkeypatch.setattr(doctor, "CHECKS", [doctor.Check("fixable", check_run, fix)])
    monkeypatch.setattr(doctor, "_prompt_yes_no", lambda q: True)
    # After fix, check passes → run_doctor returns cleanly.
    doctor.run_doctor(MagicMock())
    assert fix_calls == [1]
    assert len(runs) == 2
