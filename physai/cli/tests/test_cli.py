"""Tests for CLI argument parsing and dispatch.

Drives `physai.cli.main()` end-to-end with a mocked Session and stubbed
command implementations, so argparse (including subparser / parent-parser
default merging) is exercised for real.
"""

from unittest.mock import MagicMock

import pytest

from physai import cli


@pytest.fixture
def cli_env(monkeypatch):
    """Patch the CLI's external collaborators and return the stubs."""
    session = MagicMock(name="Session")
    session_factory = MagicMock(name="SessionFactory", return_value=session)
    monkeypatch.setattr(cli, "Session", session_factory)

    # config.load returns the resolved host; we record what it was called with.
    config_load = MagicMock(
        name="config.load",
        side_effect=lambda host_override=None, ssh_config_override=None: {
            "host": host_override or "cfg-host",
            "ssh_config": ssh_config_override,
            "model_config_roots": ["/cfg/root"],
            "data_bucket": "cfg-bucket",
        },
    )
    monkeypatch.setattr(cli.config, "load", config_load)

    # Stub every command implementation.
    stubs = {
        "run_build": MagicMock(name="build.run_build"),
        "run_pipeline": MagicMock(name="pipeline.run_pipeline"),
        "run_train": MagicMock(name="pipeline.run_train"),
        "run_eval": MagicMock(name="pipeline.run_eval"),
        "run_convert": MagicMock(name="pipeline.run_convert"),
        "list_jobs": MagicMock(name="jobs.list_jobs"),
        "status_job": MagicMock(name="jobs.status_job"),
        "logs_job": MagicMock(name="jobs.logs_job"),
        "cancel_job": MagicMock(name="jobs.cancel_job"),
        "ls": MagicMock(name="data.ls"),
        "upload": MagicMock(name="data.upload"),
        "save": MagicMock(name="data.save"),
        "run_clean": MagicMock(name="clean.run_clean"),
        "run_doctor": MagicMock(name="doctor.run_doctor"),
    }
    monkeypatch.setattr(cli.build, "run_build", stubs["run_build"])
    monkeypatch.setattr(cli.pipeline, "run_pipeline", stubs["run_pipeline"])
    monkeypatch.setattr(cli.pipeline, "run_train", stubs["run_train"])
    monkeypatch.setattr(cli.pipeline, "run_eval", stubs["run_eval"])
    monkeypatch.setattr(cli.pipeline, "run_convert", stubs["run_convert"])
    monkeypatch.setattr(cli.jobs, "list_jobs", stubs["list_jobs"])
    monkeypatch.setattr(cli.jobs, "status_job", stubs["status_job"])
    monkeypatch.setattr(cli.jobs, "logs_job", stubs["logs_job"])
    monkeypatch.setattr(cli.jobs, "cancel_job", stubs["cancel_job"])
    monkeypatch.setattr(cli.data, "ls", stubs["ls"])
    monkeypatch.setattr(cli.data, "upload", stubs["upload"])
    monkeypatch.setattr(cli.data, "save", stubs["save"])
    monkeypatch.setattr(cli.clean, "run_clean", stubs["run_clean"])
    monkeypatch.setattr(cli.doctor, "run_doctor", stubs["run_doctor"])

    return {
        "session": session,
        "session_factory": session_factory,
        "config_load": config_load,
        **stubs,
    }


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["physai", *argv])
    cli.main()


# ── --host parsing (regression tests for the parents=[] default-merge bug) ──


def test_host_at_top_level_survives_to_subcommand(cli_env, monkeypatch):
    run_cli(monkeypatch, "--host", "h1", "status", "5")
    cli_env["config_load"].assert_called_once_with(
        host_override="h1", ssh_config_override=None
    )
    cli_env["session_factory"].assert_called_once_with("h1", ssh_config=None)


def test_host_on_subcommand_wins(cli_env, monkeypatch):
    run_cli(monkeypatch, "--host", "h1", "status", "--host", "h2", "5")
    cli_env["config_load"].assert_called_once_with(
        host_override="h2", ssh_config_override=None
    )


def test_no_host_falls_back_to_config(cli_env, monkeypatch):
    run_cli(monkeypatch, "status", "5")
    cli_env["config_load"].assert_called_once_with(
        host_override=None, ssh_config_override=None
    )
    cli_env["session_factory"].assert_called_once_with("cfg-host", ssh_config=None)


def test_ssh_config_at_top_level(cli_env, monkeypatch):
    run_cli(monkeypatch, "--ssh-config", "/tmp/x", "status", "5")
    cli_env["config_load"].assert_called_once_with(
        host_override=None, ssh_config_override="/tmp/x"
    )
    cli_env["session_factory"].assert_called_once_with("cfg-host", ssh_config="/tmp/x")


def test_ssh_config_on_subcommand_wins(cli_env, monkeypatch):
    run_cli(
        monkeypatch,
        "--ssh-config",
        "/tmp/x",
        "status",
        "--ssh-config",
        "/tmp/y",
        "5",
    )
    cli_env["config_load"].assert_called_once_with(
        host_override=None, ssh_config_override="/tmp/y"
    )


# ── build ──


def test_build_basic(cli_env, monkeypatch):
    run_cli(monkeypatch, "build", "/path/to/cntr")
    cli_env["run_build"].assert_called_once_with(
        cli_env["session"], "/path/to/cntr", False, stream=True
    )


def test_build_rebuild_and_no_stream(cli_env, monkeypatch):
    run_cli(monkeypatch, "build", "/p", "--rebuild", "-n")
    cli_env["run_build"].assert_called_once_with(
        cli_env["session"], "/p", True, stream=False
    )


def test_build_no_stream_long_form(cli_env, monkeypatch):
    run_cli(monkeypatch, "build", "/p", "--no-stream")
    _, kwargs = cli_env["run_build"].call_args
    assert kwargs["stream"] is False


# ── run / train / eval ──


def test_run_full(cli_env, monkeypatch):
    run_cli(
        monkeypatch,
        "run",
        "--config",
        "cfg.yaml",
        "--from",
        "train",
        "--to",
        "eval",
        "--dataset",
        "ds",
        "--max-steps",
        "500",
        "--eval-rounds",
        "10",
        "--visual",
        "--model-config-root",
        "/a",
        "--model-config-root",
        "/b",
        "-n",
    )
    cli_env["run_pipeline"].assert_called_once()
    args, kwargs = cli_env["run_pipeline"].call_args
    assert args[0] is cli_env["session"]
    assert str(args[1]).endswith("cfg.yaml")
    # CLI roots prepend to config roots
    roots = [str(p) for p in args[2]]
    assert roots == ["/a", "/b", "/cfg/root"]
    assert kwargs == {
        "from_stage": "train",
        "to_stage": "eval",
        "raw": None,
        "dataset": "ds",
        "checkpoint": None,
        "max_steps": 500,
        "eval_rounds": 10,
        "visual": True,
        "visual_timeout": 3600,
        "stream": False,
    }


def test_run_requires_config(cli_env, monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "run", "--dataset", "ds")


def test_train_shortcut(cli_env, monkeypatch):
    run_cli(
        monkeypatch,
        "train",
        "--config",
        "cfg.yaml",
        "--dataset",
        "ds",
        "--max-steps",
        "42",
    )
    cli_env["run_train"].assert_called_once()
    args, kwargs = cli_env["run_train"].call_args
    assert args[0] is cli_env["session"]
    assert args[2] == "ds"
    assert kwargs["max_steps"] == 42
    assert kwargs["stream"] is True


def test_eval_shortcut(cli_env, monkeypatch):
    run_cli(
        monkeypatch,
        "eval",
        "--config",
        "cfg.yaml",
        "--checkpoint",
        "ckpt",
        "--visual",
        "-n",
    )
    cli_env["run_eval"].assert_called_once()
    args, kwargs = cli_env["run_eval"].call_args
    assert args[2] == "ckpt"
    assert kwargs["visual"] is True
    assert kwargs["stream"] is False


def test_convert_shortcut(cli_env, monkeypatch):
    run_cli(
        monkeypatch,
        "convert",
        "--config",
        "cfg.yaml",
        "--raw",
        "my-raw-data",
        "-n",
    )
    cli_env["run_convert"].assert_called_once()
    args, kwargs = cli_env["run_convert"].call_args
    assert args[0] is cli_env["session"]
    assert args[2] == "my-raw-data"
    assert kwargs["stream"] is False


# ── data / jobs / clean ──


def test_list(cli_env, monkeypatch):
    run_cli(monkeypatch, "list")
    cli_env["list_jobs"].assert_called_once_with(cli_env["session"])


def test_ls_with_path(cli_env, monkeypatch):
    run_cli(monkeypatch, "ls", "datasets", "foo")
    cli_env["ls"].assert_called_once_with(cli_env["session"], "datasets", "foo")


def test_upload(cli_env, monkeypatch):
    run_cli(monkeypatch, "upload", "raw", "/local/path")
    cli_env["upload"].assert_called_once_with(cli_env["session"], "raw", "/local/path")


def test_save_defaults(cli_env, monkeypatch):
    run_cli(monkeypatch, "save", "checkpoints", "my-run")
    cli_env["save"].assert_called_once_with(
        cli_env["session"],
        "checkpoints",
        "my-run",
        data_bucket="cfg-bucket",
        force=False,
        dry_run=False,
        submit_job=False,
        stream=True,
        delete=False,
    )


def test_save_force_and_dry_run(cli_env, monkeypatch):
    run_cli(monkeypatch, "save", "checkpoints", "my-run", "-f", "--dry-run")
    cli_env["save"].assert_called_once_with(
        cli_env["session"],
        "checkpoints",
        "my-run",
        data_bucket="cfg-bucket",
        force=True,
        dry_run=True,
        submit_job=False,
        stream=True,
        delete=False,
    )


def test_save_delete_flag(cli_env, monkeypatch):
    run_cli(monkeypatch, "save", "checkpoints", "my-run", "--delete")
    _, kwargs = cli_env["save"].call_args
    assert kwargs["delete"] is True


def test_save_job_flag(cli_env, monkeypatch):
    run_cli(monkeypatch, "save", "checkpoints", "my-run", "--job")
    _, kwargs = cli_env["save"].call_args
    assert kwargs["submit_job"] is True
    assert kwargs["stream"] is True


def test_save_job_no_stream(cli_env, monkeypatch):
    run_cli(monkeypatch, "save", "checkpoints", "my-run", "--job", "-n")
    _, kwargs = cli_env["save"].call_args
    assert kwargs["submit_job"] is True
    assert kwargs["stream"] is False


def test_save_rejects_other_categories(cli_env, monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "save", "datasets", "my-run")


def test_status(cli_env, monkeypatch):
    run_cli(monkeypatch, "status", "42")
    cli_env["status_job"].assert_called_once_with(cli_env["session"], "42")


def test_logs(cli_env, monkeypatch):
    run_cli(monkeypatch, "logs", "42")
    cli_env["logs_job"].assert_called_once_with(cli_env["session"], "42")


def test_cancel(cli_env, monkeypatch):
    run_cli(monkeypatch, "cancel", "42")
    cli_env["cancel_job"].assert_called_once_with(cli_env["session"], "42")


def test_clean_defaults(cli_env, monkeypatch):
    run_cli(monkeypatch, "clean")
    cli_env["run_clean"].assert_called_once_with(
        cli_env["session"], older_than=7, dry_run=False, force=False, enroot=False
    )


def test_clean_all_enroot(cli_env, monkeypatch):
    run_cli(monkeypatch, "clean", "--all", "--enroot", "-f")
    cli_env["run_clean"].assert_called_once_with(
        cli_env["session"], older_than=0, dry_run=False, force=True, enroot=True
    )


def test_doctor(cli_env, monkeypatch):
    run_cli(monkeypatch, "doctor")
    cli_env["run_doctor"].assert_called_once_with(cli_env["session"])


# ── argparse rejections ──


def test_unknown_command_exits(cli_env, monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "nope")


def test_no_command_prints_help_and_exits(cli_env, monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch)
