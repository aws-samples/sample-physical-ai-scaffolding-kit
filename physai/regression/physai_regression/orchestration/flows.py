"""Composed deploy/destroy/upgrade flows used by lifecycle modes.

Each function is a thin sequence of :mod:`deploy` primitives. Modes call
these so the pytest-side code stays focused on running checks.
"""

import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from . import deploy


def redeploy_from_clean(profile: str | None, region: str | None) -> None:
    """Destroy any existing ``PhysaiClusterStack``, then deploy a fresh one.

    The destroy ensures the deploy provisions every node via the lifecycle
    scripts in their initial-bootstrap path. This is the case that catches
    the kind of regressions a re-run on a long-lived dev cluster does not
    (missing FSx dirs, services that only autostart on first boot, etc.).
    """
    deploy.cdk_destroy(profile=profile, region=region, skip_if_absent=True)
    deploy.cdk_deploy(profile=profile, region=region)


def upgrade_in_place(
    profile: str | None,
    region: str | None,
    infra_dir: Path | None = None,
) -> None:
    """Re-upload lifecycle scripts to S3, then re-run them on existing nodes.

    Mirrors the documented "applying lifecycle script changes to a running
    cluster" flow in ``docs/en/DEPLOYMENT.md``: ``cdk deploy`` ships the new
    scripts to S3 (so future node replacements pick them up) and
    ``run-lifecycle.sh --all`` applies them to nodes already running.
    """
    deploy.cdk_deploy(profile=profile, region=region, infra_dir=infra_dir)
    deploy.run_lifecycle_all(profile=profile, region=region)


def _git_root_and_prefix() -> tuple[Path, Path]:
    """Resolve the enclosing git toplevel and physai's path relative to it.

    ``physai/`` is checked into a larger umbrella repo whose root is one
    or more levels above this package, so the worktree must be created
    from the umbrella's toplevel and the physai subdir located within it.
    Returns ``(toplevel, prefix)`` where ``prefix`` is empty when physai
    is itself the repo root.
    """
    r = subprocess.run(
        ["git", "-C", str(deploy.REPO_ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git rev-parse --show-toplevel failed: {r.stderr.strip()}")
    toplevel = Path(r.stdout.strip())
    r = subprocess.run(
        ["git", "-C", str(deploy.REPO_ROOT), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git rev-parse --show-prefix failed: {r.stderr.strip()}")
    return toplevel, Path(r.stdout.strip())


def deploy_from_ref(
    ref: str,
    profile: str | None,
    region: str | None,
) -> Path:
    """Check ``ref`` out into a fresh worktree and deploy from it.

    Returns the path to the physai subdir within the new worktree so the
    caller can clean it up. The worktree name carries a random suffix so
    concurrent runs and stale dirs from prior failures don't collide.
    """
    toplevel, prefix = _git_root_and_prefix()
    suffix = secrets.token_hex(4)
    worktree_root = Path(tempfile.gettempdir()) / f"physai-regression-worktree-{suffix}"
    git_cmd = [
        "git",
        "-C",
        str(toplevel),
        "worktree",
        "add",
        "--detach",
        str(worktree_root),
        ref,
    ]
    r = subprocess.run(git_cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"git worktree add {ref} {worktree_root} failed (exit {r.returncode}): "
            f"{r.stderr.strip()}"
        )
    physai_in_worktree = worktree_root / prefix
    infra_in_worktree = physai_in_worktree / "infra"
    try:
        deploy.npm_ci(infra_in_worktree)
        deploy.cdk_deploy(
            profile=profile,
            region=region,
            infra_dir=infra_in_worktree,
        )
    except Exception:
        # `git worktree add` already materialized worktree_root on disk. A
        # failure here (npm ci / cdk deploy) leaves it orphaned with no path
        # returned to the caller, so surface the path for manual cleanup
        # before re-raising. We deliberately do NOT auto-remove it: a
        # half-deployed worktree is exactly what an operator wants to
        # inspect, matching the keep-on-failure contract the upgrade-from-ref
        # mode already uses for check failures.
        print(
            f"deploy from {ref} failed; worktree left for inspection at "
            f"{worktree_root} (physai/ at {physai_in_worktree}). Remove it "
            f"with: git worktree remove --force {worktree_root}",
            file=sys.stderr,
        )
        raise
    return physai_in_worktree


def destroy_and_remove_worktree(
    physai_in_worktree: Path,
    profile: str | None,
    region: str | None,
) -> None:
    """Destroy the cluster stack and remove the worktree containing ``physai_in_worktree``.

    ``physai_in_worktree`` is the path returned by :func:`deploy_from_ref`
    — the physai subdir inside the worktree. The worktree itself is
    that path's ancestor: the prefix of ``physai/`` within the umbrella
    repo, stripped from the end.
    """
    deploy.cdk_destroy(profile=profile, region=region, skip_if_absent=True)
    toplevel, prefix = _git_root_and_prefix()
    # Strip the same prefix that deploy_from_ref appended.
    worktree_root = physai_in_worktree
    for _ in prefix.parts:
        worktree_root = worktree_root.parent
    git_cmd = [
        "git",
        "-C",
        str(toplevel),
        "worktree",
        "remove",
        "--force",
        str(worktree_root),
    ]
    r = subprocess.run(git_cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"git worktree remove {worktree_root} failed (exit {r.returncode}): "
            f"{r.stderr.strip()}"
        )
