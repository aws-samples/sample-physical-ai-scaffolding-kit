"""Entry point: ``python -m physai_regression <mode> [pytest-args...]``.

Modes:
    fresh               Destroy → deploy → run checks → destroy on pass.
    upgrade-existing    Apply the documented in-place upgrade (cdk deploy +
                        run-lifecycle.sh --all) to a user-managed cluster,
                        then run the check suite. Cluster is left running.
    upgrade-from-ref    Worktree at <ref> → deploy@ref → upgrade to HEAD →
                        checks → destroy + worktree cleanup. Fully ephemeral
                        on success; both artifacts kept on failure.

Examples::

    python -m physai_regression fresh \\
        --profile myprofile --region us-west-2

    python -m physai_regression upgrade-existing \\
        --profile myprofile --region us-west-2

    python -m physai_regression upgrade-existing -k dcvagent \\
        --profile myprofile --region us-west-2

    python -m physai_regression upgrade-from-ref --from-ref v0.2.0 \\
        --profile myprofile --region us-west-2

    python -m physai_regression upgrade-existing --builtin-examples \\
        --profile myprofile --region us-west-2

``--profile`` and ``--region`` are consumed by both the orchestration
primitives (``cdk deploy``/``destroy``) and the conftest fixtures, so they
are parsed at the top level and forwarded to pytest as well. All other
arguments after the mode are forwarded to pytest unchanged (``-k``, ``-v``,
``-m``, ``-x``, ``--lf``, ``--pdb``, ``--tb=short`` ...). The one exception
is a ``--`` flag that closely resembles one of the runner's own options
(e.g. ``--profil``, ``--regon``): that is rejected as a typo rather than
silently forwarded to pytest and ignored.

``--builtin-examples`` is mode-orthogonal: when set with any of the three
modes it adds the Layer 2 shipped-example checks on top of the Layer 1
platform checks (~100 min and ~$2 added per check). Layer 2 needs a raw
data fixture, supplied via ``--raw-source <URI>`` (see below); the two
flags must be set together.

``--raw-source <URI>`` is mode-orthogonal too. Accepted schemes:

- ``file:///abs/path`` — local source directory; rsynced to the cluster.
- ``s3://bucket[/prefix/]`` — S3 source. Cluster IAM is probed first; on
  permission errors a presigned-URL fallback uses the run's ``--profile``
  / ``--region`` to read the bucket.
- ``hf://owner/repo[@revision]`` — HuggingFace public dataset.

The fixture is always rm'd and re-fetched for each run; the test rms
the staged directory on success and leaves it for inspection on
failure.
"""

import argparse
import difflib
import sys
from pathlib import Path

import pytest

from .orchestration import deploy, flows
from .raw_staging import RawSourceError, parse_raw_source

CHECKS_DIR = Path(__file__).resolve().parent / "checks"

MODES = ["fresh", "upgrade-existing", "upgrade-from-ref"]

# The runner's own long options. A ``--`` token that closely resembles one of
# these is rejected as a typo; every other ``--`` flag is forwarded to pytest.
_RUNNER_LONG_FLAGS = (
    "--profile",
    "--region",
    "--from-ref",
    "--builtin-examples",
    "--raw-source",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m physai_regression",
        description=(
            "Run platform regression checks against a live cluster. "
            "Arguments after the mode are forwarded to pytest."
        ),
    )
    parser.add_argument("mode", choices=MODES, help="Lifecycle mode.")
    parser.add_argument("--profile", default=None, help="AWS profile")
    parser.add_argument("--region", default=None, help="AWS region")
    parser.add_argument(
        "--from-ref",
        default=None,
        help="Starting commit/tag to deploy before upgrading to HEAD "
        "(required for upgrade-from-ref).",
    )
    parser.add_argument(
        "--builtin-examples",
        action="store_true",
        help="Also run Layer 2 shipped-example checks (~100 min and ~$2 per "
        "check on top of the Layer 1 cost). Without this flag only the "
        "default Layer 1 platform checks run. Requires --raw-source.",
    )
    parser.add_argument(
        "--raw-source",
        dest="raw_source",
        default=None,
        metavar="<URI>",
        help="URI for the Layer 2 raw fixture. Required when "
        "--builtin-examples is set; rejected otherwise. Schemes: "
        "file:///abs/path | s3://bucket[/prefix/] | "
        "hf://owner/repo[@revision]. The fixture is re-fetched every "
        "run; the cluster wipes /fsx/raw/<name>/ before downloading.",
    )
    return parser


def _pytest_args(
    profile: str | None,
    region: str | None,
    extra: list[str],
    *,
    builtin_examples: bool = False,
    raw_source: str | None = None,
) -> list[str]:
    """Forward AWS args + caller-supplied args to pytest under ``CHECKS_DIR``.

    ``builtin_examples=True`` overrides the default ``-m "not builtin_example"``
    in ``pytest.ini`` with ``-m "platform or builtin_example"`` so both
    layers run. The default selector explicitly names ``platform`` rather
    than relying on unmarked tests because every check ships with a layer
    marker today.

    ``raw_source`` is forwarded as ``--raw-source <URI>`` so the
    ``raw_source_uri`` fixture can pick it up; the runner-level argparse
    is what enforces it being mandatory-with-``builtin_examples``, so
    this helper only forwards what it's given.
    """
    args: list[str] = [str(CHECKS_DIR)]
    if builtin_examples:
        args += ["-m", "platform or builtin_example"]
    if raw_source:
        args += ["--raw-source", raw_source]
    if profile:
        args += ["--profile", profile]
    if region:
        args += ["--region", region]
    args += extra
    return args


def _run_checks(
    profile: str | None,
    region: str | None,
    extra: list[str],
    *,
    builtin_examples: bool = False,
    raw_source: str | None = None,
) -> int:
    return int(
        pytest.main(
            _pytest_args(
                profile,
                region,
                extra,
                builtin_examples=builtin_examples,
                raw_source=raw_source,
            )
        )
    )


def _run_fresh(
    profile: str | None,
    region: str | None,
    extra: list[str],
    *,
    builtin_examples: bool = False,
    raw_source: str | None = None,
) -> int:
    """fresh: destroy → deploy → checks → destroy on pass."""
    flows.redeploy_from_clean(profile=profile, region=region)
    rc = _run_checks(
        profile,
        region,
        extra,
        builtin_examples=builtin_examples,
        raw_source=raw_source,
    )
    if rc == 0:
        deploy.cdk_destroy(profile=profile, region=region, skip_if_absent=True)
    else:
        print(
            f"Checks exited with code {rc}. Cluster left up so you can debug. "
            "Tear it down with: npx cdk destroy PhysaiClusterStack",
            file=sys.stderr,
        )
    return rc


def _run_upgrade_existing(
    profile: str | None,
    region: str | None,
    extra: list[str],
    *,
    builtin_examples: bool = False,
    raw_source: str | None = None,
) -> int:
    """upgrade-existing: apply the in-place upgrade, then run the check suite.

    On check failure the cluster is left in the upgraded state for the
    user to debug; rolling back would require a separate downgrade flow.
    """
    flows.upgrade_in_place(profile=profile, region=region)
    rc = _run_checks(
        profile,
        region,
        extra,
        builtin_examples=builtin_examples,
        raw_source=raw_source,
    )
    if rc != 0:
        print(
            f"Checks exited with code {rc}. Cluster is in the upgraded state; "
            "debug there or roll back manually.",
            file=sys.stderr,
        )
    return rc


def _run_upgrade_from_ref(
    ref: str,
    profile: str | None,
    region: str | None,
    extra: list[str],
    *,
    builtin_examples: bool = False,
    raw_source: str | None = None,
) -> int:
    """upgrade-from-ref: deploy@ref → upgrade to HEAD → checks → destroy.

    The deploy@ref step validates that ``ref`` was deployable to begin
    with; the checks validate that the in-place upgrade from ``ref`` to
    HEAD landed in a healthy state.

    On failure the cluster + worktree are left on disk for inspection;
    the worktree path is printed so cleanup is one command. Full cleanup
    happens only on success.
    """
    physai_in_worktree = flows.deploy_from_ref(ref, profile=profile, region=region)
    print(f"physai/ at {ref}: {physai_in_worktree}", file=sys.stderr)
    flows.upgrade_in_place(profile=profile, region=region)
    rc = _run_checks(
        profile,
        region,
        extra,
        builtin_examples=builtin_examples,
        raw_source=raw_source,
    )
    if rc != 0:
        print(
            f"Checks exited with code {rc}. "
            f"Cluster left in upgraded state; worktree physai/ at {physai_in_worktree}.",
            file=sys.stderr,
        )
        return rc
    flows.destroy_and_remove_worktree(
        physai_in_worktree, profile=profile, region=region
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, pytest_args = parser.parse_known_args(argv)

    # parse_known_args forwards anything it doesn't recognize to pytest, which
    # is what we want for real pytest passthrough (-k, -v, -m, -x, --lf, --pdb,
    # --tb=short, ...). The one hazard it hides is a TYPO of this runner's own
    # long flags: `--profil x` (or `--regon`) would sail through to pytest and
    # be ignored instead of erroring, so the run proceeds with no profile and
    # fails much later. Catch only that: reject a long flag that closely
    # resembles one of the runner's own options. Everything else — genuine
    # pytest flags, short flags, values — is forwarded untouched. `=value`
    # suffixes are stripped before matching so `--profil=x` is still caught.
    for arg in pytest_args:
        if not arg.startswith("--"):
            continue
        name = arg.split("=", 1)[0]
        if difflib.get_close_matches(name, _RUNNER_LONG_FLAGS, n=1, cutoff=0.7):
            parser.error(
                f"unrecognized flag {name!r} — did you mean one of the runner "
                f"flags {', '.join(_RUNNER_LONG_FLAGS)}? (Genuine pytest flags "
                f"are forwarded; only close typos of runner flags are rejected.)"
            )

    if args.mode != "upgrade-from-ref" and args.from_ref is not None:
        parser.error("--from-ref is only valid for the upgrade-from-ref mode")
    if args.mode == "upgrade-from-ref" and args.from_ref is None:
        parser.error("upgrade-from-ref requires --from-ref <commit-or-tag>")

    if args.raw_source is not None and not args.builtin_examples:
        parser.error("--raw-source is only valid with --builtin-examples")
    if args.builtin_examples and args.raw_source is None:
        parser.error("--builtin-examples requires --raw-source <URI>")
    if args.raw_source is not None:
        # Validate the URI shape now, before any deploy: parse_raw_source is
        # pure (no filesystem/network), so a typo like `s4://...` or
        # `hf://noslash` fails here in milliseconds instead of after a
        # ~tens-of-minutes cluster deploy when the staging fixture runs.
        try:
            parse_raw_source(args.raw_source)
        except RawSourceError as e:
            parser.error(str(e))

    if args.mode == "fresh":
        return _run_fresh(
            args.profile,
            args.region,
            pytest_args,
            builtin_examples=args.builtin_examples,
            raw_source=args.raw_source,
        )
    if args.mode == "upgrade-existing":
        return _run_upgrade_existing(
            args.profile,
            args.region,
            pytest_args,
            builtin_examples=args.builtin_examples,
            raw_source=args.raw_source,
        )
    if args.mode == "upgrade-from-ref":
        return _run_upgrade_from_ref(
            args.from_ref,
            args.profile,
            args.region,
            pytest_args,
            builtin_examples=args.builtin_examples,
            raw_source=args.raw_source,
        )

    raise AssertionError(f"unreachable: argparse rejected unknown mode {args.mode!r}")


if __name__ == "__main__":
    sys.exit(main())
