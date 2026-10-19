# Job Intake

Lets an external system submit a pipeline run to the cluster and be told when it finishes. The first caller is [VAMS](https://github.com/awslabs/visual-asset-management-system), which submits a training run against an asset's data and ingests the resulting checkpoints. So far the stack deploys the transport that reaches the login node; see [STATUS.md](STATUS.md) for what is built.

Job intake is **opt-in**: nothing is deployed until a caller is configured. The manual path — `physai run` on the login node, see [PHYSAI_CLI.md](PHYSAI_CLI.md) — is unchanged.

## Overview

```
this account
────────────
 submit Lambda ── SSH over the cluster VPC ──► HyperPod login node
```

## Enabling

List the permitted callers in `infra/cdk.json`. The list is the switch: with no callers the stack is not created.

```json
{
  "context": {
    "jobIntakeCallers": [
      { "accountId": "111122223333" },
      { "accountId": "444455556666", "roleNamePattern": "vams-prod-*-pask-integration" }
    ]
  }
}
```

Then deploy as usual:

```bash
cd infra
npx cdk deploy --all
```

`jobIntakeCallers` must be set in `cdk.json`, not with `-c`: the CDK CLI passes every `-c` value through as a string, and synth fails if it is not an array.

| Caller field | Required | Default | Notes |
|---|---|---|---|
| `accountId` | Yes | — | 12-digit AWS account ID |
| `roleNamePattern` | No | `*-pask-integration` | Role-name pattern, with `*` and `?` wildcards, that every caller role must match. A pattern of only wildcards is rejected. Matched as a pattern on the role's ARN rather than resolved to the role, so a deleted-and-recreated role keeps working |

Synth fails if an account ID is not 12 digits or is listed twice, or if a role-name pattern is malformed or only wildcards.

### Disabling

Destroy `PhysaiJobIntakeStack` first, then empty `jobIntakeCallers` and deploy. The other order fails: `PhysaiClusterStack` exports a value the job-intake stack imports, and CloudFormation will not remove an export still in use. Destroying `PhysaiClusterStack` destroys `PhysaiJobIntakeStack` with it, for the same reason.

### The SSH Key on the Login Node

`PhysaiClusterStack` generates an ED25519 key pair for the submit Lambda. `install_job_intake_key.sh`, a login-node lifecycle script, installs its public half for `ubuntu` whenever a login node is created or replaced, so a replacement gets it automatically. A reboot does not re-run it.

A new cluster needs nothing more: the key pair is created with it, before the login node first boots. On a cluster that was already running when job intake was enabled, run the script once after the first deploy:

```bash
infra/scripts/run-lifecycle.sh --group login-group --script install_job_intake_key.sh
```

The script installs the key only when job intake is enabled and the key pair passes verification, and otherwise removes any key it installed earlier:

- The key pair must be the one whose id CDK wrote into `physai-config.json`, which only `PhysaiClusterStack` writes. A key pair recreated by hand under the same name has a different id, so it is refused
- It must be an ED25519 key whose public key is a single well-formed line, so nothing from the API can add an `authorized_keys` option
- The installed line is `restrict,from="10.0.0.0/16" ssh-ed25519 ... job-intake:<name>`: usable only from inside the VPC, with forwarding and pty allocation disabled. Other keys in the file are left alone
- A transient lookup error leaves the file as it was
- The file is written as `ubuntu`, so the script never follows a link planted in that user's home
- The script never fails the node's lifecycle. A failure is logged, and leaves job intake unable to reach that node until the script runs again

## What Gets Deployed

`PhysaiJobIntakeStack`, defined in [`infra/lib/job-intake-stack.ts`](../../infra/lib/job-intake-stack.ts). Resource names derive from the unsuffixed `clusterName`, so they survive a redeploy of `PhysaiClusterStack`. `<clusterName>` below is that unsuffixed name, except in `<clusterName>-ExecutionRole`, which belongs to `PhysaiClusterStack` and carries its suffix.

### Login-Node SSH Key Pair

- An `AWS::EC2::KeyPair` of type ED25519, named after the suffixed cluster name. It is created in `PhysaiClusterStack` rather than this stack, so it exists before the login node first boots
- The private half is stored by CloudFormation in SSM Parameter Store as a SecureString, and only ever held in the submit Lambda's memory
- Turning job intake off, or destroying `PhysaiClusterStack`, deletes it, private half included; the public half is removed from the login node at its next lifecycle run

### Submit Lambda

`<clusterName>-job-intake-submit`: Python 3.12 zip, arm64, 512 MB, 5-minute timeout.

- Attached to the cluster VPC's private subnets, with its own security group and a single `tcp/22` ingress rule on the cluster security group
- Resolves the login node's `PrivateDnsHostname` with `sagemaker:ListClusterNodes` and `sagemaker:DescribeClusterNode` on every invocation, since HyperPod replaces nodes
- Connects as `ubuntu` with paramiko, with a 20 s connect timeout and 60 s per command, so a call fits well inside the function's timeout. Host keys are not pinned, because a replaced node has a new one

| Payload | Effect |
|---|---|
| `{"probeOnly": true}` | Opens a session and runs `hostname; whoami; sinfo` |

**Bundling.** CDK runs [`infra/lambda/build.py`](../../infra/lambda/build.py) on the host through `Code.fromCustomCommand`. Docker is not needed.

- Dependencies are installed for the Lambda's platform with `uv pip install --python-platform aarch64-unknown-linux-gnu --only-binary :all:`, so a missing wheel fails the build rather than compiling for the host
- The zip is written with fixed timestamps and modes and sorted entries, so an unchanged source produces an unchanged asset hash and no redeploy
- Requires `uv` and `python3` on the deploying machine

### IAM

- **Submit Lambda**: `ssm:GetParameter` on the key's parameter; `kms:Decrypt` conditioned on `kms:ViaService: ssm.<region>.amazonaws.com`; `sagemaker:ListClusterNodes` and `sagemaker:DescribeClusterNode`; `AWSLambdaVPCAccessExecutionRole`
- **`<clusterName>-ExecutionRole`**, in `PhysaiClusterStack` while job intake is enabled: `ec2:DescribeKeyPairs` for `install_job_intake_key.sh`. EC2 cannot scope it to one key pair, so it is granted on `*`; the script asks only for the id in its config

### Outputs

| Output | Use |
|---|---|
| `SubmitFunctionName` | Probe the transport |
| `SshKeyName` | The key pair the submit Lambda logs in with |

## Verifying

Probe the transport:

```bash
aws lambda invoke --function-name <SubmitFunctionName> \
  --payload '{"probeOnly": true}' --cli-binary-format raw-in-base64-out \
  /dev/stdout | jq .
```

| Result | Meaning |
|---|---|
| `ok: true`, `stdout` lists the `sinfo` partitions | Whole path works |
| `reason: authenticationFailed` | Network path works; the key is not installed. Run `install_job_intake_key.sh` as above and read its output |
| `no route to <host>:22` | The VPC attachment, the `tcp/22` rule, or a login node that is still booting |
| `no Running node in instance group ...` | The cluster has no running login node |
| `AccessDenied` on `ssm:GetParameter` | The submit Lambda's IAM |

## See Also

- [INFRA.md](INFRA.md) — the CDK stacks
- [PHYSAI_CLI.md](PHYSAI_CLI.md) — the manual path
