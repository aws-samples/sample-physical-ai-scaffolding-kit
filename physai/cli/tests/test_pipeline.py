"""Tests for physai pipeline (run, train, eval)."""

from pathlib import Path

import pytest

from physai.pipeline import (
    EvalStage,
    TrainStage,
    _get_stage_config,
    _load_run_config,
    _resolve_model_config,
    _resolve_stages,
)

# ── _load_run_config ──


def test_load_run_config(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text(
        "model:\n  config_dir: gr00t/so101\nstages:\n  train:\n    container: tr\n"
    )
    cfg = _load_run_config(cfg_file)
    assert cfg["model"]["config_dir"] == "gr00t/so101"
    assert cfg["stages"]["train"]["container"] == "tr"


def test_load_run_config_missing_file():
    with pytest.raises(SystemExit, match="not found"):
        _load_run_config(Path("/nonexistent.yaml"))


def test_load_run_config_missing_model(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("stages:\n  train:\n    container: tr\n")
    with pytest.raises(SystemExit, match="model"):
        _load_run_config(cfg_file)


def test_load_run_config_missing_stages(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text("model:\n  config_dir: gr00t/so101\n")
    with pytest.raises(SystemExit, match="stages"):
        _load_run_config(cfg_file)


# ── _resolve_model_config ──


def test_resolve_model_config(tmp_path):
    (tmp_path / "gr00t" / "so101").mkdir(parents=True)
    result = _resolve_model_config("gr00t/so101", [tmp_path])
    assert result == (tmp_path / "gr00t" / "so101").resolve()


def test_resolve_model_config_not_found(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        _resolve_model_config("gr00t/so101", [tmp_path])


def test_resolve_model_config_first_match(tmp_path):
    root1 = tmp_path / "a"
    root2 = tmp_path / "b"
    (root1 / "gr00t" / "so101").mkdir(parents=True)
    (root2 / "gr00t" / "so101").mkdir(parents=True)
    result = _resolve_model_config("gr00t/so101", [root1, root2])
    assert result == (root1 / "gr00t" / "so101").resolve()


# ── _resolve_stages ──


def test_resolve_stages_default():
    cfg = {"pipeline": {"stages": ["train", "eval"]}}
    assert _resolve_stages(cfg, None, None) == ["train", "eval"]


def test_resolve_stages_from_override():
    cfg = {"pipeline": {"stages": ["train", "eval"]}}
    assert _resolve_stages(cfg, "eval", None) == ["eval"]


def test_resolve_stages_from_to_override():
    cfg = {"pipeline": {"stages": ["convert", "validate", "train", "eval"]}}
    assert _resolve_stages(cfg, "train", "train") == ["train"]


def test_resolve_stages_full_range():
    cfg = {"pipeline": {"stages": ["convert", "validate", "train", "eval", "register"]}}
    assert _resolve_stages(cfg, "convert", "register") == [
        "convert",
        "validate",
        "train",
        "eval",
        "register",
    ]


def test_resolve_stages_from_not_in_config():
    cfg = {"pipeline": {"stages": ["train", "eval"]}}
    with pytest.raises(SystemExit, match="not in pipeline.stages"):
        _resolve_stages(cfg, "convert", None)


def test_resolve_stages_invalid():
    cfg = {"pipeline": {"stages": ["train", "eval"]}}
    with pytest.raises(SystemExit, match="not in pipeline.stages"):
        _resolve_stages(cfg, "bogus", None)


def test_resolve_stages_bad_order():
    cfg = {"pipeline": {"stages": ["train", "eval"]}}
    with pytest.raises(SystemExit, match="must come before"):
        _resolve_stages(cfg, "eval", "train")


def test_resolve_stages_missing_pipeline():
    with pytest.raises(SystemExit, match="pipeline.stages"):
        _resolve_stages({}, None, None)


# ── _get_stage_config ──


def test_get_stage_config():
    cfg = {"stages": {"eval": {"partition": "gpu", "container": "rt"}}}
    assert _get_stage_config(cfg, "eval") == {"partition": "gpu", "container": "rt"}


def test_get_stage_config_missing_stage():
    with pytest.raises(SystemExit, match="stages.train"):
        _get_stage_config({"stages": {}}, "train")


def test_get_stage_config_missing_container():
    with pytest.raises(SystemExit, match="container"):
        _get_stage_config({"stages": {"eval": {"partition": "gpu"}}}, "eval")


# ── TrainStage ──


def _make_stage(
    cls, cfg=None, run_id="run-test", remote_config="/cfg", remote_mc="/mc"
):
    cfg = cfg or {
        "partition": "gpu",
        "gres": "gpu:1",
        "container": "gr00t-n1.6-trainer",
    }
    return cls(cfg, run_id, remote_config, remote_mc)


def test_train_stage_validate_ok():
    stage = _make_stage(TrainStage)
    stage.validate({"dataset_dir": "/fsx/datasets/ds"})  # no error


def test_train_stage_validate_missing():
    stage = _make_stage(TrainStage)
    with pytest.raises(SystemExit, match="--dataset"):
        stage.validate({})


def test_train_stage_sbatch():
    stage = _make_stage(TrainStage, run_id="run-20260415-100000")
    ctx = {
        "dataset_dir": "/fsx/datasets/my-ds",
        "checkpoint_dir": "/fsx/checkpoints/run-20260415-100000",
    }
    sbatch = stage.generate_sbatch(ctx)
    assert "#SBATCH --job-name=physai/run/run-20260415-100000/train" in sbatch
    assert "#SBATCH --partition=gpu" in sbatch
    assert "--container-image=/fsx/enroot/gr00t-n1.6-trainer.sqsh" in sbatch
    assert "/fsx/datasets/my-ds" in sbatch
    assert "10000\n" in sbatch  # default max_steps


def test_train_stage_sbatch_max_steps_override():
    stage = _make_stage(
        TrainStage,
        cfg={"partition": "gpu", "gres": "gpu:1", "container": "tr", "max_steps": 5000},
    )
    ctx = {"dataset_dir": "/ds", "checkpoint_dir": "/cp", "max_steps": 999}
    sbatch = stage.generate_sbatch(ctx)
    assert "999\n" in sbatch  # ctx overrides config


def test_train_stage_sbatch_constraint():
    stage = _make_stage(
        TrainStage,
        cfg={
            "partition": "gpu",
            "gres": "gpu:1",
            "constraint": "l40s",
            "container": "tr",
        },
    )
    ctx = {"dataset_dir": "/ds", "checkpoint_dir": "/cp"}
    sbatch = stage.generate_sbatch(ctx)
    assert "#SBATCH --constraint=l40s" in sbatch


# ── EvalStage ──


def test_eval_stage_validate_ok():
    stage = _make_stage(EvalStage)
    stage.validate({"checkpoint_dir": "/fsx/checkpoints/ckpt"})  # no error


def test_eval_stage_validate_missing():
    stage = _make_stage(EvalStage)
    with pytest.raises(SystemExit, match="--checkpoint"):
        stage.validate({})


def test_eval_stage_sbatch():
    stage = _make_stage(
        EvalStage,
        cfg={
            "partition": "gpu",
            "gres": "gpu:1",
            "container": "leisaac-gr00t-n1.6",
            "rounds": 20,
        },
        run_id="run-20260414-090000",
    )
    ctx = {
        "checkpoint_dir": "/fsx/checkpoints/my-ckpt",
        "eval_dir": "/fsx/evaluations/run-20260414-090000",
    }
    sbatch = stage.generate_sbatch(ctx)
    assert "#SBATCH --job-name=physai/run/run-20260414-090000/eval" in sbatch
    assert "--container-image=/fsx/enroot/leisaac-gr00t-n1.6.sqsh" in sbatch
    assert "/fsx/checkpoints/my-ckpt" in sbatch
    assert "20\n" in sbatch
    assert "--visual" not in sbatch


def test_eval_stage_sbatch_visual():
    stage = _make_stage(
        EvalStage,
        cfg={"partition": "gpu", "gres": "gpu:1", "container": "rt", "rounds": 10},
    )
    ctx = {"checkpoint_dir": "/ckpt", "eval_dir": "/out", "visual": True}
    sbatch = stage.generate_sbatch(ctx)
    assert "10 --visual\n" in sbatch


def test_eval_stage_sbatch_constraint():
    stage = _make_stage(
        EvalStage,
        cfg={
            "partition": "gpu",
            "gres": "gpu:1",
            "constraint": "l40s",
            "container": "rt",
        },
    )
    ctx = {"checkpoint_dir": "/ckpt", "eval_dir": "/out"}
    sbatch = stage.generate_sbatch(ctx)
    assert "#SBATCH --constraint=l40s" in sbatch
