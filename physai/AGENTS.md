# AGENTS.md — Physical AI Pipeline Platform

> **For AI coding agents.** Human contributors can skip this file —
> [docs/en/](docs/en/) (and its Japanese counterparts in [docs/ja/](docs/ja/))
> cover the same material in more depth for human readers. This file is a
> scannable entry point optimized for agent context budgets: project map,
> long-running-operation warnings, fast verification commands, and pointers
> to the canonical docs.

The Physical AI Pipeline Platform (this `physai/` directory) is a cloud-native
pipeline platform on AWS for robot-learning workflows — from raw demos to
evaluated policies. Three workstreams: `cli/` (Python CLI), `infra/` (CDK
TypeScript), `examples/` (container definitions +
configs). Deploys to a SageMaker HyperPod Slurm cluster.

---

## ⚠️ STOP — Long-Running Operations

Do NOT run these commands without explicit user approval:

- **`npx cdk deploy --all`** — ~20 min, creates/modifies AWS resources
- **`physai build <container>`** — 10–30+ min, submits Slurm job on cluster
- **`physai run --config ...`** — hours, submits training/eval pipeline
- **`npx cdk bootstrap`** — ~2 min, modifies AWS account state
- **`infra/scripts/run-lifecycle.sh --all` (or `--node`/`--group` without `--dry-run`)** — modifies live node state on the cluster via SSM. Idempotent and safe, but still a state change — confirm before running. `--dry-run` is always safe.

Never run these autonomously. Always ask the user first.
See [docs/TIMINGS.md](docs/TIMINGS.md) for the full decision guide.

---

## Quick Verification Commands

Run these freely to verify changes. All are fast and local. Commands below
assume CWD is the repo root (`physai/`) unless prefixed with `cd`.

First-time setup (run once):

```bash
pip install -e "cli[dev]"     # installs physai CLI + ruff + pytest
cd infra && npm install       # installs CDK dependencies
```

| Workstream | Command | Duration |
|------------|---------|----------|
| `cli/` | `cd cli && pytest` | ~2 s |
| `cli/` | `cd cli && ruff check` | ~1 s |
| `cli/` | `cd cli && ruff format` | ~1 s |
| `infra/` | `cd infra && npm run build` | ~5 s |
| `infra/` | `cd infra && npm run synth` | ~10 s |
| `examples/` | No automated validation yet | — |

---

## Doc Map

| File | Purpose |
|------|---------|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | End-user guide: CLI reference, data management, troubleshooting |
| [docs/PIPELINE-DESIGN.md](docs/PIPELINE-DESIGN.md) | Platform architecture and design rationale |
| [docs/PHYSAI-DESIGN.md](docs/PHYSAI-DESIGN.md) | CLI internals: SSH session, build system, pipeline orchestration |
| [docs/INFRA.md](docs/INFRA.md) | CDK stack layout, lifecycle scripts, deployment |
| [docs/STATUS.md](docs/STATUS.md) | Phase 1 scope and implementation status |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | Code style and conventions across all workstreams |
| [docs/TIMINGS.md](docs/TIMINGS.md) | Command timings and agent decision guide |
| [README.md](README.md) | Project overview and quick start |

---

## Key Entry Points

### `cli/` — Python CLI

| File | Role |
|------|------|
| `cli/physai/cli.py` | CLI dispatcher (argparse subcommands) |
| `cli/physai/build.py` | Container build logic |
| `cli/physai/pipeline.py` | Pipeline orchestration (train → eval chaining) |
| `cli/physai/ssh.py` | SSH session management via subprocess |
| `cli/physai/config.py` | Config loading (`~/.physai/config.yaml`) |

### `infra/` — CDK TypeScript

| File | Role |
|------|------|
| `infra/bin/app.ts` | CDK app entry point |
| `infra/lib/infra-stack.ts` | VPC, S3, FSx, RDS, Secrets Manager |
| `infra/lib/cluster-stack.ts` | HyperPod cluster, IAM, lifecycle bucket |
| `infra/lifecycle/on_create.sh` | Node bootstrap entry point (called by HyperPod) |
| `infra/lifecycle/lifecycle_script.py` | Lifecycle orchestrator (Python, runs all scripts in order) |
| `infra/lifecycle/_lib.sh` | Shared node-type detection + `require_node_type` guard |
| `infra/scripts/run-lifecycle.sh` | Re-run lifecycle scripts on existing nodes via SSM |

### `examples/` — Container Definitions

| File | Role |
|------|------|
| `examples/so101-gr00t/project.yaml` | Shared container config (base image, env vars) |
| `examples/so101-gr00t/containers/*/container.yaml` | Per-container build spec (name, partition, gres) |
| `examples/so101-gr00t/configs/*.yaml` | Run configs for pipeline jobs |

---

## Gotchas: Running Commands on HyperPod via SSM

HyperPod cluster nodes use `sagemaker-cluster:<cluster-id>_<group>-<node-id>`
SSM targets, not regular EC2 instance IDs. This has several non-obvious
consequences that apply to anything in `infra/scripts/` talking to the
cluster:

1. **`aws ssm send-command` does NOT work.** The `sagemaker-cluster:` target
   format is only accepted by `aws ssm start-session`. `send-command`
   requires `i-*` or `mi-*` IDs and returns `InvalidInstanceId` for HyperPod
   nodes. So all remote command execution goes through `start-session` with
   `AWS-StartNonInteractiveCommand` as the document.

2. **`start-session` needs a PTY on the client side.** Running
   `aws ssm start-session ...` from a script (no TTY) causes the data
   channel to close before all output has been delivered to your stdout —
   you see "Cannot perform start session: EOF" with most of the command
   output missing. Wrap the `aws` call in `script -q /dev/null ...` to
   allocate a PTY. The `script` utility's syntax differs between GNU
   (`script -q -c CMD /dev/null`) and BSD/macOS (`script -q /dev/null
   CMD...`); handle both if the tool needs to work on both.

3. **The CLI's exit code ≠ the remote exit code.** `aws ssm start-session`
   exits 0 as long as the session opened, regardless of whether the remote
   command succeeded. To detect remote failure, pass
   `separateOutputStream=["true"]` in `--parameters` and parse the
   `EXIT_CODE: N` line the agent appends to the output.

4. **The `command` parameter is tokenized by `shlex.Split`, not a shell.**
   Shell metacharacters (`|`, `&&`, `;`, `>`) are passed as literal argv
   elements unless you explicitly wrap in `bash -c "..."`. Put the whole
   pipeline inside `bash -c`.

5. **Output files persist even if the stream is lost.** The agent writes
   remote stdout/stderr to files under
   `/var/lib/amazon/ssm/sagemaker-cluster:<...>/session/orchestration/<session-id>/NonInteractiveCommands/`
   (`stdout`, `stderr`, `ipcTempFile.log`). If output is garbled or
   truncated on your end, these files have the complete record — useful for
   debugging agent behavior.

The canonical implementation of all four points is in
`infra/scripts/run-lifecycle.sh` (see `run_on_node()`). If you're writing a
new SSM-based tool, copy that pattern rather than rolling your own.

---

## Conventions

See [docs/CONVENTIONS.md](docs/CONVENTIONS.md) for code style, naming, and
commit conventions across all workstreams.

---

## Known Gaps

- No tests or linter configured for `infra/` (TypeScript)
- No shellcheck for `infra/lifecycle/` shell scripts
- JSON schemas in `cli/physai/schemas/` validate `container.yaml`, `project.yaml`, `run_config.yaml`, and `~/.physai/config.yaml` at load time via `jsonschema`
- No multi-session agent progress tracking (checkpoint files, feature JSON) yet
