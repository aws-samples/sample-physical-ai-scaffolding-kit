"""Tests for physai pipeline (eval, train)."""

import pytest

from physai.pipeline import (
    _generate_eval_sbatch,
    _generate_train_sbatch,
    _get_resources,
    _load_run_config,
    _resolve_model_config,
)


# ── _load_run_config ──


def test_load_run_config(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text(
        "model:\n  config_dir: gr00t/so101\nresources:\n  eval: {container: rt}\n"
    )
    cfg = _load_run_config(str(cfg_file))
    assert cfg["model"]["config_dir"] == "gr00t/so101"
    assert cfg["resources"]["eval"]["container"] == "rt"


def test_load_run_config_missing_file():
    with pytest.raises(SystemExit, match="not found"):
        _load_run_config("/nonexistent.yaml")


def test_load_run_config_missing_model(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("resources:\n  eval: {container: rt}\n")
    with pytest.raises(SystemExit, match="model"):
        _load_run_config(str(cfg_file))


# ── _resolve_model_config ──


def test_resolve_model_config(tmp_path):
    (tmp_path / "gr00t" / "so101").mkdir(parents=True)
    result = _resolve_model_config("gr00t/so101", [str(tmp_path)])
    assert result == (tmp_path / "gr00t" / "so101").resolve()


def test_resolve_model_config_not_found(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        _resolve_model_config("gr00t/so101", [str(tmp_path)])


def test_resolve_model_config_first_match(tmp_path):
    root1 = tmp_path / "a"
    root2 = tmp_path / "b"
    (root1 / "gr00t" / "so101").mkdir(parents=True)
    (root2 / "gr00t" / "so101").mkdir(parents=True)
    result = _resolve_model_config("gr00t/so101", [str(root1), str(root2)])
    assert result == (root1 / "gr00t" / "so101").resolve()


# ── _get_resources ──


def test_get_resources():
    cfg = {"resources": {"eval": {"partition": "gpu", "container": "rt"}}}
    assert _get_resources(cfg, "eval") == {"partition": "gpu", "container": "rt"}


def test_get_resources_missing_stage():
    with pytest.raises(SystemExit, match="resources.train"):
        _get_resources({"resources": {}}, "train")


def test_get_resources_missing_container():
    with pytest.raises(SystemExit, match="container"):
        _get_resources({"resources": {"eval": {"partition": "gpu"}}}, "eval")


# ── _generate_eval_sbatch ──


def test_generate_eval_sbatch():
    run_cfg = {"sim": {"environment": "LeIsaac-SO101-LiftCube-v0"}}
    res = {"partition": "gpu", "gres": "gpu:1", "container": "leisaac-runtime"}
    sbatch = _generate_eval_sbatch(
        run_cfg,
        res,
        run_id="eval-20260414-090000",
        remote_config="/fsx/physai/sync/eval-20260414-090000/run_config.yaml",
        remote_model_config="/fsx/physai/sync/eval-20260414-090000/model_config",
        checkpoint_dir="/fsx/checkpoints/my-ckpt",
        output_dir="/fsx/evaluations/eval-20260414-090000",
        eval_rounds=20,
        visual=False,
    )
    expected = """\
#!/bin/bash
#SBATCH --job-name=physai/eval/leisaac-runtime
#SBATCH --comment="checkpoint=/fsx/checkpoints/my-ckpt"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
export RUN_CONFIG=/fsx/physai/sync/eval-20260414-090000/run_config.yaml
export DISPLAY=${DISPLAY:-:0}
export PYTHONUNBUFFERED=1

srun --container-image=/fsx/enroot/leisaac-runtime.sqsh \\
  --container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix \\
  bash /app/eval.sh \\
    /fsx/checkpoints/my-ckpt \\
    /fsx/physai/sync/eval-20260414-090000/model_config \\
    /fsx/evaluations/eval-20260414-090000 \\
    20
"""
    assert sbatch == expected


def test_generate_eval_sbatch_visual():
    res = {"partition": "gpu", "gres": "gpu:1", "container": "leisaac-runtime"}
    sbatch = _generate_eval_sbatch(
        {}, res, "r", "/cfg", "/mc", "/ckpt", "/out", 10, visual=True
    )
    assert "10 --visual\n" in sbatch


# ── _generate_train_sbatch ──


def test_generate_train_sbatch():
    res = {"partition": "gpu", "gres": "gpu:1", "container": "gr00t-trainer"}
    sbatch = _generate_train_sbatch(
        run_cfg={},
        res=res,
        run_id="train-20260415-100000",
        remote_config="/fsx/physai/sync/train-20260415-100000/run_config.yaml",
        remote_model_config="/fsx/physai/sync/train-20260415-100000/model_config",
        dataset_dir="/fsx/datasets/my-dataset",
        output_dir="/fsx/checkpoints/train-20260415-100000",
        max_steps=30000,
    )
    expected = """\
#!/bin/bash
#SBATCH --job-name=physai/train/gr00t-trainer
#SBATCH --comment="dataset=/fsx/datasets/my-dataset"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
export RUN_CONFIG=/fsx/physai/sync/train-20260415-100000/run_config.yaml
export PYTHONUNBUFFERED=1

srun --container-image=/fsx/enroot/gr00t-trainer.sqsh \\
  --container-mounts=/fsx:/fsx \\
  bash /app/train.sh \\
    /fsx/datasets/my-dataset \\
    /fsx/physai/sync/train-20260415-100000/model_config \\
    /fsx/checkpoints/train-20260415-100000 \\
    30000
"""
    assert sbatch == expected
