"""Layer 2 — shipped-example regression checks.

These run real example pipelines end-to-end against a small raw-data
fixture supplied via ``--raw-source <URI>`` (file:// / s3:// / hf://).

The ``examples/so101-gr00t/`` example ships two GR00T variants — N1.5 and
N1.6 — that share the ``so101-converter`` and ``leisaac-runtime``
containers but differ in their trainer and eval containers, configs, and
model-config dirs. The check is parametrized over both so a regression in
either lineage is caught; the parametrize ids are the version strings
(``n1.5`` / ``n1.6``), so ``-k n1.5`` runs just that variant.

For each variant:

1. The shared ``<variant>_raw_staged`` fixture stages the URI into a
   per-run directory ``/fsx/raw/<base>-<YYYYMMDD-HHMMSS>/`` on the cluster
   (`raw_staging.stage_raw`). The cluster wipes the destination first
   so a stale partial fixture from a previous failed run can't mask a
   real change. The dest name is timestamped so concurrent regression
   runs don't trample each other.
2. The check rebuilds every container the variant needs (so a stale
   ``.sqsh`` from a prior run can't mask a broken Dockerfile, setup-hook,
   or container spec). The shared base containers are built once per
   session; the per-variant trainer/eval containers are built per variant.
3. ``physai run --max-steps 100`` runs against the staged raw; the
   suite asserts convert/train/eval all reach COMPLETED and that
   ``metrics.json`` exists in the eval run dir.
4. On test pass the staged raw is rm'd. On test fail it's kept so the
   operator can inspect what actually got downloaded.

Wall time: ~100 min per variant on a healthy cluster (~40 min builds plus
~57 min eval as the long pole). The shared base-container builds happen
once, so the two variants together run in roughly 150–180 min rather than
2×100. Cost: ~$2 / variant / run. Add 1–10 min for staging depending on
URI scheme and fixture size; staging output is silent.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from physai_regression.raw_staging import stage_raw

# Output parsing is shared via the _parsing module; the sacct/squeue polling
# helpers stay in test_pipeline (they take a live session and are
# pipeline-specific).
from physai_regression.checks._parsing import _extract_run_id
from physai_regression.checks.test_pipeline import (
    _wait_for_run_jobs,
    _wait_for_stage_states,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SO101_GR00T_DIR = REPO_ROOT / "examples" / "so101-gr00t"
# Shared by every GR00T variant: the converter (raw → dataset) and the
# leisaac runtime that the per-variant eval containers extend via
# ``base_container``. Built once per session; ``leisaac-runtime`` must come
# before any ``leisaac-gr00t-*`` that extends it.
SO101_SHARED_CONTAINERS = ("so101-converter", "leisaac-runtime")
# ``--model-config-root`` is the same dir for both variants; the variant's
# config file names the version-specific subdir under it.
SO101_MODEL_CONFIG_ROOT = SO101_GR00T_DIR / "model_configs"

PIPELINE_TIMEOUT_S = 60 * 90  # 90 min: train+eval at 100 steps + queue slack


@dataclass(frozen=True)
class LiftCubeCase:
    """One GR00T-version variant of the SO101 LiftCube example.

    ``version`` is the parametrize id and the token that appears verbatim
    in the config filename and the version-specific container names
    (``so101_liftcube_gr00t-<version>.yaml``, ``gr00t-<version>-trainer``,
    ``leisaac-gr00t-<version>``).
    """

    version: str
    config: Path
    trainer: str
    eval_container: str
    raw_base: str
    dataset: str


def _liftcube_case(version: str) -> LiftCubeCase:
    return LiftCubeCase(
        version=version,
        config=SO101_GR00T_DIR / "configs" / f"so101_liftcube_gr00t-{version}.yaml",
        trainer=f"gr00t-{version}-trainer",
        eval_container=f"leisaac-gr00t-{version}",
        raw_base=f"regression-builtin-liftcube-{version}",
        dataset=f"regression-builtin-liftcube-{version}",
    )


LIFTCUBE_CASES = [_liftcube_case("n1.5"), _liftcube_case("n1.6")]


@pytest.fixture(params=LIFTCUBE_CASES, ids=[c.version for c in LIFTCUBE_CASES])
def liftcube_case(request: pytest.FixtureRequest) -> LiftCubeCase:
    """The GR00T variant under test for this parametrized invocation."""
    return request.param


@pytest.fixture(scope="session")
def liftcube_containers_built(physai_cli) -> None:
    """Rebuild every container the LiftCube variants need, shared ones once.

    ``--rebuild`` is mandatory here: a stale ``.sqsh`` left over from a
    prior run would let the pipeline succeed even if a Dockerfile,
    setup-hook, or ``container.yaml`` were broken — exactly the regression
    Layer 2 exists to catch. Order matters: ``leisaac-gr00t-<version>``
    extends ``leisaac-runtime`` via ``base_container``, so the shared
    runtime (built first, below) must be on disk before them.

    Session-scoped and version-agnostic so the shared ``so101-converter``
    and ``leisaac-runtime`` are built once even though the check is
    parametrized over both variants.
    """
    names = list(SO101_SHARED_CONTAINERS)
    for case in LIFTCUBE_CASES:
        names += [case.trainer, case.eval_container]
    for name in names:
        physai_cli.run(
            "build",
            str(SO101_GR00T_DIR / "containers" / name),
            "--rebuild",
        )


def _raw_present(session, name: str) -> bool:
    out = session.run(f"test -d /fsx/raw/{name} && echo y || echo n").strip()
    return out == "y"


@pytest.fixture
def liftcube_raw_staged(
    liftcube_case: LiftCubeCase,
    physai_session,
    raw_source_uri: str | None,
    aws_profile: str | None,
    aws_region: str | None,
) -> str:
    """Stage the LiftCube raw fixture into a per-run ``/fsx/raw/<name>/``.

    Returns the staged dest name so the test body can use it as the
    ``--raw`` arg to ``physai run`` and clean it up on success. The raw
    fixture is staged per variant (each into its own timestamped dir) so
    the two parametrized runs stay fully independent.

    A missing ``--raw-source`` is rejected earlier than this fixture: the
    runner rejects it at argparse time, and ``pytest_collection_modifyitems``
    rejects it at collection time for a direct ``pytest`` invocation. The
    guard here is a last-resort assert so the fixture never silently
    proceeds with ``None``.
    """
    assert raw_source_uri is not None, (
        "raw_source_uri unset — pytest_collection_modifyitems should have "
        "rejected this run before any test executed"
    )
    name = f"{liftcube_case.raw_base}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    stage_raw(
        physai_session,
        raw_source_uri,
        name,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )
    if not _raw_present(physai_session, name):
        pytest.fail(
            f"raw staging completed but /fsx/raw/{name} is not present "
            f"(source: {raw_source_uri})"
        )
    return name


@pytest.mark.builtin_example
def test_so101_gr00t_liftcube(
    physai_session,
    physai_cli,
    liftcube_case: LiftCubeCase,
    liftcube_raw_staged: str,
    liftcube_containers_built,
) -> None:
    """``physai run`` of a LiftCube GR00T variant to convert→train→eval COMPLETED.

    Parametrized over the N1.5 and N1.6 variants. ``--max-steps 100`` caps
    training so the suite finishes in ~40 min rather than the multi-hour
    default. Success-rate is not asserted — 100 training steps will not
    solve LiftCube — only that the run reached COMPLETED on every stage and
    that the eval container wrote ``metrics.json`` to its run directory.
    """
    raw_name = liftcube_raw_staged
    dataset_name = liftcube_case.dataset
    dataset_dir = f"/fsx/datasets/{dataset_name}"
    physai_session.run(f"rm -rf {dataset_dir}")

    run_id = ""
    succeeded = False
    try:
        r = physai_cli.run(
            "run",
            "--config",
            str(liftcube_case.config),
            "--raw",
            raw_name,
            "--dataset",
            dataset_name,
            "--model-config-root",
            str(SO101_MODEL_CONFIG_ROOT),
            "--max-steps",
            "100",
            "-n",
            timeout=600,
        )
        run_id = _extract_run_id(r.stdout)
        # First wait (up to the full pipeline budget) for the jobs to leave
        # the queue, then settle the brief squeue→sacct accounting lag so the
        # COMPLETED assertions below don't race a transient COMPLETING state.
        _wait_for_run_jobs(physai_session, run_id, timeout=PIPELINE_TIMEOUT_S)
        states = _wait_for_stage_states(
            physai_session, run_id, ("convert", "train", "eval")
        )

        for stage in ("convert", "train", "eval"):
            matches = [s for n, s in states.items() if n.endswith(f"/{stage}")]
            assert matches == ["COMPLETED"], (
                f"{stage} stage did not complete cleanly: {states}"
            )

        eval_dir = f"/fsx/evaluations/{run_id}"
        metrics_raw = physai_session.run(f"cat {eval_dir}/metrics.json")
        json.loads(metrics_raw)
        succeeded = True
    finally:
        physai_session.run(f"rm -rf {dataset_dir}")
        if run_id:
            physai_session.run(
                f"rm -rf /fsx/checkpoints/{run_id}* /fsx/evaluations/{run_id}*"
            )
        # Only rm the staged raw on success. On failure, leave it on
        # /fsx/raw/ so the operator can inspect what was actually
        # downloaded; the next run picks a new timestamped name and
        # won't conflict.
        if succeeded:
            physai_session.run(f"rm -rf /fsx/raw/{raw_name}")
