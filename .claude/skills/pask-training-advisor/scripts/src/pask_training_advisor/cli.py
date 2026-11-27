"""Command line entry point of the training advisor.

Every subcommand prints exactly one JSON object to stdout. The exit code is 0 on success, 1 when
``validate-proposal`` finds violations, and 2 for expected errors such as a missing file or a run
that cannot be fetched. Stack traces of expected errors go to stderr. AWS credentials come from
the process environment; this module never sets ``AWS_PROFILE``.
"""

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from mlflow.exceptions import MlflowException

from pask_training_advisor.constraints import load_operational_constraints, load_template_schema
from pask_training_advisor.datasets import diagnose_datasets, load_dataset_thresholds
from pask_training_advisor.detect import detect_environment
from pask_training_advisor.diagnose import diagnose_snapshot, load_thresholds
from pask_training_advisor.fetch import (
    TRACKING_URI_ENV,
    FetchError,
    create_client,
    fetch_snapshot,
)
from pask_training_advisor.models import (
    DATASETS_FILE,
    DEFAULT_EXPERIMENTS,
    DEFAULT_MAX_COMPARE_RUNS,
    DIAGNOSIS_FILE,
    PROPOSAL_FILE,
    SNAPSHOT_FILE,
    VAMS_PARAMETERS_FILE,
    SchemaError,
    default_output_dir,
    load_proposal,
    load_snapshot,
    read_json,
    save_diagnosis,
    save_snapshot,
    write_json,
)
from pask_training_advisor.plots import plot_all
from pask_training_advisor.proposal import (
    to_vams_parameters,
    validate_proposal,
    write_vams_parameters,
)
from pask_training_advisor.publish import PublishError, publish_artifacts
from pask_training_advisor.vams import (
    VamsError,
    fetch_vams_datasets,
    resolve_profile,
    subprocess_runner,
)

PROG = "pask-training-advisor"
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2
SCHEMA_RULE = "schema"


class CliError(Exception):
    """Raised when the command line arguments or input files cannot be used."""


EXPECTED_ERRORS: tuple[type[BaseException], ...] = (
    CliError,
    FetchError,
    PublishError,
    SchemaError,
    VamsError,
    json.JSONDecodeError,
    OSError,
    MlflowException,
)

CommandResult = tuple[int, dict[str, Any]]


def _print_json(data: dict[str, Any]) -> None:
    """Print one JSON object to stdout.

    Args:
        data: JSON object to print.
    """
    print(json.dumps(data, ensure_ascii=False))


def _redact_tracking_uri(message: str) -> str:
    """Replace the tracking URI in a message so that the App ARN is not printed.

    Args:
        message: Message that may contain the value of ``MLFLOW_TRACKING_URI``.

    Returns:
        The message with the tracking URI replaced by the variable name.
    """
    uri = os.environ.get(TRACKING_URI_ENV, "")
    if not uri:
        return message
    return message.replace(uri, f"<{TRACKING_URI_ENV}>")


def _require_file(path: Path, description: str) -> Path:
    """Check that an input file exists.

    Args:
        path: File to check.
        description: Name of the file used in the error message.

    Returns:
        The same path.

    Raises:
        CliError: If the file does not exist.
    """
    if not path.is_file():
        raise CliError(f"{description} not found: {path}")
    return path


def _resolve_input(path: Path | None, output_dir: Path, file_name: str) -> Path:
    """Return an input path given on the command line, or the file in the output directory.

    Args:
        path: Path given on the command line, or None.
        output_dir: Output directory of the session.
        file_name: File name used when ``path`` is None.

    Returns:
        The path to read.
    """
    return output_dir / file_name if path is None else path


def run_fetch(args: argparse.Namespace) -> CommandResult:
    """Fetch the target run and its comparison runs and write ``snapshot.json``.

    Args:
        args: Parsed arguments of ``fetch``.

    Returns:
        The exit code and the JSON result. The tracking URI is left out of the result.
    """
    output_dir = default_output_dir() if args.output_dir is None else args.output_dir
    experiments = DEFAULT_EXPERIMENTS if args.experiment is None else tuple(args.experiment)
    snapshot = fetch_snapshot(
        create_client(),
        run_id=args.run_id,
        pipeline_execution_id=args.pipeline_execution_id,
        experiments=experiments,
        max_compare_runs=args.max_compare_runs,
    )
    snapshot_path = output_dir / SNAPSHOT_FILE
    save_snapshot(snapshot, snapshot_path)
    return EXIT_OK, {
        "ok": True,
        "outputDir": str(output_dir),
        "snapshot": str(snapshot_path),
        "targetRunId": snapshot.target.run_id,
        "comparisonRunIds": [run.run_id for run in snapshot.comparisons],
    }


def run_detect(args: argparse.Namespace) -> CommandResult:
    """Check whether MLflow and VAMS can be read and whether a report can be written.

    Args:
        args: Parsed arguments of ``detect``.

    Returns:
        Exit code 0 and the result of each check. ``canReport`` is false without MLflow.
    """
    result = detect_environment(
        create_client, subprocess_runner, resolve_profile(args.vams_profile)
    )
    return EXIT_OK, {"ok": True, **result}


def run_fetch_vams(args: argparse.Namespace) -> CommandResult:
    """Read the VAMS datasets the runs of ``snapshot.json`` used and write ``datasets.json``.

    Args:
        args: Parsed arguments of ``fetch-vams``.

    Returns:
        The exit code and the JSON result with the assets that were read.
    """
    snapshot_path = _resolve_input(args.snapshot, args.output_dir, SNAPSHOT_FILE)
    snapshot = load_snapshot(_require_file(snapshot_path, SNAPSHOT_FILE))
    profile = resolve_profile(args.vams_profile)
    datasets = fetch_vams_datasets(snapshot, create_client(), subprocess_runner, profile)
    datasets_path = args.output_dir / DATASETS_FILE
    write_json(datasets_path, datasets)
    return EXIT_OK, {
        "ok": True,
        "datasets": str(datasets_path),
        "vamsProfile": profile,
        "assets": [
            {"assetId": asset["assetId"], "assetName": asset["assetName"], "roles": asset["roles"]}
            for asset in datasets["assets"]
        ],
        "runsUsingAssets": len(datasets["runsUsingAssets"]),
    }


def _datasets_path(args: argparse.Namespace) -> Path | None:
    """Decide which ``datasets.json`` ``analyze`` reads.

    Args:
        args: Parsed arguments of ``analyze``.

    Returns:
        ``--datasets`` when given (it must exist), else ``<output-dir>/datasets.json`` when it
        exists, else None.
    """
    if args.datasets is not None:
        return _require_file(args.datasets, DATASETS_FILE)
    default_path = args.output_dir / DATASETS_FILE
    return default_path if default_path.is_file() else None


def run_analyze(args: argparse.Namespace) -> CommandResult:
    """Diagnose the runs of ``snapshot.json`` and write ``diagnosis.json``.

    When ``datasets.json`` is available, the diagnosis also gets the ``datasets`` section.

    Args:
        args: Parsed arguments of ``analyze``.

    Returns:
        The exit code and the JSON result with the labels of each run and the exclusion
        candidates.
    """
    snapshot_path = _resolve_input(args.snapshot, args.output_dir, SNAPSHOT_FILE)
    snapshot = load_snapshot(_require_file(snapshot_path, SNAPSHOT_FILE))
    if args.thresholds is not None:
        _require_file(args.thresholds, "thresholds file")
    if args.dataset_thresholds is not None:
        _require_file(args.dataset_thresholds, "dataset thresholds file")
    diagnosis = diagnose_snapshot(snapshot, load_thresholds(args.thresholds))
    datasets_path = _datasets_path(args)
    if datasets_path is not None:
        datasets = diagnose_datasets(
            read_json(datasets_path), load_dataset_thresholds(args.dataset_thresholds)
        )
        if datasets["targetRunId"] != snapshot.target.run_id:
            raise CliError(
                f"{DATASETS_FILE} was fetched for run {datasets['targetRunId']}, "
                f"not for the target run {snapshot.target.run_id}"
            )
        diagnosis = replace(diagnosis, datasets=datasets)
    diagnosis_path = args.output_dir / DIAGNOSIS_FILE
    save_diagnosis(diagnosis, diagnosis_path)
    result: dict[str, Any] = {
        "ok": True,
        "diagnosis": str(diagnosis_path),
        "targetRunId": diagnosis.target.run_id,
        "labels": list(diagnosis.target.labels),
        "comparisons": [
            {"runId": run.run_id, "labels": list(run.labels)} for run in diagnosis.comparisons
        ],
        "datasets": None if datasets_path is None else str(datasets_path),
    }
    if diagnosis.datasets is not None:
        result["datasetExclusions"] = len(diagnosis.datasets["datasetExclusions"])
        result["episodeExclusions"] = len(diagnosis.datasets["episodeExclusions"])
    return EXIT_OK, result


def _schema_violation_result(message: str) -> CommandResult:
    """Build the result of a ``proposal.json`` that cannot be read as a proposal.

    Args:
        message: Explanation of the problem.

    Returns:
        Exit code 1 and a result with one ``schema`` violation.
    """
    return EXIT_VIOLATIONS, {
        "ok": False,
        "violations": [{"key": None, "rule": SCHEMA_RULE, "message": message}],
        "warnings": [],
    }


def run_validate_proposal(args: argparse.Namespace) -> CommandResult:
    """Validate ``proposal.json`` and write ``vams_parameters.json`` when it passes.

    ``vams_parameters.json`` is removed first, so it only exists after a successful validation.

    Args:
        args: Parsed arguments of ``validate-proposal``.

    Returns:
        The exit code and the JSON result. Exit code 1 means the proposal broke a rule.

    Raises:
        CliError: If ``--pipeline-id`` is empty or an input file is missing.
    """
    vams_parameters_path = args.output_dir / VAMS_PARAMETERS_FILE
    vams_parameters_path.unlink(missing_ok=True)
    if args.pipeline_id == "":
        raise CliError("--pipeline-id must not be empty")
    snapshot_path = _resolve_input(args.snapshot, args.output_dir, SNAPSHOT_FILE)
    snapshot = load_snapshot(_require_file(snapshot_path, SNAPSHOT_FILE))
    if args.constraints is not None:
        _require_file(args.constraints, "constraints file")
    if args.template is not None:
        _require_file(args.template, "template file")
    constraints = load_operational_constraints(args.constraints)
    schema = load_template_schema(args.template)
    proposal_path = _resolve_input(args.proposal, args.output_dir, PROPOSAL_FILE)
    _require_file(proposal_path, PROPOSAL_FILE)
    try:
        proposal = load_proposal(proposal_path)
    except json.JSONDecodeError as error:
        return _schema_violation_result(f"{proposal_path.name} is not valid JSON: {error}")
    except SchemaError as error:
        return _schema_violation_result(str(error))
    result = validate_proposal(proposal, snapshot, schema, constraints)
    if not result.ok:
        return EXIT_VIOLATIONS, result.to_dict()
    vams_parameters = to_vams_parameters(
        result.normalized_parameters, template_id=schema.template_id, pipeline_id=args.pipeline_id
    )
    write_vams_parameters(vams_parameters_path, vams_parameters)
    return EXIT_OK, {
        "ok": True,
        "vamsParameters": str(vams_parameters_path),
        "warnings": list(result.warnings),
    }


def run_plot(args: argparse.Namespace) -> CommandResult:
    """Draw the learning curve figures of ``snapshot.json``.

    Args:
        args: Parsed arguments of ``plot``.

    Returns:
        The exit code and the JSON result with the written PNG files.
    """
    snapshot_path = _resolve_input(args.snapshot, args.output_dir, SNAPSHOT_FILE)
    snapshot = load_snapshot(_require_file(snapshot_path, SNAPSHOT_FILE))
    figures = plot_all(snapshot, args.output_dir)
    return EXIT_OK, {"ok": True, "figures": [str(path) for path in figures]}


def run_publish(args: argparse.Namespace) -> CommandResult:
    """Upload the session outputs to the target run as MLflow artifacts.

    Args:
        args: Parsed arguments of ``publish``.

    Returns:
        The exit code and the JSON result with the artifact path and the uploaded files.
    """
    if args.run_id is None:
        snapshot_path = _require_file(args.output_dir / SNAPSHOT_FILE, SNAPSHOT_FILE)
        run_id = load_snapshot(snapshot_path).target.run_id
    else:
        run_id = args.run_id
    result = publish_artifacts(create_client(), run_id, args.output_dir)
    return EXIT_OK, {"ok": True, "runId": run_id, **result.to_dict()}


def _add_output_dir(parser: argparse.ArgumentParser) -> None:
    """Add the required ``--output-dir`` option.

    Args:
        parser: Subcommand parser.
    """
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Output directory of the session."
    )


def _add_snapshot(parser: argparse.ArgumentParser) -> None:
    """Add the ``--snapshot`` option.

    Args:
        parser: Subcommand parser.
    """
    parser.add_argument(
        "--snapshot", type=Path, help=f"Snapshot file. Default: <output-dir>/{SNAPSHOT_FILE}."
    )


def _add_vams_profile(parser: argparse.ArgumentParser) -> None:
    """Add the ``--vams-profile`` option.

    Args:
        parser: Subcommand parser.
    """
    parser.add_argument(
        "--vams-profile",
        help="VamsCLI profile. Default: $VAMS_CLI_PROFILE, else 'default'.",
    )


def _add_detect_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``detect`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser("detect", help="Check whether MLflow and VAMS can be read.")
    _add_vams_profile(parser)
    parser.set_defaults(handler=run_detect)


def _add_fetch_vams_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``fetch-vams`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser(
        "fetch-vams", help="Read the VAMS datasets the runs of the snapshot used."
    )
    _add_output_dir(parser)
    _add_snapshot(parser)
    _add_vams_profile(parser)
    parser.set_defaults(handler=run_fetch_vams)


def _add_fetch_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``fetch`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser("fetch", help="Fetch the target and comparison runs.")
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--run-id", help="MLflow run ID of the target run.")
    target_group.add_argument(
        "--pipeline-execution-id", help="VAMS pipeline execution ID of the target run."
    )
    parser.add_argument(
        "--experiment",
        action="append",
        help=(
            "Experiment name to search. Repeat to give several. "
            f"Default: {', '.join(DEFAULT_EXPERIMENTS)}."
        ),
    )
    parser.add_argument(
        "--max-compare-runs",
        type=int,
        default=DEFAULT_MAX_COMPARE_RUNS,
        help=f"Upper limit of comparison runs. Default: {DEFAULT_MAX_COMPARE_RUNS}.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Default: samples/lerobot-openpi/outputs/advisor/<UTC timestamp>.",
    )
    parser.set_defaults(handler=run_fetch)


def _add_analyze_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``analyze`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser("analyze", help="Diagnose the runs of the snapshot.")
    _add_output_dir(parser)
    _add_snapshot(parser)
    parser.add_argument(
        "--thresholds",
        type=Path,
        help="Thresholds file. Default: config/diagnosis_thresholds.json.",
    )
    parser.add_argument(
        "--datasets",
        type=Path,
        help=f"Datasets file. Default: <output-dir>/{DATASETS_FILE} when it exists.",
    )
    parser.add_argument(
        "--dataset-thresholds",
        type=Path,
        help="Dataset thresholds file. Default: config/dataset_thresholds.json.",
    )
    parser.set_defaults(handler=run_analyze)


def _add_validate_proposal_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``validate-proposal`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser(
        "validate-proposal", help="Validate proposal.json and write vams_parameters.json."
    )
    _add_output_dir(parser)
    parser.add_argument("--pipeline-id", required=True, help="VAMS pipeline ID.")
    parser.add_argument(
        "--proposal", type=Path, help=f"Proposal file. Default: <output-dir>/{PROPOSAL_FILE}."
    )
    _add_snapshot(parser)
    parser.add_argument(
        "--constraints",
        type=Path,
        help="Operational constraints file. Default: config/proposal_constraints.json.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        help="VAMS pipeline template. Default: hyperpod/vams/template.json.",
    )
    parser.set_defaults(handler=run_validate_proposal)


def _add_plot_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``plot`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser("plot", help="Draw the learning curve figures.")
    _add_output_dir(parser)
    _add_snapshot(parser)
    parser.set_defaults(handler=run_plot)


def _add_publish_parser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``publish`` subcommand.

    Args:
        subparsers: Subparsers of the main parser.
    """
    parser = subparsers.add_parser(
        "publish", help="Upload the session outputs to the run as MLflow artifacts."
    )
    _add_output_dir(parser)
    parser.add_argument(
        "--run-id", help="Run that receives the artifacts. Default: the target run of snapshot."
    )
    parser.set_defaults(handler=run_publish)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand.

    Returns:
        The argument parser.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Diagnose LeRobot training runs in MLflow and validate the next parameters.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_detect_parser(subparsers)
    _add_fetch_parser(subparsers)
    _add_fetch_vams_parser(subparsers)
    _add_analyze_parser(subparsers)
    _add_validate_proposal_parser(subparsers)
    _add_plot_parser(subparsers)
    _add_publish_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and print its JSON result.

    Args:
        argv: Command line arguments without the program name. ``sys.argv[1:]`` when None.

    Returns:
        The exit code: 0 on success, 1 for proposal violations, 2 for expected errors.
    """
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], CommandResult] = args.handler
    try:
        exit_code, result = handler(args)
    except EXPECTED_ERRORS as error:
        traceback.print_exc(file=sys.stderr)
        message = _redact_tracking_uri(f"{type(error).__name__}: {error}")
        _print_json({"ok": False, "error": message})
        return EXIT_ERROR
    _print_json(result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
