# Conventions

> **For AI coding agents.** Prescriptive code-style rules for agents
> modifying this repository. Human contributors typically pick these up from
> existing code — this file exists because agents benefit from an explicit
> list they can consult before writing new code. See [AGENTS.md](../AGENTS.md)
> for the full agent entry point.

Prescriptive rules for all workstreams. Follow these when writing or modifying code.

---

## Python (`cli/`)

- MUST use Python 3.10+ type hints: `str | None`, not `Optional[str]`
- MUST add docstrings to modules and all public functions
- MUST prefix private functions with `_`
- MUST keep dependencies minimal — standard library + `pyyaml` + `jsonschema` only. DO NOT add heavy frameworks
- MUST pass linting with zero warnings and stay formatted:
  ```bash
  ruff check
  ruff format
  ```
- MUST pass formatting (CI-style, no writes):
  ```bash
  ruff format --check
  ```
  Use `ruff format` (no `--check`) to apply fixes before committing.
- Tests MUST mirror source layout: `cli/tests/test_<module>.py` for `cli/physai/<module>.py`
- MUST use `pytest` as the test runner, invoked via `python -m`:
  ```bash
  cd cli && python -m pytest
  ```
  Plain `pytest` resolves `import physai` through whatever package is on `sys.path` — which, with an editable install pointing at a different worktree, is *not* this worktree's source. `python -m pytest` prepends the cwd to `sys.path`, so you always test the worktree you're sitting in.
- MUST use `unittest.mock` for SSH session mocking — DO NOT make real SSH calls in tests
- Use `from __future__ import annotations` only in modules that actually need it — i.e. those with forward references (types referring to a not-yet-defined class in the same module). On Python <3.14 those modules won't import without it (or without manually string-quoting each forward-ref annotation); on 3.14+ it's a harmless no-op (PEP 649 makes annotations lazy by default).
- Entry point is `physai.cli:main` registered as a console_script in `pyproject.toml`
- Ruff config lives in `cli/pyproject.toml` — check there before adding ignore rules

---

## TypeScript (`infra/`)

- MUST use strict mode (`"strict": true` in tsconfig.json)
- MUST target ES2022 (`"target": "ES2022"`, commonjs modules, outDir `dist/`)
- MUST follow CDK v2 patterns
- MUST define a typed `Props` interface for each Stack class
- MUST use namespace imports:
  ```typescript
  import * as cdk from "aws-cdk-lib";
  ```
- MUST pass `tsc` compilation:
  ```bash
  cd infra && npm run build
  ```
- Formatting: singleQuote, tabWidth 2, printWidth 100 (matches parent `.vscode/settings.json`)
- DO NOT add test frameworks yet — no test infrastructure exists (known gap)
- DO NOT add eslint — no config exists; rely on `tsc` for correctness

---

## Shell (`infra/lifecycle/`)

These scripts run on HyperPod nodes during cluster creation, orchestrated by
`lifecycle_script.py` (invoked from `on_create.sh`).

- Use descriptive snake_case filenames (e.g. `install_docker.sh`, `start_slurm.sh`) — ordering is controlled by `lifecycle_script.py`, NOT by filename prefix
- MUST include `#!/bin/bash` shebang if using bash-specific features; use `#!/bin/sh` otherwise
- SHOULD use `set -euo pipefail` (or `-exo pipefail` when tracing is useful); some existing scripts still use `set -ex` — migrate opportunistically
- MUST source `_lib.sh` right after `set -...` to auto-detect `NODE_TYPE` (controller/login/compute). Scripts that only apply to some node types MUST call `require_node_type <type> [<type> ...]` immediately after sourcing
- Scripts MUST be idempotent and safe to re-run — `run-lifecycle.sh` re-runs them on existing nodes; each re-run should hit an "already installed" fast path when the node is already configured
- Scripts run as root during node provisioning — no `.root.sh` suffix needed
- No shellcheck configured in CI — review shell changes manually (known gap). Local `shellcheck -x infra/lifecycle/*.sh` should be clean
- To apply changes to a running cluster, use `infra/scripts/run-lifecycle.sh` (preferred — works on all node types including the controller). Replacing a node via `scontrol update ... state=fail` also works for workers/login but requires `cdk deploy` first to update the S3 copy. See [`docs/en/DEPLOYMENT.md`](en/DEPLOYMENT.md#applying-lifecycle-script-changes-to-a-running-cluster-advanced).

---

## Container Definitions (`examples/`)

- Each container directory MUST contain a `container.yaml` with at minimum:
  - `name` — container identifier
  - `partition` — Slurm partition to build on (default `gpu`)
  - Optional: `gres` — GPU resource spec (e.g. `gpu:1`). Omit for CPU-only containers.
  - Optional: `base_container` for layered builds (references another container's name)
- `setup-hooks/` directory contains shell scripts that run during `physai build`:
  - MUST use numbered prefix for ordering: `10-foo.sh`, `20-bar.sh`, `90-cleanup.sh`, etc.
  - MUST use `.root.sh` suffix for hooks that need to run as root (e.g. `10-system-packages.root.sh`); plain `.sh` runs as the unprivileged build user
  - MUST include `#!/bin/bash` shebang and `set -euo pipefail` for bash scripts
- Optional `app/` directory holds runtime scripts (e.g. `train.sh`, `eval.sh`) copied to `/app/` in the image
- Shared config lives in `project.yaml` one level up (base_image, env vars)
- DO NOT write Dockerfiles — this repo uses a custom build system via `physai build`
- DO NOT modify `project.yaml` without checking impact on all sibling containers
- Build output: squashfs images on `/fsx/enroot/` on the cluster

---

## Configuration Files

- MUST use YAML for all project config — not JSON, not TOML
- Exceptions (tool-mandated JSON): `package.json`, `tsconfig.json`, `cdk.json`
- User config (`~/.physai/config.yaml`) is NOT checked into the repo
- `run_config.yaml` files live in `examples/*/configs/`
- Model configs live in `examples/*/model_configs/`
- JSON schemas for each YAML config format live in `cli/physai/schemas/` (`cli-config.schema.json`, `project.schema.json`, `container.schema.json`, `run-config.schema.json`) and are validated at load time via `schema.py`

---

## Git

- Commit subjects for changes inside `physai/` MUST be prefixed with `physai: ` to scope the change to this sub-project
  - Example: `physai: Add base_container, doctor, non-streaming mode`
  - This is because `physai/` is one sub-project inside a monorepo (alongside `hyperpod/`, `samples/`). Commits touching those siblings use their own prefix
- Branch naming follows `feat/*`, `fix/*` based on recent history
- DO NOT force-push to shared branches
- DO NOT commit secrets, credentials, or local-state files: `.env`, `*.pem`, AWS credential files, `~/.aws/` contents, or anything containing API keys/tokens
