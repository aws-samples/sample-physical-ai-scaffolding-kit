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
| `cd cli && pytest` | ~2 s | local | yes | YES |
| `ruff check cli/physai/` | ~1 s | local | yes | YES |
| `cd infra && npm install` | ~30 s | local | yes | YES |
| `cd infra && npm run build` | ~5 s | local | yes | YES |
| `cd infra && npm run synth` | ~10 s | local | yes | YES |
| `pip install -e cli` | ~5 s | local | yes | YES |
| `physai list` | seconds | local (SSH) | yes | YES |
| `physai logs <job-id>` | seconds | local (SSH) | yes | YES |
| ⚠️ `npx cdk bootstrap` | **~2 min** | AWS account | yes | **NO** — modifies account state |
| ⚠️ `npx cdk deploy --all` | **~20 min** | AWS | yes | **NO** — creates/modifies AWS resources |
| ⚠️ `physai build <container>` | **10–30+ min** | HyperPod cluster | yes (without `-n`) | **NO** — submits Slurm job |
| ⚠️ `physai run --config ...` | **hours** | HyperPod cluster | yes (without `-n`) | **NO** — submits training/eval pipeline |

---

## Decision Guide

Use this checklist after making changes. All commands below assume CWD is the
repo root (`physai/`) unless they include an explicit `cd`.

- **Changed `cli/` Python code?**
  → Run `cd cli && pytest` (from `cli/`), then `ruff check cli/physai/` (from repo root). Both are safe and fast.
- **Changed `infra/` TypeScript?**
  → Run `cd infra && npm run build` then `npm run synth` (both from `infra/`). Safe and fast.
- **Changed `infra/lifecycle/` shell scripts?**
  → No automated verification available. Describe your changes and ask the user for review.
- **Changed `examples/*/containers/*/setup-hooks/`?**
  → No local verification possible. Ask the user before running `physai build`.
- **Need to deploy infra to AWS?**
  → ⚠️ **STOP.** Ask the user. Never run `cdk deploy` autonomously.
- **Need to build a container?**
  → ⚠️ **STOP.** Ask the user. `physai build` submits a Slurm job on the cluster.
- **Need to run a pipeline?**
  → ⚠️ **STOP.** Ask the user. `physai run` can take hours.

---

## The `-n` Flag

`physai build -n` and `physai run -n` submit the Slurm job and return immediately
instead of streaming logs. Use `physai list` to check job status and
`physai logs <job-id>` to view output afterward.

Prefer `-n` in all cases — the blocking mode (without `-n`) is for interactive
humans watching output, not for agents.
