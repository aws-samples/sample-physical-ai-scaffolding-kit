"""physai CLI entry point."""

import argparse
import sys

from . import build, clean, config, jobs
from .ssh import Session


def main():
    parser = argparse.ArgumentParser(
        prog="physai", description="Physical AI Pipeline CLI"
    )
    parser.add_argument("--host", help="SSH host (overrides config)")
    sub = parser.add_subparsers(dest="command")

    # build
    p_build = sub.add_parser("build", help="Build a container")
    p_build.add_argument("container_dir", help="Path to container folder")
    p_build.add_argument(
        "--rebuild", action="store_true", help="Remove existing sqsh first"
    )

    # list
    sub.add_parser("list", help="List physai jobs")

    # status
    p_status = sub.add_parser("status", help="Show job status")
    p_status.add_argument("job_id", help="Slurm job ID")

    # logs
    p_logs = sub.add_parser("logs", help="Tail job log")
    p_logs.add_argument("job_id", help="Slurm job ID")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a job")
    p_cancel.add_argument("job_id", help="Slurm job ID")

    # clean
    p_clean = sub.add_parser("clean", help="Remove old build dirs and logs")
    p_clean.add_argument(
        "--older-than",
        type=int,
        default=7,
        metavar="DAYS",
        help="Remove items older than N days (default: 7)",
    )
    p_clean.add_argument(
        "--all", action="store_true", help="Remove all (ignore age filter)"
    )
    p_clean.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed"
    )
    p_clean.add_argument(
        "-f", action="store_true", dest="force", help="Skip confirmation"
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cfg = config.load(args.host)
    session = Session(cfg["host"])

    if args.command == "build":
        build.run_build(session, args.container_dir, args.rebuild)
    elif args.command == "list":
        jobs.list_jobs(session)
    elif args.command == "status":
        jobs.status_job(session, args.job_id)
    elif args.command == "logs":
        jobs.logs_job(session, args.job_id)
    elif args.command == "cancel":
        jobs.cancel_job(session, args.job_id)
    elif args.command == "clean":
        clean.run_clean(
            session,
            older_than=0 if args.all else args.older_than,
            dry_run=args.dry_run,
            force=args.force,
        )
