"""Tests for fixtures in ``physai_regression.conftest``.

Cluster discovery and ssh-config generation invoke ``aws`` and the
``setup-ssh.sh`` script via ``subprocess.run``. We mock that boundary so
the tests exercise the fixture wiring without touching AWS or the
filesystem outside ``tmp_path``.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from physai_regression import conftest as conftest_module


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _unwrap(fixture):
    """Return a fixture's underlying function so we can call it directly.

    ``@pytest.fixture`` stores the undecorated function on ``__wrapped__``;
    the tests call it with explicit args instead of going through pytest's
    fixture injection.
    """
    return fixture.__wrapped__


# ── cluster_name ────────────────────────────────────────────────────────────


def _request_with(cluster: str | None = None) -> MagicMock:
    req = MagicMock()
    req.config.getoption.side_effect = lambda name: {"--cluster": cluster}.get(name)
    return req


def test_cluster_name_uses_cli_override_without_calling_aws():
    with patch("physai_regression.conftest.describe_stack") as ds:
        out = _unwrap(conftest_module.cluster_name)(
            _request_with(cluster="explicit-cluster"),
            aws_profile=None,
            aws_region=None,
        )
    assert out == "explicit-cluster"
    ds.assert_not_called()


def test_cluster_name_resolves_from_cfn_output():
    with patch(
        "physai_regression.conftest.describe_stack",
        return_value={
            "Outputs": [
                {"OutputKey": "Other", "OutputValue": "ignore-me"},
                {"OutputKey": "ClusterName", "OutputValue": "physai-cluster-abc12345"},
            ]
        },
    ) as ds:
        out = _unwrap(conftest_module.cluster_name)(
            _request_with(),
            aws_profile="myprofile",
            aws_region="us-west-2",
        )
    assert out == "physai-cluster-abc12345"
    kwargs = ds.call_args.kwargs
    assert kwargs == {"profile": "myprofile", "region": "us-west-2"}
    # The stack name is positional; describe_stack no longer takes a query.
    assert ds.call_args.args[0] == "PhysaiClusterStack"


def test_cluster_name_fails_loudly_when_output_empty():
    with patch(
        "physai_regression.conftest.describe_stack",
        # Stack exists but has no matching output key.
        return_value={"Outputs": [{"OutputKey": "Other", "OutputValue": "x"}]},
    ):
        with pytest.raises(pytest.fail.Exception) as excinfo:
            _unwrap(conftest_module.cluster_name)(
                _request_with(),
                aws_profile=None,
                aws_region=None,
            )
    assert "ClusterName" in str(excinfo.value)


def test_cluster_name_fails_loudly_when_stack_not_found():
    with patch(
        "physai_regression.conftest.describe_stack",
        side_effect=conftest_module.StackNotFound("no such stack"),
    ):
        with pytest.raises(pytest.fail.Exception) as excinfo:
            _unwrap(conftest_module.cluster_name)(
                _request_with(),
                aws_profile=None,
                aws_region=None,
            )
    assert "no such stack" in str(excinfo.value)


# ── ssh_config_path ─────────────────────────────────────────────────────────


def test_ssh_config_path_invokes_setup_ssh_with_output_flag(tmp_path: Path):
    fake_setup = tmp_path / "setup-ssh.sh"
    fake_setup.write_text("#!/bin/sh\n")
    fake_setup.chmod(0o755)

    with (
        patch("physai_regression.conftest.SETUP_SSH", fake_setup),
        patch(
            "physai_regression.conftest.subprocess.run",
            return_value=_completed(stdout="ok"),
        ) as run,
    ):
        gen = _unwrap(conftest_module.ssh_config_path)(
            cluster_name="my-cluster",
            aws_profile="p",
            aws_region="r",
        )
        path = next(gen)
        try:
            cmd = run.call_args.args[0]
            assert cmd[0] == str(fake_setup)
            assert "--cluster" in cmd and "my-cluster" in cmd
            assert "--output" in cmd
            output_path = Path(cmd[cmd.index("--output") + 1])
            assert output_path == path
            assert "--profile" in cmd and "p" in cmd
            assert "--region" in cmd and "r" in cmd
        finally:
            # Run the cleanup half of the fixture.
            for _ in gen:
                pass


def test_ssh_config_path_cleans_up_tempfile_after_session(tmp_path: Path):
    fake_setup = tmp_path / "setup-ssh.sh"
    fake_setup.write_text("#!/bin/sh\n")
    fake_setup.chmod(0o755)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        # Touch the output file so the cleanup branch has something to remove.
        out_idx = cmd.index("--output")
        Path(cmd[out_idx + 1]).write_text("Host physai-login\n")
        captured["path"] = cmd[out_idx + 1]
        return _completed()

    with (
        patch("physai_regression.conftest.SETUP_SSH", fake_setup),
        patch("physai_regression.conftest.subprocess.run", side_effect=fake_run),
    ):
        gen = _unwrap(conftest_module.ssh_config_path)(
            cluster_name="c",
            aws_profile=None,
            aws_region=None,
        )
        path = next(gen)
        assert path.exists()
        for _ in gen:
            pass
        assert not path.exists()
        assert captured["path"] == str(path)


def test_ssh_config_path_fails_loudly_when_setup_ssh_returns_nonzero(tmp_path: Path):
    fake_setup = tmp_path / "setup-ssh.sh"
    fake_setup.write_text("#!/bin/sh\nexit 1\n")
    fake_setup.chmod(0o755)

    with (
        patch("physai_regression.conftest.SETUP_SSH", fake_setup),
        patch(
            "physai_regression.conftest.subprocess.run",
            return_value=_completed(returncode=1, stderr="boom"),
        ),
    ):
        gen = _unwrap(conftest_module.ssh_config_path)(
            cluster_name="c",
            aws_profile=None,
            aws_region=None,
        )
        with pytest.raises(pytest.fail.Exception) as excinfo:
            next(gen)
        assert "setup-ssh.sh" in str(excinfo.value)


# ── pytest_collection_modifyitems: --raw-source guard ───────────────────────

# These run the guard through *real* pytest collection via the `pytester`
# fixture rather than calling the hook with mock items. The hook's
# correctness depends on running after pytest's own ``-m`` deselection
# (``trylast=True``); a mock-item call can't reproduce that ordering, so it
# would miss the case where a default ``-m "not builtin_example"`` run
# wrongly aborts because the Layer 2 item is still in ``items`` at hook time.
pytest_plugins = ["pytester"]

# Mirrors the regression suite's own pytest.ini: Layer 2 is deselected by
# default, so an ordinary run must not require --raw-source.
_GUARD_INI = """
[pytest]
addopts = -m "not builtin_example"
markers =
    platform: layer 1
    builtin_example: layer 2
"""


def _make_guard_project(pytester: pytest.Pytester) -> None:
    pytester.makeini(_GUARD_INI)
    pytester.makeconftest(
        "from physai_regression.conftest import (\n"
        "    pytest_addoption,\n"
        "    pytest_collection_modifyitems,\n"
        ")\n"
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.platform
        def test_layer1():
            pass

        @pytest.mark.builtin_example
        def test_layer2():
            pass
        """
    )


def test_default_run_does_not_require_raw_source(pytester: pytest.Pytester):
    """Default run (Layer 2 deselected by ini) must run Layer 1, not abort."""
    _make_guard_project(pytester)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, deselected=1)


def test_selecting_builtin_example_without_raw_source_errors(pytester: pytest.Pytester):
    _make_guard_project(pytester)
    result = pytester.runpytest("-m", "platform or builtin_example")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*--raw-source*"])


def test_selecting_builtin_example_with_raw_source_runs(pytester: pytest.Pytester):
    _make_guard_project(pytester)
    result = pytester.runpytest(
        "-m", "platform or builtin_example", "--raw-source", "file:///tmp/raw"
    )
    result.assert_outcomes(passed=2)
