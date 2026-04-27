"""Tests for physai pipeline (run, train, eval)."""

from pathlib import Path, PurePosixPath

import pytest

from physai.pipeline import (
    Artifact,
    ConvertStage,
    Dir,
    EvalStage,
    File,
    JobMetadata,
    TrainStage,
    _format_produces,
    _get_stage_config,
    _load_run_config,
    _resolve_model_config,
    _resolve_stages,
    _sbatch_header,
    _validate_artifact_name,
)

# ── _load_run_config ──


def test_load_run_config(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text(
        "pipeline:\n  stages: [train]\nmodel:\n  name: gr00t\n  config_dir: gr00t/so101\nstages:\n  train:\n    container: tr\n"
    )
    cfg = _load_run_config(cfg_file)
    assert cfg["model"]["config_dir"] == "gr00t/so101"
    assert cfg["stages"]["train"]["container"] == "tr"


def test_load_run_config_missing_file():
    with pytest.raises(SystemExit, match="not found"):
        _load_run_config(Path("/nonexistent.yaml"))


def test_load_run_config_missing_model(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text(
        "pipeline:\n  stages: [train]\nstages:\n  train:\n    container: tr\n"
    )
    with pytest.raises(SystemExit, match="model"):
        _load_run_config(cfg_file)


def test_load_run_config_missing_stages(tmp_path):
    cfg_file = tmp_path / "run.yaml"
    cfg_file.write_text(
        "pipeline:\n  stages: [train]\nmodel:\n  name: gr00t\n  config_dir: gr00t/so101\n"
    )
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


# ── ConvertStage ──


def test_convert_stage_validate_ok():
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    stage.validate({"raw_dir": "/fsx/raw/my-demos"})  # no error


def test_convert_stage_validate_missing():
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    with pytest.raises(SystemExit, match="--raw"):
        stage.validate({})


def test_convert_stage_sbatch():
    stage = _make_stage(
        ConvertStage,
        cfg={"partition": "cpu", "container": "so101-converter"},
        run_id="run-20260415-100000",
    )
    ctx = {
        "raw_dir": Dir(PurePosixPath("/fsx/raw/leisaac-pick-orange")),
        "dataset_dir": Dir(PurePosixPath("/fsx/datasets/leisaac-pick-orange")),
    }
    sbatch = stage.generate_sbatch(ctx)
    assert "#SBATCH --job-name=physai/run/run-20260415-100000/convert" in sbatch
    assert "#SBATCH --partition=cpu" in sbatch
    assert "--gres" not in sbatch  # no GPU for CPU converter
    assert "--container-image=/fsx/enroot/so101-converter.sqsh" in sbatch
    assert "bash /app/convert.sh" in sbatch
    assert "/fsx/raw/leisaac-pick-orange" in sbatch
    assert "/fsx/datasets/leisaac-pick-orange" in sbatch


# ── inputs()/outputs() API ──


def test_convert_stage_declares_inputs_outputs():
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    ctx = {"raw_dir": Dir(PurePosixPath("/fsx/raw/my-demos"))}
    assert stage.inputs(ctx) == [Dir(PurePosixPath("/fsx/raw/my-demos"))]
    assert stage.outputs(ctx) == [Dir(PurePosixPath("/fsx/datasets/my-demos"))]


def test_train_stage_declares_inputs_outputs():
    stage = _make_stage(TrainStage, run_id="run-20260415-100000")
    ctx = {"dataset_dir": Dir(PurePosixPath("/fsx/datasets/my-ds"))}
    assert stage.inputs(ctx) == [Dir(PurePosixPath("/fsx/datasets/my-ds"))]
    assert stage.outputs(ctx) == [
        Dir(PurePosixPath("/fsx/checkpoints/run-20260415-100000"))
    ]


def test_eval_stage_declares_inputs_outputs():
    stage = _make_stage(
        EvalStage,
        cfg={"partition": "gpu", "gres": "gpu:1", "container": "rt"},
        run_id="run-20260415-100000",
    )
    ctx = {"checkpoint_dir": Dir(PurePosixPath("/fsx/checkpoints/ckpt"))}
    assert stage.inputs(ctx) == [Dir(PurePosixPath("/fsx/checkpoints/ckpt"))]
    assert stage.outputs(ctx) == [
        Dir(PurePosixPath("/fsx/evaluations/run-20260415-100000"))
    ]


# ── produces= serialization round-trip ──


def _extract_comment(sbatch: str) -> str:
    for line in sbatch.splitlines():
        if line.startswith("#SBATCH --comment="):
            return line.split("=", 1)[1].strip('"')
    raise AssertionError("no --comment line in sbatch")


def _round_trip(outputs: list) -> list[bool]:
    """Build an sbatch header for `outputs`, then parse tokens back and check
    each original artifact is recoverable as a produces=<path> token."""
    sbatch = _sbatch_header(JobMetadata(name="physai/run/x/y", outputs=outputs))
    tokens = _extract_comment(sbatch).split()
    return [_format_produces(p) in tokens for p in outputs]


def test_produces_round_trip_directory():
    artifacts = [Dir(PurePosixPath("/fsx/datasets/my-demos"))]
    assert _round_trip(artifacts) == [True]


def test_produces_round_trip_file():
    artifacts = [File(PurePosixPath("/fsx/results/run-42/metrics.json"))]
    assert _round_trip(artifacts) == [True]


def test_produces_round_trip_mixed_file_and_directory():
    artifacts = [
        Dir(PurePosixPath("/fsx/datasets/my-demos")),
        Dir(PurePosixPath("/fsx/checkpoints/run-1")),
        File(PurePosixPath("/fsx/results/run-1/metrics.json")),
    ]
    assert _round_trip(artifacts) == [True, True, True]


def test_produces_distinguishes_file_and_dir_at_same_path():
    """File(/x) and Dir(/x) must serialize to distinct tokens."""
    shared = PurePosixPath("/fsx/shared/name")
    assert _format_produces(File(shared)) != _format_produces(Dir(shared))
    # And exact-token match must discriminate them
    file_token = _format_produces(File(shared))
    dir_token = _format_produces(Dir(shared))
    assert file_token not in dir_token.split()
    assert dir_token not in file_token.split()


def test_produces_no_prefix_collision():
    """A path that's a strict prefix of another must not match it as a token."""
    long_path = Dir(PurePosixPath("/fsx/checkpoints/run-10"))
    short_path = Dir(PurePosixPath("/fsx/checkpoints/run-1"))
    comment = _format_produces(long_path)
    # exact-token match (comment.split()) must NOT find the short path
    assert _format_produces(short_path) not in comment.split()


def test_produces_rejects_whitespace_in_path():
    with pytest.raises(SystemExit, match="whitespace"):
        _format_produces(Dir(PurePosixPath("/fsx/datasets/bad name")))


def test_artifact_from_token_inverse_of_as_token():
    """from_token(as_token(x)) == x for both File and Dir."""
    cases = [
        File(PurePosixPath("/fsx/results/run-1/metrics.json")),
        Dir(PurePosixPath("/fsx/datasets/my-demos")),
        Dir(PurePosixPath("/fsx/checkpoints/run-10")),
    ]
    for original in cases:
        assert Artifact.from_token(original.as_token()) == original


# ── ConvertStage dataset-name override ──


def test_convert_outputs_default_to_raw_name():
    """Without ctx['dataset_dir'], convert derives output name from --raw."""
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    ctx = {"raw_dir": Dir(PurePosixPath("/fsx/raw/my-demos"))}
    assert stage.outputs(ctx) == [Dir(PurePosixPath("/fsx/datasets/my-demos"))]


def test_convert_outputs_honor_dataset_override():
    """When ctx['dataset_dir'] is set (user passed --dataset), convert uses it."""
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    ctx = {
        "raw_dir": Dir(PurePosixPath("/fsx/raw/ugly-raw-name")),
        "dataset_dir": Dir(PurePosixPath("/fsx/datasets/clean-name")),
    }
    assert stage.outputs(ctx) == [Dir(PurePosixPath("/fsx/datasets/clean-name"))]


def test_convert_prepare_does_not_clobber_existing_dataset_dir():
    """prepare() must leave ctx['dataset_dir'] alone if the user set it."""
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    user_choice = Dir(PurePosixPath("/fsx/datasets/user-choice"))
    ctx = {
        "raw_dir": Dir(PurePosixPath("/fsx/raw/ugly-raw-name")),
        "dataset_dir": user_choice,
    }
    stage.prepare(ctx)
    assert ctx["dataset_dir"] == user_choice


def test_convert_prepare_populates_when_absent():
    """prepare() fills in dataset_dir when the user didn't pass --dataset."""
    stage = _make_stage(
        ConvertStage, cfg={"partition": "cpu", "container": "so101-converter"}
    )
    ctx = {"raw_dir": Dir(PurePosixPath("/fsx/raw/my-demos"))}
    stage.prepare(ctx)
    assert ctx["dataset_dir"] == Dir(PurePosixPath("/fsx/datasets/my-demos"))


# ── _validate_artifact_name ──


def test_validate_artifact_name_ok():
    _validate_artifact_name("clean-name", "dataset")  # no exception
    _validate_artifact_name("run-20260429-154500", "checkpoint")


def test_validate_artifact_name_rejects_slash():
    with pytest.raises(SystemExit, match="Invalid dataset name"):
        _validate_artifact_name("a/b", "dataset")


def test_validate_artifact_name_rejects_dot():
    with pytest.raises(SystemExit, match="Invalid raw data name"):
        _validate_artifact_name("..", "raw data")


def test_validate_artifact_name_rejects_whitespace():
    with pytest.raises(SystemExit, match="whitespace"):
        _validate_artifact_name("bad name", "dataset")


def test_validate_artifact_name_rejects_empty():
    with pytest.raises(SystemExit, match="Invalid dataset name"):
        _validate_artifact_name("", "dataset")
