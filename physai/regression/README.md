# physai-regression

Regression checks for the Physical AI Pipeline Platform. Each check is a
pytest function that runs against a live cluster and asserts the platform
machinery (lifecycle scripts, Slurm features, DCV state, CLI surface) is
intact.

The suite is a [uv](https://docs.astral.sh/uv/) virtual project; it imports
`physai.ssh.Session` and the `physai` console script from the sibling `cli/`
package via an editable path dep, so `uv run` always exercises this
worktree's CLI (not a stray editable install elsewhere on the machine).
Provision the environment first:

```bash
cd physai/regression
uv sync
```

## Run the checks against a live cluster

The checks SSH into the cluster's login node and submit a Slurm
allocation across the GPU partition, briefly preempting the queue. If
others share the cluster, coordinate before running.

The runner takes a lifecycle mode positional. `python -m physai_regression
--help` lists the modes; arguments after the mode are forwarded to
pytest.

### Modes

- **`fresh`** — `cdk destroy PhysaiClusterStack` → `cdk deploy` → run
  checks → `cdk destroy` (only on pass; left up on failure for debugging).
  Idempotent; ~25 min wall time. Use to catch regressions that only
  surface on a true first-boot deployment.
- **`upgrade-existing`** — `cdk deploy PhysaiClusterStack` +
  `infra/scripts/run-lifecycle.sh --all` → run checks. Mirrors the
  documented "applying lifecycle script changes to a running cluster"
  upgrade flow against a user-managed cluster. ~13 min against a healthy
  cluster (the upgrade is a near-no-op when scripts are unchanged).
  Cluster is left running; on check failure it stays in the upgraded
  state with no automated rollback.
- **`upgrade-from-ref`** — Fully ephemeral end-to-end upgrade test:
  `git worktree add` at `<ref>` → `npm ci` + `cdk deploy` from the
  worktree → `cdk deploy` + `run-lifecycle.sh --all` from current HEAD
  (the upgrade) → run checks → `cdk destroy` + worktree cleanup. ~35 min
  wall time. On failure the cluster + worktree are left on disk so
  you can inspect; the worktree path is printed to stderr.

### Coverage layers

Checks are split into two layers, selected via marker:

- **Layer 1 — Platform** (`@pytest.mark.platform`; default). Runs against
  the tiny fake-project fixture under `regression/fixtures/fake-project/`.
  Validates lifecycle scripts, Slurm features, DCV state, CLI plumbing,
  the build system, and end-to-end pipeline chaining in seconds-per-check.
  Total wall time once the cluster is up: ~8 min.
- **Layer 2 — Built-in example** (`@pytest.mark.builtin_example`; opt-in
  via `--builtin-examples`). Runs the real shipped examples under
  `examples/` against a small but real raw fixture, with `--max-steps 100`
  capping training. Always rebuilds the example's containers — a stale
  `.sqsh` would mask exactly the kind of regression this layer exists
  to catch. Validates that the bundled pipelines still convert, train,
  and eval to COMPLETED end-to-end. Today this is the `so101-gr00t/`
  LiftCube example, parametrized over **both** GR00T variants (N1.5 and
  N1.6); the parametrize ids are the version strings, so `-k n1.5` /
  `-k n1.6` runs just one. Wall time: ~100 min and ~$2 of AWS spend per
  variant (the container rebuilds and the capped training run dominate);
  budget ~150–180 min for both, since the shared `so101-converter` and
  `leisaac-runtime` containers are built only once. Requires
  `--raw-source <URI>` (see below).

### `--raw-source <URI>`

Layer 2 needs a raw data fixture. Supply it via the mandatory
`--raw-source` flag (valid only with `--builtin-examples`; rejected
otherwise). The fixture is re-fetched fresh for each run and removed
when the check passes (left in place on failure for inspection).

Three URI schemes:

- **`file:///abs/path`** — a local directory on the machine running the
  suite. Its contents are uploaded to the cluster.
  ```bash
  --raw-source file:///Users/me/datasets/liftcube/
  ```
- **`s3://bucket[/prefix/]`** — an S3 prefix. The credentials from
  `--profile`/`--region` must be able to read it.
  ```bash
  --raw-source s3://my-bucket/datasets/liftcube/
  ```
- **`hf://owner/repo[@revision]`** — a public HuggingFace dataset, with
  an optional pinned `@revision`.
  ```bash
  --raw-source hf://organization/liftcube-demos
  --raw-source hf://organization/liftcube-demos@v1.2
  ```

`--builtin-examples` is mode-orthogonal — it can be set with any of the
three modes and adds Layer 2 on top of whatever Layer 1 set the mode
already runs.

### Examples

```bash
cd physai/regression

# Fresh deploy + checks + teardown
python -m physai_regression fresh \
  --profile <aws-profile> --region <aws-region>

# Apply current HEAD's lifecycle to a running cluster, then run the check suite
python -m physai_regression upgrade-existing \
  --profile <aws-profile> --region <aws-region>

# Just one check:
python -m physai_regression upgrade-existing -k dcvagent \
  --profile <aws-profile> --region <aws-region>

# Or against an explicit cluster (skip CFN lookup):
python -m physai_regression upgrade-existing \
  --cluster physai-cluster-abc12345 \
  --profile <aws-profile> --region <aws-region>

# Ephemeral upgrade test: fresh deploy from a baseline ref, upgrade to HEAD, tear down
python -m physai_regression upgrade-from-ref --from-ref v0.2.0 \
  --profile <aws-profile> --region <aws-region>

# Add the Layer 2 shipped-example checks on top of any mode. Runs both GR00T
# variants (N1.5 + N1.6): ~100 min and ~$2 per variant, ~150–180 min total.
# --raw-source is mandatory with --builtin-examples; see the section above for URI schemes.
python -m physai_regression upgrade-existing --builtin-examples \
  --raw-source file:///path/to/local/raw/ \
  --profile <aws-profile> --region <aws-region>

# Just one Layer 2 variant (-k matches the parametrize id):
python -m physai_regression upgrade-existing --builtin-examples -k n1.6 \
  --raw-source file:///path/to/local/raw/ \
  --profile <aws-profile> --region <aws-region>
```

The runner resolves the cluster from the `PhysaiClusterStack`
CloudFormation output (or `--cluster`) and connects over SSH using a
private temporary config — your `~/.ssh/config` is **not** touched, and
nothing it creates is left behind.

## Run unit tests (no AWS required)

The unit tests under `regression/tests/` exercise the regression code's
own wiring with mocks — they don't talk to a cluster:

```bash
cd physai/regression
uv run pytest tests/
```
