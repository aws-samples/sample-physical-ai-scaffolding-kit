"""Tests for ``physai_regression.orchestration``.

The orchestration primitives wrap ``subprocess.run`` calls to the AWS CLI
and ``npx cdk``. These tests mock that boundary so the wiring is checked
without invoking AWS or CDK.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from physai_regression.orchestration import deploy, flows


def _client_error(code: str, message: str) -> ClientError:
    """Build a botocore ClientError with the given error Code/Message."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeStacks")


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _flag_value(cmd: list[str], flag: str) -> str:
    """Return the argv element immediately following ``flag``.

    Asserts adjacency so a regression that emits ``--profile r --region p``
    (values swapped) is caught — plain ``"p" in cmd`` membership would not.
    """
    assert flag in cmd, f"{flag} not in {cmd}"
    return cmd[cmd.index(flag) + 1]


# ── aws_cli_args ──────────────────────────────────────────────────────────


def test_aws_cli_args_includes_only_set_values():
    assert deploy.aws_cli_args(None, None) == []
    assert deploy.aws_cli_args("p", None) == ["--profile", "p"]
    assert deploy.aws_cli_args(None, "r") == ["--region", "r"]
    assert deploy.aws_cli_args("p", "r") == ["--profile", "p", "--region", "r"]
    # include_region=False drops --region (the cdk case) but keeps --profile.
    assert deploy.aws_cli_args("p", "r", include_region=False) == ["--profile", "p"]
    assert deploy.aws_cli_args(None, "r", include_region=False) == []


# ── describe_stack ────────────────────────────────────────────────────────


def _cfn_mock(*, describe_return=None, describe_side_effect=None) -> MagicMock:
    """Patch the CloudFormation client factory to a mock and return it.

    ``describe_return`` sets the ``describe_stacks`` response dict;
    ``describe_side_effect`` makes it raise instead.
    """
    client = MagicMock()
    if describe_side_effect is not None:
        client.describe_stacks.side_effect = describe_side_effect
    else:
        client.describe_stacks.return_value = describe_return
    return client


def test_describe_stack_returns_stack_dict():
    client = _cfn_mock(
        describe_return={
            "Stacks": [
                {"StackName": "PhysaiClusterStack", "StackStatus": "CREATE_COMPLETE"}
            ]
        }
    )
    with patch(
        "physai_regression.orchestration.deploy.cloudformation_client",
        return_value=client,
    ):
        stack = deploy.describe_stack("PhysaiClusterStack", profile="p", region="r")
    assert stack["StackStatus"] == "CREATE_COMPLETE"
    client.describe_stacks.assert_called_once_with(StackName="PhysaiClusterStack")


def test_describe_stack_raises_StackNotFound_when_stack_absent():
    """CloudFormation's "does not exist" ValidationError → StackNotFound."""
    client = _cfn_mock(
        describe_side_effect=_client_error(
            "ValidationError", "Stack with id X does not exist"
        )
    )
    with patch(
        "physai_regression.orchestration.deploy.cloudformation_client",
        return_value=client,
    ):
        with pytest.raises(deploy.StackNotFound, match="does not exist"):
            deploy.describe_stack("X")


def test_describe_stack_generic_validation_error_is_query_error():
    """A ValidationError that is NOT "does not exist" must be a query error,
    not mistaken for absence (ValidationError is a generic code)."""
    client = _cfn_mock(
        describe_side_effect=_client_error(
            "ValidationError", "Template error: something else"
        )
    )
    with patch(
        "physai_regression.orchestration.deploy.cloudformation_client",
        return_value=client,
    ):
        with pytest.raises(deploy.StackQueryError, match="ValidationError"):
            deploy.describe_stack("X")


def test_describe_stack_raises_StackQueryError_on_non_absence_failure():
    """Auth/network/throttle failures must NOT be mistaken for absence."""
    client = _cfn_mock(
        describe_side_effect=_client_error(
            "ExpiredToken", "The security token included in the request has expired"
        )
    )
    with patch(
        "physai_regression.orchestration.deploy.cloudformation_client",
        return_value=client,
    ):
        with pytest.raises(deploy.StackQueryError, match="ExpiredToken"):
            deploy.describe_stack("X")


# ── stack_exists ──────────────────────────────────────────────────────────


def test_stack_exists_returns_true_for_running_stack():
    with patch(
        "physai_regression.orchestration.deploy.describe_stack",
        return_value={"StackStatus": "CREATE_COMPLETE"},
    ) as ds:
        assert (
            deploy.stack_exists("PhysaiClusterStack", profile="p", region="r") is True
        )
    # Pin the stack name and forwarded creds — a regression that dropped
    # profile/region would otherwise pass.
    assert ds.call_args.args == ("PhysaiClusterStack",)
    assert ds.call_args.kwargs == {"profile": "p", "region": "r"}


def test_stack_exists_returns_false_when_stack_absent():
    """Genuine absence → describe_stack raises StackNotFound → False."""
    with patch(
        "physai_regression.orchestration.deploy.describe_stack",
        side_effect=deploy.StackNotFound("does not exist"),
    ):
        assert deploy.stack_exists("PhysaiClusterStack") is False


def test_stack_exists_propagates_query_error_rather_than_reporting_absent():
    """A StackQueryError must propagate so cdk_destroy doesn't silently skip
    and leak a live stack when CloudFormation is merely unreachable."""
    with patch(
        "physai_regression.orchestration.deploy.describe_stack",
        side_effect=deploy.StackQueryError("ExpiredToken"),
    ):
        with pytest.raises(deploy.StackQueryError):
            deploy.stack_exists("PhysaiClusterStack")


def test_stack_exists_returns_false_for_delete_complete():
    with patch(
        "physai_regression.orchestration.deploy.describe_stack",
        return_value={"StackStatus": "DELETE_COMPLETE"},
    ):
        assert deploy.stack_exists("PhysaiClusterStack") is False


# ── cdk_deploy ────────────────────────────────────────────────────────────


def test_cdk_deploy_passes_profile_as_flag_and_region_via_env():
    """``cdk deploy`` silently ignores ``--region`` (aws/aws-cdk#28725).

    The region must reach the synth subprocess via ``AWS_REGION`` /
    ``AWS_DEFAULT_REGION`` so env-agnostic stacks resolve to the right
    region. ``--profile`` is documented and stays as a flag.
    """
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(),
    ) as run:
        deploy.cdk_deploy(profile="p", region="r")
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["npx", "cdk", "deploy"]
    assert "PhysaiClusterStack" in cmd
    assert _flag_value(cmd, "--require-approval") == "never"
    assert _flag_value(cmd, "--profile") == "p"
    assert "--region" not in cmd, "--region must NOT be on the cdk argv"
    env = run.call_args.kwargs["env"]
    assert env["AWS_REGION"] == "r"
    assert env["AWS_DEFAULT_REGION"] == "r"
    assert run.call_args.kwargs["cwd"] == str(deploy.DEFAULT_INFRA_DIR)


def test_cdk_deploy_without_region_inherits_parent_env():
    """When ``region`` is not passed, the subprocess sees the parent env unchanged."""
    with (
        patch.dict(
            "os.environ",
            {"AWS_REGION": "parent-region", "AWS_DEFAULT_REGION": "parent-region"},
            clear=False,
        ),
        patch(
            "physai_regression.orchestration.deploy.subprocess.run",
            return_value=_completed(),
        ) as run,
    ):
        deploy.cdk_deploy(profile="p")
    env = run.call_args.kwargs["env"]
    assert env["AWS_REGION"] == "parent-region"
    assert env["AWS_DEFAULT_REGION"] == "parent-region"


def test_cdk_deploy_uses_provided_infra_dir(tmp_path):
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(),
    ) as run:
        deploy.cdk_deploy(infra_dir=tmp_path)
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


def test_cdk_deploy_raises_on_nonzero_exit():
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(returncode=1),
    ):
        with pytest.raises(RuntimeError, match="cdk deploy"):
            deploy.cdk_deploy()


# ── cdk_destroy ───────────────────────────────────────────────────────────


def test_cdk_destroy_skips_when_stack_absent():
    with (
        patch(
            "physai_regression.orchestration.deploy.stack_exists", return_value=False
        ) as exists,
        patch("physai_regression.orchestration.deploy.subprocess.run") as run,
    ):
        deploy.cdk_destroy(profile="p", region="r")
    exists.assert_called_once()
    run.assert_not_called()


def test_cdk_destroy_runs_when_stack_present():
    with (
        patch("physai_regression.orchestration.deploy.stack_exists", return_value=True),
        patch(
            "physai_regression.orchestration.deploy.subprocess.run",
            return_value=_completed(),
        ) as run,
    ):
        deploy.cdk_destroy(profile="p", region="r")
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["npx", "cdk", "destroy"]
    assert "PhysaiClusterStack" in cmd
    assert "--force" in cmd
    assert _flag_value(cmd, "--profile") == "p"
    assert "--region" not in cmd, "--region must NOT be on the cdk argv"
    env = run.call_args.kwargs["env"]
    assert env["AWS_REGION"] == "r"
    assert env["AWS_DEFAULT_REGION"] == "r"


def test_cdk_destroy_skip_if_absent_false_runs_unconditionally():
    with (
        patch("physai_regression.orchestration.deploy.stack_exists") as exists,
        patch(
            "physai_regression.orchestration.deploy.subprocess.run",
            return_value=_completed(),
        ) as run,
    ):
        deploy.cdk_destroy(skip_if_absent=False)
    exists.assert_not_called()
    run.assert_called_once()


def test_cdk_destroy_raises_on_nonzero_exit():
    with (
        patch("physai_regression.orchestration.deploy.stack_exists", return_value=True),
        patch(
            "physai_regression.orchestration.deploy.subprocess.run",
            return_value=_completed(returncode=1),
        ),
    ):
        with pytest.raises(RuntimeError, match="cdk destroy"):
            deploy.cdk_destroy()


# ── flows.redeploy_from_clean ─────────────────────────────────────────────


def test_redeploy_from_clean_destroys_then_deploys():
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_destroy",
            side_effect=lambda **_: call_order.append("destroy"),
        ) as destroy,
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_deploy",
            side_effect=lambda **_: call_order.append("deploy"),
        ) as do_deploy,
    ):
        flows.redeploy_from_clean(profile="p", region="r")
    assert call_order == ["destroy", "deploy"]
    destroy.assert_called_once_with(profile="p", region="r", skip_if_absent=True)
    do_deploy.assert_called_once_with(profile="p", region="r")


# ── run_lifecycle_all ─────────────────────────────────────────────────────


def test_run_lifecycle_all_invokes_script_with_aws_args():
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(),
    ) as run:
        deploy.run_lifecycle_all(profile="p", region="r")
    cmd = run.call_args.args[0]
    assert cmd[0] == str(deploy.RUN_LIFECYCLE_SH)
    assert "--all" in cmd
    assert _flag_value(cmd, "--profile") == "p"
    assert _flag_value(cmd, "--region") == "r"
    assert run.call_args.kwargs["cwd"] == str(deploy.DEFAULT_INFRA_DIR)


def test_run_lifecycle_all_raises_on_nonzero_exit():
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(returncode=2),
    ):
        with pytest.raises(RuntimeError, match="run-lifecycle.sh"):
            deploy.run_lifecycle_all(profile="p", region="r")


def test_run_lifecycle_all_raises_when_script_missing(tmp_path):
    with patch(
        "physai_regression.orchestration.deploy.RUN_LIFECYCLE_SH",
        tmp_path / "missing.sh",
    ):
        with pytest.raises(RuntimeError, match="not found"):
            deploy.run_lifecycle_all()


# ── npm_ci ────────────────────────────────────────────────────────────────


def test_npm_ci_invokes_npm_in_infra_dir(tmp_path):
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(),
    ) as run:
        deploy.npm_ci(tmp_path)
    assert run.call_args.args[0] == ["npm", "ci"]
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


def test_npm_ci_raises_on_nonzero_exit(tmp_path):
    with patch(
        "physai_regression.orchestration.deploy.subprocess.run",
        return_value=_completed(returncode=1),
    ):
        with pytest.raises(RuntimeError, match="npm ci failed"):
            deploy.npm_ci(tmp_path)


# ── flows.upgrade_in_place ────────────────────────────────────────────────


def test_upgrade_in_place_deploys_then_runs_lifecycle():
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_deploy",
            side_effect=lambda **_: call_order.append("deploy"),
        ),
        patch(
            "physai_regression.orchestration.flows.deploy.run_lifecycle_all",
            side_effect=lambda **_: call_order.append("lifecycle"),
        ),
        patch("physai_regression.orchestration.flows.deploy.cdk_destroy") as destroy,
    ):
        flows.upgrade_in_place(profile="p", region="r")
    assert call_order == ["deploy", "lifecycle"]
    destroy.assert_not_called()


def test_upgrade_in_place_forwards_infra_dir(tmp_path):
    with (
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy") as do_deploy,
        patch("physai_regression.orchestration.flows.deploy.run_lifecycle_all"),
    ):
        flows.upgrade_in_place(profile="p", region="r", infra_dir=tmp_path)
    assert do_deploy.call_args.kwargs["infra_dir"] == tmp_path


# ── flows.deploy_from_ref ─────────────────────────────────────────────────


def _git_dispatch(toplevel: str = "/repo", prefix: str = "physai/"):
    """Stub for subprocess.run that responds to git rev-parse + worktree calls."""
    captured: list[list[str]] = []

    def run(argv, *args, **kwargs):
        captured.append(list(argv))
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            if "--show-toplevel" in argv:
                return _completed(stdout=toplevel + "\n")
            if "--show-prefix" in argv:
                return _completed(stdout=prefix + "\n")
        return _completed()

    return run, captured


def test_deploy_from_ref_creates_worktree_with_physai_prefix_and_deploys_from_it():
    """``physai/`` lives inside an umbrella repo; the worktree is created at
    the umbrella toplevel and the physai subdir is located via the git
    prefix. ``npm ci`` and ``cdk deploy`` run against
    ``<worktree>/<prefix>/infra``."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="physai/")
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch("physai_regression.orchestration.flows.deploy.npm_ci") as do_npm_ci,
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy") as do_deploy,
    ):
        physai_path = flows.deploy_from_ref("v0.2.0", profile="p", region="r")
    add_cmd = next(c for c in calls if "worktree" in c and "add" in c)
    assert add_cmd[:3] == ["git", "-C", "/repo"]
    assert "v0.2.0" in add_cmd
    worktree_root_str = add_cmd[add_cmd.index("--detach") + 1]
    worktree_root = Path(worktree_root_str)
    assert "physai-regression-worktree-" in worktree_root.name
    assert physai_path == worktree_root / "physai"
    do_npm_ci.assert_called_once_with(physai_path / "infra")
    do_deploy.assert_called_once()
    assert do_deploy.call_args.kwargs["infra_dir"] == physai_path / "infra"
    assert do_deploy.call_args.kwargs["profile"] == "p"
    assert do_deploy.call_args.kwargs["region"] == "r"


def test_deploy_from_ref_runs_npm_ci_before_cdk_deploy():
    """npm ci must complete before cdk deploy: cdk needs aws-cdk-lib resolved."""
    run_stub, _ = _git_dispatch(toplevel="/repo", prefix="physai/")
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch(
            "physai_regression.orchestration.flows.deploy.npm_ci",
            side_effect=lambda *_a, **_k: call_order.append("npm_ci"),
        ),
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_deploy",
            side_effect=lambda *_a, **_k: call_order.append("cdk_deploy"),
        ),
    ):
        flows.deploy_from_ref("HEAD", profile=None, region=None)
    assert call_order == ["npm_ci", "cdk_deploy"]


def test_deploy_from_ref_when_physai_is_repo_root():
    """Empty git prefix → physai is the repo root; no subpath appended."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="")
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch("physai_regression.orchestration.flows.deploy.npm_ci"),
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy") as do_deploy,
    ):
        physai_path = flows.deploy_from_ref("HEAD", profile=None, region=None)
    add_cmd = next(c for c in calls if "worktree" in c and "add" in c)
    worktree_root = Path(add_cmd[add_cmd.index("--detach") + 1])
    assert physai_path == worktree_root
    assert do_deploy.call_args.kwargs["infra_dir"] == worktree_root / "infra"


def test_deploy_from_ref_unique_worktree_paths():
    """Random suffix avoids collisions between concurrent runs."""
    run_stub, _ = _git_dispatch()
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch("physai_regression.orchestration.flows.deploy.npm_ci"),
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy"),
    ):
        a = flows.deploy_from_ref("HEAD", profile=None, region=None)
        b = flows.deploy_from_ref("HEAD", profile=None, region=None)
    assert a != b


def test_deploy_from_ref_raises_on_git_failure():
    """Worktree-add failure surfaces as RuntimeError; rev-parse calls succeed first."""

    def run(argv, *args, **kwargs):
        if "rev-parse" in argv:
            if "--show-toplevel" in argv:
                return _completed(stdout="/repo\n")
            return _completed(stdout="physai/\n")
        return _completed(returncode=128, stderr="fatal: bad ref")

    with patch("physai_regression.orchestration.flows.subprocess.run", side_effect=run):
        with pytest.raises(RuntimeError, match="git worktree add"):
            flows.deploy_from_ref("nope", profile=None, region=None)


def test_deploy_from_ref_prints_worktree_path_when_npm_ci_fails(capsys):
    """npm ci failure after worktree-add must surface the worktree path."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="physai/")
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch(
            "physai_regression.orchestration.flows.deploy.npm_ci",
            side_effect=RuntimeError("npm ci failed in X (exit 1)"),
        ),
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy") as do_deploy,
    ):
        with pytest.raises(RuntimeError, match="npm ci failed"):
            flows.deploy_from_ref("v0.2.0", profile="p", region="r")
    do_deploy.assert_not_called()  # cdk_deploy never reached
    add_cmd = next(c for c in calls if "worktree" in c and "add" in c)
    worktree_root = add_cmd[add_cmd.index("--detach") + 1]
    err = capsys.readouterr().err
    assert worktree_root in err
    assert "git worktree remove --force" in err


def test_deploy_from_ref_prints_worktree_path_when_cdk_deploy_fails(capsys):
    """cdk deploy failure after worktree-add must surface the worktree path."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="physai/")
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch("physai_regression.orchestration.flows.deploy.npm_ci"),
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_deploy",
            side_effect=RuntimeError("cdk deploy PhysaiClusterStack failed (exit 1)"),
        ),
    ):
        with pytest.raises(RuntimeError, match="cdk deploy"):
            flows.deploy_from_ref("HEAD", profile=None, region=None)
    add_cmd = next(c for c in calls if "worktree" in c and "add" in c)
    worktree_root = add_cmd[add_cmd.index("--detach") + 1]
    assert worktree_root in capsys.readouterr().err


def test_deploy_from_ref_prints_nothing_on_success(capsys):
    """The keep-on-failure message must not fire on the happy path."""
    run_stub, _ = _git_dispatch(toplevel="/repo", prefix="physai/")
    with (
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
        patch("physai_regression.orchestration.flows.deploy.npm_ci"),
        patch("physai_regression.orchestration.flows.deploy.cdk_deploy"),
    ):
        flows.deploy_from_ref("HEAD", profile=None, region=None)
    assert "worktree left for inspection" not in capsys.readouterr().err


# ── flows.destroy_and_remove_worktree ─────────────────────────────────────


def test_destroy_and_remove_worktree_destroys_then_removes_worktree():
    """Worktree removal targets the worktree root, derived by stripping the
    physai prefix from the path returned by :func:`deploy_from_ref`."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="physai/")
    call_order: list[str] = []
    with (
        patch(
            "physai_regression.orchestration.flows.deploy.cdk_destroy",
            side_effect=lambda **_: call_order.append("destroy"),
        ),
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=lambda *a, **k: call_order.append("git") or run_stub(*a, **k),
        ),
    ):
        flows.destroy_and_remove_worktree(
            Path("/tmp/wt-x/physai"), profile="p", region="r"
        )
    assert call_order[0] == "destroy"
    assert "git" in call_order  # at least one git invocation after destroy
    remove_cmd = next(c for c in calls if "worktree" in c and "remove" in c)
    assert remove_cmd[:3] == ["git", "-C", "/repo"]
    assert "--force" in remove_cmd
    assert "/tmp/wt-x" in remove_cmd
    # Worktree root is the parent of the physai subpath; not the physai dir itself.
    assert "/tmp/wt-x/physai" not in remove_cmd


def test_destroy_and_remove_worktree_when_physai_is_repo_root():
    """Empty prefix → worktree root *is* the path passed in."""
    run_stub, calls = _git_dispatch(toplevel="/repo", prefix="")
    with (
        patch("physai_regression.orchestration.flows.deploy.cdk_destroy"),
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run_stub,
        ),
    ):
        flows.destroy_and_remove_worktree(Path("/tmp/wt-x"), profile=None, region=None)
    remove_cmd = next(c for c in calls if "worktree" in c and "remove" in c)
    assert "/tmp/wt-x" in remove_cmd


def test_destroy_and_remove_worktree_raises_on_git_failure():
    def run(argv, *args, **kwargs):
        if "rev-parse" in argv:
            if "--show-toplevel" in argv:
                return _completed(stdout="/repo\n")
            return _completed(stdout="physai/\n")
        return _completed(returncode=1, stderr="not a worktree")

    with (
        patch("physai_regression.orchestration.flows.deploy.cdk_destroy"),
        patch(
            "physai_regression.orchestration.flows.subprocess.run",
            side_effect=run,
        ),
    ):
        with pytest.raises(RuntimeError, match="git worktree remove"):
            flows.destroy_and_remove_worktree(
                Path("/tmp/wt-x/physai"), profile=None, region=None
            )
