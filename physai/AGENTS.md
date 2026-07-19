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
- **`python -m physai_regression fresh ...`** — `cdk destroy PhysaiClusterStack` → `cdk deploy` → checks → `cdk destroy` on pass. ~25 min wall time, AWS spend, and any in-flight Slurm jobs are lost when the stack is destroyed.
- **`python -m physai_regression upgrade-existing ...`** — Applies the in-place upgrade (`cdk deploy PhysaiClusterStack` + `run-lifecycle.sh --all`) to a user-managed cluster, then runs the platform check suite. ~13 min wall time. Modifies the user's running cluster's lifecycle state and re-uploads scripts to S3; on check failure the cluster is left in the upgraded state with no automated rollback.
- **`python -m physai_regression upgrade-from-ref --from-ref <ref> ...`** — Ephemeral fresh deploy from `<ref>`, upgrade to HEAD, run the platform check suite, then destroy + worktree cleanup. ~35 min wall time, AWS spend, and on failure leaves both a running cluster and a `git worktree` on disk for inspection.
- **Adding `--builtin-examples` to any mode** — Adds the Layer 2 shipped-example checks on top of the Layer 1 platform checks. Requires `--raw-source <URI>` (`file:///abs/path` | `s3://bucket[/prefix/]` | `hf://owner/repo[@rev]`); the cluster wipes `/fsx/raw/<base>-<timestamp>/` and re-fetches every run, so the staged directory is replaced each time. Example: `python -m physai_regression upgrade-existing --builtin-examples --raw-source file:///path/to/raw/ --profile ... --region ...`. Today this runs the `so101-gr00t/` example for **both** GR00T variants (N1.5 and N1.6) — ~100 min and ~$2 of additional AWS spend per variant, so budget ~150–180 min total for the two (the shared `so101-converter`/`leisaac-runtime` builds happen once). Each variant always rebuilds its containers and submits a real `physai run` against the staged fixture (`--max-steps 100`; eval is the long pole at ~57 min). Use `-k n1.5` / `-k n1.6` to run just one variant. Without the flag only Layer 1 runs.
- The unit tests under `regression/tests/` are local and safe; the runner itself in any mode is not.

Never run these autonomously. Always ask the user first.
See [docs/TIMINGS.md](docs/TIMINGS.md) for the full decision guide.

**Tearing down the deployment.** Don't reach for raw `cdk destroy` on
`PhysaiInfraStack` — termination protection is on, and FSx/RDS/S3 are
RETAINed by CloudFormation so a naive destroy fails halfway and leaves
the stack stuck. Run `infra/scripts/cleanup.sh --profile <p> --region <r>`,
which prints the full ordered teardown procedure (cluster destroy →
empty FSx/RDS → S3 → disable termination protection → infra destroy →
optional secret force-delete). The script doesn't execute anything; it
just emits the commands for the user to review and run.

---

## Quick Verification Commands

Run these freely to verify changes. All are fast and local. Commands below
assume CWD is the repo root (`physai/`) unless prefixed with `cd`.

First-time setup (run once):

```bash
pip install -e "cli[dev]"     # installs physai CLI + ruff + pytest
cd infra && npm install       # installs CDK dependencies
cd regression && uv sync      # provisions the regression env (physai editable from ../cli, + boto3)

# Pre-commit hooks (config lives at the repo root: ../.pre-commit-config.yaml).
# Hooks are scoped to physai/ files and split between two stages:
#   - pre-commit: ruff, ty (type check), cli pytest, regression unit
#                 pytest (via uv run), shellcheck, tsc --noEmit,
#                 whitespace/EOF/yaml/json
#   - pre-push:   cdk synth
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```

If `pre-commit install` errors with **"Cowardly refusing to install hooks
with `core.hooksPath` set"**, another tool on your machine is managing git
hooks via a system- or global-level `core.hooksPath`. Find it with:

```bash
git config --show-origin --get-all core.hooksPath
```

Then pick one of:

1. **Chain via the other tool.** If it documents a way to invoke local
   `.git/hooks/*`, install pre-commit with `GIT_CONFIG=/dev/null` so it
   ignores the system setting during install only:
   ```bash
   GIT_CONFIG=/dev/null pre-commit install --hook-type pre-commit --hook-type pre-push
   ```
   The other tool's hook then runs first and delegates to the pre-commit
   script in `.git/hooks/`.
2. **Use Git 2.54+ config-based hooks.** `git hook list pre-commit` should
   show both; see `git help hook`.
3. **Skip git wiring; run manually.** No install — invoke before pushing:
   ```bash
   pre-commit run --all-files
   pre-commit run --all-files --hook-stage pre-push
   ```

| Workstream | Command | Duration |
|------------|---------|----------|
| `cli/` | `cd cli && python -m pytest` | ~2 s |
| `cli/` | `cd cli && ruff check` | ~1 s |
| `cli/` | `cd cli && ruff format` | ~1 s |
| `infra/` | `cd infra && npm run build` | ~5 s |
| `infra/` | `cd infra && npm run synth` | ~10 s |
| `regression/` | `cd regression && uv run pytest tests/` | ~1 s |
| Python (all) | `pre-commit run ty --all-files` (type-checks `cli/`, `regression/`, `infra/lifecycle/` in pre-commit's managed env) | ~3 s |
| `examples/` | No automated validation yet | — |

The regression *checks* themselves (`python -m physai_regression fresh ...`, `... upgrade-existing ...`, or `... upgrade-from-ref ...`) talk to a live cluster — see the STOP block above.

---

## Read Whole Files

When reading a doc or source file to **edit it, summarize it, decide whether
it's correct, or answer a question that depends on its content**, read the
**entire** file before acting. Do NOT make decisions based on a partial read
(`offset` / `limit`, or a `grep`-then-edit shortcut).

Why this matters here:

- Docs in this repo are heavily cross-referenced (e.g. RUN_SAMPLE references
  PIPELINE_DEVELOP §4 by anchor; the example README deep-links into STATUS).
  Editing one paragraph without seeing the rest of the file routinely breaks
  internal consistency or repeats information another section already covers.
- Source files have implicit invariants spread across the file (e.g.
  `STAGE_REGISTRY` vs `ALL_STAGES` in `cli/physai/pipeline.py`; `--gres` being
  optional only when `cfg.get("gres")` is falsy in `build.py`). A partial read
  hides these.
- "Looks fine" after a partial read is not "is fine." Multiple bugs found
  during the doc audit on this branch were introduced exactly this way.

Acceptable partial reads:

- Locating a known string or pattern with `grep -n` followed by a **full Read
  of the file** before editing.
- Reading a generated file (e.g. `package-lock.json`, very large logs) where
  the whole file is too big to be useful and the question is local.
- Re-checking a small region you've already read in full earlier in the
  session, where the file is known unchanged.

If a file is genuinely too large to read whole, say so explicitly to the user
and ask how to proceed — don't silently work from fragments.

---

## Doc Map

| File | Purpose |
|------|---------|
| [docs/en/DEPLOYMENT.md](docs/en/DEPLOYMENT.md) | Step-by-step cluster deployment |
| [docs/en/RUN_SAMPLE.md](docs/en/RUN_SAMPLE.md) | Run the bundled SO-101 + GR00T sample |
| [docs/en/PHYSAI_CLI.md](docs/en/PHYSAI_CLI.md) | CLI reference: commands, data management, build/run workflow internals |
| [docs/en/PIPELINE_DEVELOP.md](docs/en/PIPELINE_DEVELOP.md) | Author your own pipeline: containers, entrypoint contracts, run configs |
| [docs/en/PIPELINE_DESIGN.md](docs/en/PIPELINE_DESIGN.md) | Platform architecture and design rationale |
| [docs/en/INFRA.md](docs/en/INFRA.md) | CDK stack layout, lifecycle scripts, deployment internals |
| [docs/en/STATUS.md](docs/en/STATUS.md) | Phase 1 scope and implementation status |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | Code style and conventions across all workstreams |
| [docs/TIMINGS.md](docs/TIMINGS.md) | Command timings and agent decision guide |
| [regression/README.md](regression/README.md) | Layer 1 + Layer 2 regression checks against a live cluster |
| [README.md](README.md) | Project overview and quick start |

Japanese counterparts live under [docs/ja/](docs/ja/) with the same filenames + `.ja.md` suffix.

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
| `infra/scripts/cleanup.sh` | Print the ordered teardown procedure for the deployment (does not execute) |

### `examples/` — Container Definitions

| File | Role |
|------|------|
| `examples/so101-gr00t/project.yaml` | Shared container config (base image, env vars) |
| `examples/so101-gr00t/containers/*/container.yaml` | Per-container build spec (name, partition, gres) |
| `examples/so101-gr00t/configs/*.yaml` | Run configs for pipeline jobs |

### `regression/` — Live-Cluster Regression Checks

| File | Role |
|------|------|
| `regression/physai_regression/__main__.py` | Entry point: `python -m physai_regression <mode> ...` (modes: `fresh`, `upgrade-existing`, `upgrade-from-ref`; mode-orthogonal flag: `--builtin-examples`) |
| `regression/physai_regression/orchestration/` | `cdk deploy`/`destroy`/stack-discovery wrappers and the lifecycle-mode sequences they compose |
| `regression/physai_regression/conftest.py` | Session fixtures: `cluster_name` + `data_bucket_name` (CFN), `ssh_config_path` (tempfile), `physai_session`, `physai_cli` (CLI subprocess wrapper), `fake_project_dir`, `fake_containers_built` |
| `regression/physai_regression/checks/` | Pytest checks; each function is one regression test. Layer 1 (`@pytest.mark.platform`) runs by default; Layer 2 (`@pytest.mark.builtin_example`) runs only with `--builtin-examples` |
| `regression/fixtures/fake-project/` | Tiny project (`fake-converter`, `fake-trainer`, `fake-evaluator`) that exercises the full pipeline path in seconds |
| `regression/tests/` | Unit tests for the regression code itself (no AWS/SSH) |

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

## Gotchas: `cdk deploy --region` is silently ignored

`cdk deploy` and `cdk destroy` do not honor `--region` despite the
top-level `cdk` parser accepting it (the flag is parsed and discarded;
see [aws/aws-cdk#28725](https://github.com/aws/aws-cdk/issues/28725) and
`cdk deploy --help`, which does not list it). For environment-agnostic
stacks like this app's, the deploy region is resolved from
`AWS_REGION` / `AWS_DEFAULT_REGION` in the parent shell, falling back to
the profile's `region` setting. `--profile` works as documented.

Practical rules when scripting around `cdk`:

- Pass the target region in the subprocess **environment**, not on the
  argv: `env={"AWS_REGION": region, "AWS_DEFAULT_REGION": region, ...}`.
- The `aws` CLI does honor `--region` correctly — only `cdk` is special.
  Use `--region` freely on `aws cloudformation`, `aws s3`, etc.
- When generating commands for a human to copy-paste (e.g.
  `infra/scripts/cleanup.sh`), emit `export AWS_REGION=<region>` before
  the `cdk` line rather than appending `--region` to the cdk command.

The canonical implementation is
`regression/physai_regression/orchestration/deploy.py` — copy that
pattern when adding new `cdk` invocations.

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
