"""Tests for the VAMS handling of mlflow_train.py against a local MLflow file store.

LeRobot is only installed in the training container, so the lerobot modules that
mlflow_train.py imports are replaced with empty stubs before it is loaded.

Run from samples/lerobot-openpi/training:
    uv run --with mlflow --with pytest pytest tests
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import mlflow
import pytest

TRAINING_DIR = Path(__file__).resolve().parent.parent
PIPELINE_EXECUTION_ID = "exec-0001_a"
DATASET_DIGEST = "sha256:" + "ab" * 32
VIEW_EPISODES = [0, 2, 4]
NORM_STATS = {"action": {"mean": [0.5], "q01": [0.0], "q99": [1.0]}}


def _install_lerobot_stubs() -> None:
    """Register empty lerobot modules so mlflow_train.py can be imported without LeRobot."""
    modules = {
        "lerobot": {"__version__": "0.0.0"},
        "lerobot.scripts": {},
        "lerobot.scripts.lerobot_train": {"main": lambda: None, "WandBLogger": None},
        "lerobot.utils": {},
        "lerobot.utils.constants": {"PRETRAINED_MODEL_DIR": "pretrained_model"},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        sys.modules.setdefault(name, module)


def _load_mlflow_train() -> types.ModuleType:
    """Import training/mlflow_train.py as a module."""
    _install_lerobot_stubs()
    spec = importlib.util.spec_from_file_location("mlflow_train", TRAINING_DIR / "mlflow_train.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mlflow_train = _load_mlflow_train()


@pytest.fixture(autouse=True)
def file_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point MLflow at a file store in a temporary directory."""
    # Recent MLflow versions refuse the file store unless it is explicitly allowed.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    mlflow.set_tracking_uri(f"file:{tmp_path / 'mlruns'}")
    yield
    while mlflow.active_run():
        mlflow.end_run()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Create a VAMS run directory with input_manifest.json and vams_context.json."""
    directory = tmp_path / "vams-runs" / PIPELINE_EXECUTION_ID
    directory.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1,
        "bucket": "asset-bucket",
        "assetRootS3Key": "db/asset/",
        "versioned": True,
        "objects": [
            {
                "bucket": "asset-bucket",
                "key": "db/asset/meta/info.json",
                "versionId": "v1",
                "etag": "e1",
                "size": 10,
                "relativePath": "meta/info.json",
            }
        ],
    }
    (directory / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    context = {
        "schemaVersion": 1,
        "trigger": "vams",
        "workflowDatabaseId": "wf-db",
        "workflowId": "wf-1",
        "workflowExecutionId": "wf-exec-1",
        "pipelineExecutionId": PIPELINE_EXECUTION_ID,
        "pipelineName": "pask-training",
        "executingUserName": "alice",
        "input": {
            "databaseId": "db",
            "assetId": "asset",
            "assetName": "pick-and-place",
            "bucket": "asset-bucket",
            "assetRootS3Key": "db/asset/",
            "versioned": False,
        },
        "output": {"databaseId": "db", "assetId": "model-asset"},
        "datasetDigest": DATASET_DIGEST,
        "inputManifestFile": str(directory / "input_manifest.json"),
        "resultFile": str(directory / "mlflow_run.json"),
    }
    (directory / "vams_context.json").write_text(json.dumps(context), encoding="utf-8")
    return directory


def test_load_vams_context_reads_valid_file(run_dir: Path) -> None:
    context = mlflow_train.load_vams_context(str(run_dir / "vams_context.json"))
    assert context["pipelineExecutionId"] == PIPELINE_EXECUTION_ID


def test_load_vams_context_fails_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mlflow_train.load_vams_context(str(tmp_path / "missing.json"))


def test_load_vams_context_fails_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "vams_context.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        mlflow_train.load_vams_context(str(path))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schemaVersion", 3),
        ("schemaVersion", "1"),
        ("schemaVersion", True),
        ("trigger", "manual"),
        ("pipelineExecutionId", "id' OR '1'='1"),
        ("datasetDigest", "ab" * 32),
        ("resultFile", ""),
    ],
)
def test_load_vams_context_rejects_invalid_fields(run_dir: Path, key: str, value: object) -> None:
    path = run_dir / "vams_context.json"
    context = json.loads(path.read_text(encoding="utf-8"))
    context[key] = value
    path.write_text(json.dumps(context), encoding="utf-8")
    with pytest.raises(ValueError):
        mlflow_train.load_vams_context(str(path))


def test_build_trigger_tags_for_manual_run() -> None:
    assert mlflow_train.build_trigger_tags(None) == {"pask.trigger": "manual"}


def test_build_trigger_tags_for_vams_run(run_dir: Path) -> None:
    context = mlflow_train.load_vams_context(str(run_dir / "vams_context.json"))
    tags = mlflow_train.build_trigger_tags(context)
    assert tags["pask.trigger"] == "vams"
    assert tags["vams.pipeline_execution_id"] == PIPELINE_EXECUTION_ID
    assert tags["vams.executing_user_name"] == "alice"
    assert tags["vams.input.versioned"] == "false"
    assert tags["vams.output.asset_id"] == "model-asset"
    assert tags["vams.dataset_digest"] == DATASET_DIGEST


def test_find_vams_run_returns_run_of_same_execution() -> None:
    experiment_id = mlflow.set_experiment("vams-training").experiment_id
    with mlflow.start_run() as other_run:
        mlflow.set_tag("vams.pipeline_execution_id", "other-exec")
    assert mlflow_train.find_vams_run(experiment_id, PIPELINE_EXECUTION_ID) is None
    with mlflow.start_run() as run:
        mlflow.set_tag("vams.pipeline_execution_id", PIPELINE_EXECUTION_ID)
    found = mlflow_train.find_vams_run(experiment_id, PIPELINE_EXECUTION_ID)
    assert found.info.run_id == run.info.run_id
    assert found.info.run_id != other_run.info.run_id


def test_find_vams_run_rejects_unsafe_id() -> None:
    experiment_id = mlflow.set_experiment("vams-training").experiment_id
    with pytest.raises(ValueError):
        mlflow_train.find_vams_run(experiment_id, "x' OR tags.a = 'b")


def test_vams_run_records_lineage_and_result_file(run_dir: Path) -> None:
    context = mlflow_train.load_vams_context(str(run_dir / "vams_context.json"))
    mlflow.set_experiment("vams-training")
    with mlflow.start_run() as run:
        mlflow_train.write_mlflow_run_file(context["resultFile"], run.info)
        mlflow.set_tags(mlflow_train.build_trigger_tags(context))
        mlflow_train.log_vams_inputs(context, None)

    result = json.loads((run_dir / "mlflow_run.json").read_text(encoding="utf-8"))
    assert result == {
        "run_id": run.info.run_id,
        "experiment_id": run.info.experiment_id,
        "tracking_uri": mlflow.get_tracking_uri(),
    }
    assert [p.name for p in run_dir.iterdir() if p.name.startswith(".")] == []

    artifacts = [a.path for a in mlflow.MlflowClient().list_artifacts(run.info.run_id, "vams")]
    assert artifacts == ["vams/input_manifest.json"]

    dataset_inputs = mlflow.get_run(run.info.run_id).inputs.dataset_inputs
    assert len(dataset_inputs) == 1
    dataset = dataset_inputs[0].dataset
    assert dataset.name == "pick-and-place"
    assert dataset.digest == "ab" * 18
    assert json.loads(dataset.source) == {"uri": "s3://asset-bucket/db/asset/"}


def _write_context(run_dir: Path, context: dict) -> Path:
    """Overwrite vams_context.json in run_dir and return its path."""
    path = run_dir / "vams_context.json"
    path.write_text(json.dumps(context), encoding="utf-8")
    return path


@pytest.fixture
def view_run_dir(run_dir: Path, tmp_path: Path) -> Path:
    """Turn run_dir into the run directory of a curation view (schemaVersion 2)."""
    dataset_root = tmp_path / "view_dataset"
    (dataset_root / "meta").mkdir(parents=True)
    (dataset_root / "meta" / "stats.json").write_text(json.dumps(NORM_STATS), encoding="utf-8")
    manifest = {
        "curation_manifest_version": 2,
        "mode": "view",
        "episodes": VIEW_EPISODES,
        "excluded_episodes": [
            {"episode_index": 1, "reasons": ["blurry"]},
            {"episode_index": 3, "reasons": ["failed"]},
        ],
    }
    (run_dir / "curation_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    context = json.loads((run_dir / "vams_context.json").read_text(encoding="utf-8"))
    context["schemaVersion"] = 2
    context["input"].update(
        {
            "databaseId": "view-db",
            "assetId": "view-asset",
            "assetName": "pick-and-place-view",
            "bucket": "asset-bucket",
            "assetRootS3Key": "view-db/view-asset/",
            "kind": "view",
        }
    )
    context["view"] = {
        "manifestFile": str(run_dir / "curation_manifest.json"),
        "createdAt": "2026-10-20T00:00:00Z",
        "reviewUpdatedAt": "2026-10-21T00:00:00Z",
        "episodeCount": len(VIEW_EPISODES),
        "excludedEpisodeCount": 2,
        "source": {
            "databaseId": "src-db",
            "assetId": "src-asset",
            "assetVersion": "1",
            "bucket": "source-bucket",
            "assetRootS3Key": "src-db/src-asset/",
        },
    }
    _write_context(run_dir, context)
    return run_dir


def _load_view_context(view_run_dir: Path) -> dict:
    """Load the view vams_context.json written by the view_run_dir fixture."""
    return mlflow_train.load_vams_context(str(view_run_dir / "vams_context.json"))


def test_load_vams_context_accepts_view(view_run_dir: Path) -> None:
    context = _load_view_context(view_run_dir)
    assert mlflow_train.is_view_context(context)


def test_load_vams_context_accepts_view_without_optional_fields(view_run_dir: Path) -> None:
    context = json.loads((view_run_dir / "vams_context.json").read_text(encoding="utf-8"))
    del context["view"]["createdAt"]
    del context["view"]["reviewUpdatedAt"]
    del context["view"]["source"]["assetVersion"]
    path = _write_context(view_run_dir, context)
    assert mlflow_train.is_view_context(mlflow_train.load_vams_context(str(path)))


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("input", "kind", "dataset"),
        ("view", "manifestFile", ""),
        ("view", "episodeCount", -1),
        ("view", "episodeCount", "3"),
        ("view", "episodeCount", True),
        ("view", "excludedEpisodeCount", None),
        ("view", "source", "src-asset"),
        ("source", "databaseId", ""),
        ("source", "assetId", None),
        ("source", "bucket", 1),
        ("source", "assetRootS3Key", ""),
    ],
)
def test_load_vams_context_rejects_invalid_view(
    view_run_dir: Path, section: str, key: str, value: object
) -> None:
    context = json.loads((view_run_dir / "vams_context.json").read_text(encoding="utf-8"))
    target = context["view"]["source"] if section == "source" else context[section]
    target[key] = value
    path = _write_context(view_run_dir, context)
    with pytest.raises(ValueError):
        mlflow_train.load_vams_context(str(path))


def test_load_vams_context_rejects_schema_version_2_without_view(view_run_dir: Path) -> None:
    context = json.loads((view_run_dir / "vams_context.json").read_text(encoding="utf-8"))
    del context["view"]
    path = _write_context(view_run_dir, context)
    with pytest.raises(ValueError):
        mlflow_train.load_vams_context(str(path))


def test_load_vams_context_rejects_view_kind_in_schema_version_1(view_run_dir: Path) -> None:
    context = json.loads((view_run_dir / "vams_context.json").read_text(encoding="utf-8"))
    context["schemaVersion"] = 1
    path = _write_context(view_run_dir, context)
    with pytest.raises(ValueError):
        mlflow_train.load_vams_context(str(path))


def test_apply_view_episodes_appends_episodes(view_run_dir: Path) -> None:
    context = _load_view_context(view_run_dir)
    argv = ["mlflow_train.py", "--dataset.repo_id=local/view"]
    result = mlflow_train.apply_view_episodes(context, argv)
    assert result == [*argv, "--dataset.episodes=[0,2,4]"]
    assert argv == ["mlflow_train.py", "--dataset.repo_id=local/view"]


def test_apply_view_episodes_keeps_argv_for_dataset(run_dir: Path) -> None:
    context = mlflow_train.load_vams_context(str(run_dir / "vams_context.json"))
    argv = ["mlflow_train.py", "--dataset.repo_id=local/data"]
    assert mlflow_train.apply_view_episodes(context, argv) == argv


@pytest.mark.parametrize("existing", ["--dataset.episodes=[1]", "--dataset.episodes"])
def test_apply_view_episodes_rejects_existing_episodes(view_run_dir: Path, existing: str) -> None:
    context = _load_view_context(view_run_dir)
    with pytest.raises(ValueError):
        mlflow_train.apply_view_episodes(context, ["mlflow_train.py", existing, "[1]"])


@pytest.mark.parametrize(
    "episodes",
    [[], [2, 0, 4], [0, 0, 4], [-1, 2, 4], [0, True, 4], [0, "2", 4], [0, 2.0, 4], "0,2,4", [0, 2]],
)
def test_apply_view_episodes_rejects_invalid_episodes(view_run_dir: Path, episodes: object) -> None:
    context = _load_view_context(view_run_dir)
    manifest_path = Path(context["view"]["manifestFile"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["episodes"] = episodes
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        mlflow_train.apply_view_episodes(context, ["mlflow_train.py"])


def test_build_trigger_tags_for_dataset_has_no_view_tags(run_dir: Path) -> None:
    context = mlflow_train.load_vams_context(str(run_dir / "vams_context.json"))
    tags = mlflow_train.build_trigger_tags(context)
    assert tags["vams.input.kind"] == "dataset"
    assert not [name for name in tags if name.startswith(("vams.source.", "vams.view."))]


def test_build_trigger_tags_for_view(view_run_dir: Path) -> None:
    tags = mlflow_train.build_trigger_tags(_load_view_context(view_run_dir))
    assert tags["vams.input.kind"] == "view"
    assert tags["vams.input.asset_id"] == "view-asset"
    assert tags["vams.source.database_id"] == "src-db"
    assert tags["vams.source.asset_id"] == "src-asset"
    assert tags["vams.source.asset_version"] == "1"
    assert tags["vams.view.episode_count"] == "3"
    assert tags["vams.view.excluded_episode_count"] == "2"
    assert tags["vams.view.review_updated_at"] == "2026-10-21T00:00:00Z"


def test_view_run_drops_null_asset_version_tag(view_run_dir: Path) -> None:
    context = json.loads((view_run_dir / "vams_context.json").read_text(encoding="utf-8"))
    context["view"]["source"]["assetVersion"] = None
    del context["view"]["reviewUpdatedAt"]
    path = _write_context(view_run_dir, context)
    tags = mlflow_train.build_trigger_tags(mlflow_train.load_vams_context(str(path)))
    mlflow.set_experiment("vams-training")
    with mlflow.start_run() as run:
        mlflow.set_tags({name: value for name, value in tags.items() if value})
    logged_tags = mlflow.get_run(run.info.run_id).data.tags
    assert "vams.source.asset_version" not in logged_tags
    assert "vams.view.review_updated_at" not in logged_tags
    assert logged_tags["vams.source.asset_id"] == "src-asset"


def test_view_run_records_view_artifacts_and_source_dataset(
    view_run_dir: Path, tmp_path: Path
) -> None:
    context = _load_view_context(view_run_dir)
    mlflow.set_experiment("vams-training")
    with mlflow.start_run() as run:
        mlflow_train.log_vams_inputs(context, str(tmp_path / "view_dataset"))

    client = mlflow.MlflowClient()
    artifacts = sorted(a.path for a in client.list_artifacts(run.info.run_id, "vams"))
    assert artifacts == [
        "vams/curation_manifest.json",
        "vams/input_manifest.json",
        "vams/norm_stats.json",
    ]
    norm_stats_path = client.download_artifacts(run.info.run_id, "vams/norm_stats.json", str(tmp_path))
    assert json.loads(Path(norm_stats_path).read_text(encoding="utf-8")) == NORM_STATS

    dataset_inputs = mlflow.get_run(run.info.run_id).inputs.dataset_inputs
    assert len(dataset_inputs) == 1
    dataset = dataset_inputs[0].dataset
    assert dataset.name == "pick-and-place-view"
    assert json.loads(dataset.source) == {"uri": "s3://source-bucket/src-db/src-asset/"}


def test_view_run_requires_dataset_root(view_run_dir: Path) -> None:
    context = _load_view_context(view_run_dir)
    mlflow.set_experiment("vams-training")
    with mlflow.start_run(), pytest.raises(ValueError):
        mlflow_train.log_vams_inputs(context, None)
