"""Tests for the ``python -m physai_regression`` argparse + dispatch surface.

These don't run pytest for real — they assert that the runner forwards the
right arguments to pytest, drives the orchestration flows in the correct
order for each mode, and rejects unknown modes.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from physai_regression.__main__ import CHECKS_DIR, main


# ── upgrade-existing ──────────────────────────────────────────────────────


def test_upgrade_existing_upgrades_then_runs_checks():
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.__main__.flows.upgrade_in_place",
            side_effect=lambda **_: call_order.append("upgrade"),
        ),
        patch(
            "physai_regression.__main__.pytest.main",
            side_effect=lambda *_a, **_k: call_order.append("pytest") or 0,
        ),
    ):
        rc = main(["upgrade-existing", "--profile", "p", "--region", "r"])
    assert rc == 0
    assert call_order == ["upgrade", "pytest"]


def test_upgrade_existing_returns_pytest_rc_on_check_failure(capsys):
    """Check failure: cluster left in upgraded state; rc is the pytest rc."""
    with (
        patch("physai_regression.__main__.flows.upgrade_in_place"),
        patch("physai_regression.__main__.pytest.main", return_value=5),
    ):
        rc = main(["upgrade-existing", "--profile", "p", "--region", "r"])
    assert rc == 5
    err = capsys.readouterr().err
    assert "upgraded state" in err


def test_upgrade_existing_does_not_destroy():
    """``upgrade-existing`` operates on a running cluster — never destroys."""
    with (
        patch("physai_regression.__main__.flows.upgrade_in_place"),
        patch("physai_regression.__main__.pytest.main", return_value=0),
        patch("physai_regression.__main__.deploy.cdk_destroy") as destroy,
        patch("physai_regression.__main__.flows.redeploy_from_clean") as redeploy,
    ):
        main(["upgrade-existing", "--profile", "p", "--region", "r"])
    destroy.assert_not_called()
    redeploy.assert_not_called()


# ── fresh ─────────────────────────────────────────────────────────────────


def test_fresh_redeploys_then_runs_checks_then_destroys_on_pass():
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.__main__.flows.redeploy_from_clean",
            side_effect=lambda **_: call_order.append("redeploy"),
        ),
        patch(
            "physai_regression.__main__.pytest.main",
            side_effect=lambda *_a, **_k: call_order.append("pytest") or 0,
        ),
        patch(
            "physai_regression.__main__.deploy.cdk_destroy",
            side_effect=lambda **_: call_order.append("destroy"),
        ),
    ):
        rc = main(["fresh", "--profile", "p", "--region", "r"])
    assert rc == 0
    assert call_order == ["redeploy", "pytest", "destroy"]


def test_fresh_skips_destroy_on_pytest_failure(capsys):
    with (
        patch("physai_regression.__main__.flows.redeploy_from_clean"),
        patch("physai_regression.__main__.pytest.main", return_value=1),
        patch("physai_regression.__main__.deploy.cdk_destroy") as destroy,
    ):
        rc = main(["fresh", "--profile", "p", "--region", "r"])
    assert rc == 1
    destroy.assert_not_called()
    err = capsys.readouterr().err
    assert "Cluster left up" in err


@pytest.mark.parametrize(
    "argv_prefix",
    [
        ["fresh"],
        ["upgrade-existing"],
        ["upgrade-from-ref", "--from-ref", "v0.2.0"],
    ],
    ids=["fresh", "upgrade-existing", "upgrade-from-ref"],
)
def test_aws_and_extra_args_forwarded_to_pytest_for_every_mode(argv_prefix):
    """``_pytest_args`` is mode-agnostic: every mode forwards CHECKS_DIR,
    the AWS args, and the user's pytest passthrough in the same order."""
    forwarded = _captured_pytest_args(
        argv_prefix + ["--profile", "p", "--region", "r", "-k", "dcvagent"]
    )
    assert forwarded[0] == str(CHECKS_DIR)
    assert forwarded[1:] == ["--profile", "p", "--region", "r", "-k", "dcvagent"]


def test_fresh_passes_aws_args_to_orchestration():
    with (
        patch("physai_regression.__main__.flows.redeploy_from_clean") as redeploy,
        patch("physai_regression.__main__.deploy.cdk_destroy") as destroy,
        patch("physai_regression.__main__.pytest.main", return_value=0),
    ):
        main(["fresh", "--profile", "p", "--region", "r"])
    redeploy.assert_called_once_with(profile="p", region="r")
    destroy.assert_called_once_with(profile="p", region="r", skip_if_absent=True)


# ── upgrade-from-ref ──────────────────────────────────────────────────────


def _wt_path(name: str = "wt") -> Path:
    return Path(f"/tmp/{name}")


def test_upgrade_from_ref_full_success_deploys_upgrades_checks_destroys():
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.__main__.flows.deploy_from_ref",
            side_effect=lambda *_a, **_k: call_order.append("deploy_ref") or _wt_path(),
        ),
        patch(
            "physai_regression.__main__.flows.upgrade_in_place",
            side_effect=lambda **_: call_order.append("upgrade"),
        ),
        patch(
            "physai_regression.__main__.pytest.main",
            side_effect=lambda *_a, **_k: call_order.append("pytest") or 0,
        ),
        patch(
            "physai_regression.__main__.flows.destroy_and_remove_worktree",
            side_effect=lambda *_a, **_k: call_order.append("destroy"),
        ) as destroy,
    ):
        rc = main(
            [
                "upgrade-from-ref",
                "--from-ref",
                "v0.2.0",
                "--profile",
                "p",
                "--region",
                "r",
            ]
        )
    assert rc == 0
    assert call_order == ["deploy_ref", "upgrade", "pytest", "destroy"]
    # The worktree path from deploy_from_ref must be the one handed to
    # cleanup — otherwise cleanup removes the wrong (or no) worktree.
    assert destroy.call_args.args[0] == _wt_path()
    assert destroy.call_args.kwargs == {"profile": "p", "region": "r"}


def test_upgrade_from_ref_passes_ref_to_deploy_from_ref():
    with (
        patch(
            "physai_regression.__main__.flows.deploy_from_ref",
            return_value=_wt_path(),
        ) as deploy_ref,
        patch("physai_regression.__main__.flows.upgrade_in_place"),
        patch("physai_regression.__main__.pytest.main", return_value=0),
        patch("physai_regression.__main__.flows.destroy_and_remove_worktree"),
    ):
        main(
            [
                "upgrade-from-ref",
                "--from-ref",
                "v0.2.0",
                "--profile",
                "p",
                "--region",
                "r",
            ]
        )
    deploy_ref.assert_called_once_with("v0.2.0", profile="p", region="r")


def test_upgrade_from_ref_check_failure_skips_destroy(capsys):
    """Check failure: cluster + worktree left in place so the user can debug."""
    with (
        patch(
            "physai_regression.__main__.flows.deploy_from_ref",
            return_value=_wt_path("post-fail"),
        ),
        patch("physai_regression.__main__.flows.upgrade_in_place"),
        patch("physai_regression.__main__.pytest.main", return_value=7),
        patch(
            "physai_regression.__main__.flows.destroy_and_remove_worktree"
        ) as destroy,
    ):
        rc = main(["upgrade-from-ref", "--from-ref", "v0.2.0", "--profile", "p"])
    assert rc == 7
    destroy.assert_not_called()
    err = capsys.readouterr().err
    assert "upgraded state" in err
    assert "/tmp/post-fail" in err


def test_upgrade_from_ref_requires_from_ref():
    with pytest.raises(SystemExit) as excinfo:
        main(["upgrade-from-ref", "--profile", "p"])
    assert excinfo.value.code == 2


def test_from_ref_rejected_for_non_upgrade_from_ref_modes():
    with pytest.raises(SystemExit) as excinfo:
        main(["fresh", "--from-ref", "v0.2.0", "--profile", "p"])
    assert excinfo.value.code == 2


# ── argparse rejections ───────────────────────────────────────────────────


def test_unknown_mode_is_rejected_by_argparse():
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-real-mode"])
    # argparse exits 2 on usage errors.
    assert excinfo.value.code == 2


def test_missing_mode_is_rejected_by_argparse():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


# ── unknown-flag rejection / pytest passthrough ───────────────────────────


def test_runner_flag_typo_is_rejected(capsys):
    """A close typo of a runner long flag (--proflie) must error, not reach pytest.

    Note argparse resolves unambiguous abbreviations (``--profil`` →
    ``--profile``), so the typo here is a transposition argparse can't match;
    the close-match guard then catches it before it silently reaches pytest.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["fresh", "--proflie", "p"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--proflie" in err
    assert "--profile" in err  # names the flag it resembles


def test_runner_flag_typo_with_equals_is_rejected(capsys):
    """The `--flag=value` form is caught too (the name is matched pre-`=`)."""
    with pytest.raises(SystemExit) as excinfo:
        main(["upgrade-existing", "--regon=us-west-2"])
    assert excinfo.value.code == 2
    assert "--region" in capsys.readouterr().err


def test_pytest_short_flags_still_forwarded():
    """Legitimate pytest short flags pass through untouched, incl. ---valued -k."""
    forwarded = _captured_pytest_args(
        ["upgrade-existing", "--profile", "p", "-k", "dcvagent", "-x", "-v"]
    )
    assert forwarded[-4:] == ["-k", "dcvagent", "-x", "-v"]


def test_common_pytest_long_flags_forwarded_without_separator():
    """Genuine pytest long flags (unlike runner typos) forward untouched.

    These don't resemble any runner flag, so no `--` separator is needed —
    the guard only rejects close typos of the runner's own options.
    """
    forwarded = _captured_pytest_args(
        ["upgrade-existing", "--profile", "p", "--lf", "--tb=short", "--maxfail=1"]
    )
    for flag in ("--lf", "--tb=short", "--maxfail=1"):
        assert flag in forwarded


def test_short_flag_value_starting_with_dashes_is_forwarded():
    """A `-k` expression whose value begins with `--` is not mistaken for a flag."""
    forwarded = _captured_pytest_args(
        ["upgrade-existing", "--profile", "p", "-k", "--slow-only"]
    )
    assert forwarded[-2:] == ["-k", "--slow-only"]


# ── --builtin-examples flag plumbing ──────────────────────────────────────


def _captured_pytest_args(argv: list[str]) -> list[str]:
    """Run ``main(argv)`` with all orchestration patched out and return the
    argv handed to ``pytest.main``.

    Each mode's flow primitive is patched to a no-op so ``pytest.main`` is
    the only side effect we care about; rc=0 keeps fresh's destroy from
    erroring.
    """
    with (
        patch("physai_regression.__main__.flows.redeploy_from_clean"),
        patch("physai_regression.__main__.flows.upgrade_in_place"),
        patch(
            "physai_regression.__main__.flows.deploy_from_ref",
            return_value=_wt_path(),
        ),
        patch("physai_regression.__main__.flows.destroy_and_remove_worktree"),
        patch("physai_regression.__main__.deploy.cdk_destroy"),
        patch("physai_regression.__main__.pytest.main", return_value=0) as pmain,
    ):
        rc = main(argv)
    assert rc == 0
    pmain.assert_called_once()
    (forwarded,), _ = pmain.call_args
    return forwarded


def test_default_run_forwards_neither_marker_selector_nor_raw_source():
    """Without --builtin-examples the runner adds no ``-m`` override (it
    relies on pytest.ini's default to exclude Layer 2) and no
    ``--raw-source``."""
    forwarded = _captured_pytest_args(
        ["upgrade-existing", "--profile", "p", "--region", "r"]
    )
    assert "-m" not in forwarded
    assert "--raw-source" not in forwarded


@pytest.mark.parametrize(
    "argv",
    [
        [
            "fresh",
            "--builtin-examples",
            "--raw-source",
            "file:///tmp/raw",
            "--profile",
            "p",
        ],
        [
            "upgrade-existing",
            "--builtin-examples",
            "--raw-source",
            "file:///tmp/raw",
            "--profile",
            "p",
        ],
        [
            "upgrade-from-ref",
            "--builtin-examples",
            "--raw-source",
            "file:///tmp/raw",
            "--from-ref",
            "v0.2.0",
            "--profile",
            "p",
        ],
    ],
    ids=["fresh", "upgrade-existing", "upgrade-from-ref"],
)
def test_builtin_examples_works_for_every_mode(argv: list[str]):
    """The flag is mode-orthogonal: every mode's checks invocation gets it."""
    forwarded = _captured_pytest_args(argv)
    i = forwarded.index("-m")
    assert forwarded[i + 1] == "platform or builtin_example"


def test_builtin_examples_appears_before_extra_pytest_args():
    """``-m`` selector goes ahead of the user's pytest passthrough so a
    user-supplied ``-k`` etc. composes with — not overrides — the marker
    filter."""
    forwarded = _captured_pytest_args(
        [
            "upgrade-existing",
            "--builtin-examples",
            "--raw-source",
            "file:///tmp/raw",
            "--profile",
            "p",
            "-k",
            "liftcube",
        ]
    )
    i_m = forwarded.index("-m")
    i_k = forwarded.index("-k")
    assert i_m < i_k


# ── --raw-source flag plumbing ────────────────────────────────────────────


def test_raw_source_without_builtin_examples_is_rejected(capsys):
    """``--raw-source`` is meaningful only when Layer 2 is selected."""
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "upgrade-existing",
                "--raw-source",
                "file:///tmp/raw",
                "--profile",
                "p",
            ]
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--raw-source is only valid with --builtin-examples" in err


def test_builtin_examples_without_raw_source_is_rejected(capsys):
    """Layer 2 needs an explicit fixture URI; reject the flag-without-pair case."""
    with pytest.raises(SystemExit) as excinfo:
        main(["upgrade-existing", "--builtin-examples", "--profile", "p"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--builtin-examples requires --raw-source" in err


def test_malformed_raw_source_uri_is_rejected_before_deploy(capsys):
    """A bad URI must fail argparse-time, before any cluster operation."""
    with (
        patch("physai_regression.__main__.flows.redeploy_from_clean") as redeploy,
        patch("physai_regression.__main__.pytest.main") as pmain,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "fresh",
                    "--builtin-examples",
                    "--raw-source",
                    "s4://typo-scheme",
                    "--profile",
                    "p",
                ]
            )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unsupported raw source scheme" in err
    # The deploy and the checks must never have started.
    redeploy.assert_not_called()
    pmain.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        [
            "fresh",
            "--builtin-examples",
            "--raw-source",
            "hf://owner/repo",
            "--profile",
            "p",
        ],
        [
            "upgrade-existing",
            "--builtin-examples",
            "--raw-source",
            "hf://owner/repo",
            "--profile",
            "p",
        ],
        [
            "upgrade-from-ref",
            "--builtin-examples",
            "--raw-source",
            "hf://owner/repo",
            "--from-ref",
            "v0.2.0",
            "--profile",
            "p",
        ],
    ],
    ids=["fresh", "upgrade-existing", "upgrade-from-ref"],
)
def test_raw_source_works_for_every_mode(argv: list[str]):
    forwarded = _captured_pytest_args(argv)
    i = forwarded.index("--raw-source")
    assert forwarded[i + 1] == "hf://owner/repo"


def test_raw_source_appears_before_extra_pytest_args():
    """The forwarded ``--raw-source`` precedes user passthrough so an
    explicit ``-k`` doesn't accidentally insert itself between flag and value."""
    forwarded = _captured_pytest_args(
        [
            "upgrade-existing",
            "--builtin-examples",
            "--raw-source",
            "file:///tmp/raw",
            "--profile",
            "p",
            "-k",
            "liftcube",
        ]
    )
    assert forwarded.index("--raw-source") < forwarded.index("-k")
