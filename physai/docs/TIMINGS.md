# Command Timings & Agent Decision Guide

> **For AI coding agents.** This file tells agents how long each command
> takes and which ones need explicit user approval before running
> (deployments, training jobs, anything that costs money or modifies shared
> AWS state). Humans generally don't need this file — they already know
> which commands are slow. See [AGENTS.md](../AGENTS.md) for the full agent
> entry point.

Reference for every command an agent might run. Check here before executing anything.

---

## Timing Table

| Command | Duration | Runs where | Blocks terminal? | Safe to run unprompted? |
|---------|----------|------------|-------------------|------------------------|
| `cd cli && python -m pytest` | ~2 s | local | yes | YES |
| `ruff check cli/physai/` | ~1 s | local | yes | YES |
| `ruff format cli/physai/` | ~1 s | local | yes | YES |
| `cd infra && npm install` | ~30 s | local | yes | YES |
| `cd infra && npm run build` | ~5 s | local | yes | YES |
| `cd infra && npm run synth` | ~10 s | local | yes | YES |
| `pip install -e cli` | ~5 s | local | yes | YES |
| `cd regression && uv sync` | ~3 s | local | yes | YES |
| `cd regression && uv run pytest tests/` | ~1 s | local | yes | YES |
| ⚠️ `python -m physai_regression fresh ...` | **~25 min** | local + AWS + cluster | yes | **NO** — destroys, redeploys, and (on pass) destroys `PhysaiClusterStack`. ~8 min destroy (skipped if stack absent) + ~8 min deploy + ~8 min Layer 1 + ~8 min destroy on pass. Costs AWS deploy time and kills any in-flight jobs |
| ⚠️ `python -m physai_regression upgrade-existing ...` | **~13 min** | local + AWS + cluster | yes | **NO** — `cdk deploy` + `run-lifecycle.sh --all` against a user-managed cluster, then run checks. ~5 min upgrade + ~8 min Layer 1. Modifies node state in place; no automated rollback if checks fail |
| ⚠️ `python -m physai_regression upgrade-from-ref --from-ref <ref> ...` | **~35 min** | local + AWS + cluster | yes | **NO** — ephemeral fresh deploy from `<ref>` + upgrade-in-place to HEAD + checks + destroy + worktree cleanup. ~6 min worktree `npm ci` + deploy from `<ref>` ~8 min + ~5 min upgrade + ~8 min Layer 1 + ~8 min destroy. AWS deploy + teardown spend each run |
| ⚠️ `python -m physai_regression <mode> --builtin-examples --raw-source <URI> ...` | **+~150–180 min** for both Layer 2 variants (~100 min each, shared builds once) | local + AWS + cluster | yes | **NO** — adds the shipped-example checks on top of any mode's Layer 1 cost. Runs `so101-gr00t/` for both GR00T variants (N1.5 + N1.6); `-k n1.5` / `-k n1.6` runs just one (~100 min, ~$2). `--raw-source` is mandatory (`file:///abs/path` \| `s3://bucket[/prefix/]` \| `hf://owner/repo[@rev]`); the cluster wipes `/fsx/raw/<name>-<ts>/` and re-fetches per variant. Each variant rebuilds its containers (~40 min, dominated by `leisaac-runtime`'s ~26 min — built once and shared across variants) and submits a real `physai run` (`--max-steps 100`; convert ~1 min, train ~5 min, eval ~57 min). Add 1–10 min for staging depending on URI scheme + fixture size; staging is silent (no progress streaming). ~$2 AWS spend per variant |
| `physai list` | seconds | local (SSH) | yes | YES |
| `physai logs <job-id>` | seconds | local (SSH) | yes | YES |
| `infra/scripts/run-lifecycle.sh --dry-run ...` | seconds | local (+ AWS read-only) | yes | YES |
| `infra/scripts/run-lifecycle.sh --node ... --script ...` | seconds–minutes | cluster (via SSM) | yes | **ASK** — modifies node state (but idempotent) |
| `infra/scripts/run-lifecycle.sh --all` | ~1 min (no-op) to 10+ min (first install) | cluster (via SSM) | yes | **ASK** — modifies every node |
| `infra/scripts/cleanup.sh ...` | seconds | local (+ AWS read-only) | yes | YES — prints commands only; does not execute. Use this to discover the correct teardown order before running any `cdk destroy`/`aws ... delete-*` |
| ⚠️ `npx cdk bootstrap` | **~2 min** | AWS account | yes | **NO** — modifies account state |
| ⚠️ `npx cdk deploy --all` | **~20 min** | AWS | yes | **NO** — creates/modifies AWS resources |
| ⚠️ `physai build <container>` | **10–30+ min** | HyperPod cluster | yes (without `-n`) | **NO** — submits Slurm job |
| ⚠️ `physai run --config ...` | **hours** | HyperPod cluster | yes (without `-n`) | **NO** — submits training/eval pipeline |
| ⚠️ `physai eval --visual ...` | **minutes–hours** | HyperPod cluster | yes (without `-n`) | **NO** — submits an eval job and holds a DCV slot on a GPU node |
| `aws ssm start-session ... AWS-StartPortForwardingSession` (DCV tunnel) | runs until killed | local | yes (foreground) | **ASK** — long-lived tunnel; run in background and stop it when done |

---

## Decision Guide

Use this checklist after making changes. All commands below assume CWD is the
repo root (`physai/`) unless they include an explicit `cd`.

- **Changed `cli/` Python code?**
  → Run `cd cli && python -m pytest` (from `cli/`), then `ruff check cli/physai/` and `ruff format cli/physai/` (from repo root). All three are safe and fast.
- **Changed `regression/` Python code?**
  → Run `cd regression && uv run pytest tests/`. The unit tests under `tests/` use mocks and don't talk to AWS — they're safe. The actual checks (`physai_regression/checks/`) only run via `python -m physai_regression {fresh,upgrade-existing,upgrade-from-ref} ...` against a live cluster — ⚠️ **STOP** and ask the user before invoking any of them. `fresh` (~25 min) and `upgrade-from-ref` (~35 min) destroy/redeploy the cluster; `upgrade-existing` (~13 min) only modifies a running cluster's lifecycle state. Adding `--builtin-examples` to any mode runs the Layer 2 shipped-example checks on top of Layer 1 — today that's the `so101-gr00t/` example for both GR00T variants (N1.5 + N1.6), ~100 min and ~$2 of AWS spend per variant (~150–180 min total since the `so101-converter`/`leisaac-runtime` builds are shared; containers always rebuilt; real `physai run` capped at `--max-steps 100`, with eval as the long pole at ~57 min). Use `-k n1.5` / `-k n1.6` to run just one variant. Layer 2 also requires `--raw-source <URI>` (`file:///abs/path` \| `s3://bucket[/prefix/]` \| `hf://owner/repo[@rev]`) which stages a fresh raw fixture into `/fsx/raw/` each run. Only opt in when you've changed `examples/` or want to validate end-to-end before a release.
- **Changed `infra/` TypeScript?**
  → Run `cd infra && npm run build` then `npm run synth` (both from `infra/`). Safe and fast.
- **Changed `infra/lifecycle/` shell scripts?**
  → Run `shellcheck -x infra/lifecycle/*.sh` for a local lint pass (not in CI, run manually). To apply on the live cluster, use `infra/scripts/run-lifecycle.sh` — ASK the user first, since it modifies node state. `--dry-run` is always safe.
- **Changed `examples/*/containers/*/setup-hooks/`?**
  → No local verification possible. Ask the user before running `physai build`.
- **Need to deploy infra to AWS?**
  → ⚠️ **STOP.** Ask the user. Never run `cdk deploy` autonomously.
- **Need to build a container?**
  → ⚠️ **STOP.** Ask the user. `physai build` submits a Slurm job on the cluster.
- **Need to run a pipeline?**
  → ⚠️ **STOP.** Ask the user. `physai run` can take hours.
- **Need to test visual evaluation (`physai eval --visual`)?**
  → ⚠️ **STOP.** Ask the user. Submits an eval Slurm job AND holds the GPU node's DCV flock until the job ends. Browser verification needs a live SSM tunnel — run it with `run_in_background` and stop the task when done so you don't leave it open.
- **Need to re-run lifecycle scripts on the cluster?**
  → ⚠️ **STOP.** Ask the user. `run-lifecycle.sh` is idempotent and safe, but it modifies live node state. Use `--dry-run` freely to preview.
- **Need to tear down the deployment?**
  → ⚠️ **STOP.** Ask the user. Don't run raw `cdk destroy PhysaiInfraStack` — termination protection is on, and FSx/RDS/S3 are RETAINed by CloudFormation, so a naive destroy fails halfway. Run `infra/scripts/cleanup.sh --profile <p> --region <r>` first to print the ordered teardown procedure (cluster destroy → FSx/RDS/S3 → disable termination protection → infra destroy → optional secret force-delete). The script prints commands only; review and run them yourself.

---

## The `-n` Flag

`physai build -n` and `physai run -n` submit the Slurm job and return immediately
instead of streaming logs. Use `physai list` to check job status and
`physai logs <job-id>` to view output afterward.

Prefer `-n` in all cases — the blocking mode (without `-n`) is for interactive
humans watching output, not for agents.
