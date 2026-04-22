#!/usr/bin/env bash
# cleanup.sh — print the exact commands needed to tear down the physai CDK deployment.
#
# DOES NOT EXECUTE ANYTHING. It queries the current stacks to resolve resource
# names and prints concrete commands for the user to review and run manually.
#
# Usage: infra/cleanup.sh [--profile PROFILE] [--region REGION]

set -euo pipefail

AWS_ARGS=()
AWS_ARGS_STR=" --no-cli-pager"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) AWS_ARGS+=(--profile "$2"); AWS_ARGS_STR+=" --profile $2"; shift 2 ;;
    --region)  AWS_ARGS+=(--region  "$2"); AWS_ARGS_STR+=" --region $2";  shift 2 ;;
    -h|--help)
      sed -n 's/^# \{0,1\}//p' "$0" | head -7
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Resolve resource names from the current stacks
resource() {  # stack, resource_type
  aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} cloudformation list-stack-resources --stack-name "$1" \
    --query "StackResourceSummaries[?ResourceType=='$2']|[0].PhysicalResourceId" \
    --output text 2>/dev/null || echo ""
}

DATA_BUCKET=$(resource PhysaiInfraStack AWS::S3::Bucket)
FSX_ID=$(resource PhysaiInfraStack AWS::FSx::FileSystem)
RDS_ID=$(resource PhysaiInfraStack AWS::RDS::DBInstance)
SECRET_ID=$(resource PhysaiInfraStack AWS::SecretsManager::Secret)

cat <<EOF
================================================================================
 DANGER — READ BEFORE PROCEEDING
================================================================================

This will destroy the physai CDK deployment. The following data is PERMANENTLY
LOST unless you have copies elsewhere:

  • S3 data bucket       — raw demos, datasets, checkpoints, results
  • FSx for Lustre       — working storage (/fsx)
  • RDS (Slurm accounting)
  • HyperPod cluster     — all running jobs are killed
  • Lifecycle scripts bucket
  • VPC, subnets, security groups

Some resources are RETAINED by CloudFormation (DataBucket, FSx, RDS) when
PhysaiInfraStack is destroyed. They must be deleted manually after the stack is gone.

PhysaiInfraStack has termination protection ON. You must disable it first.

This script PRINTS commands only — it does NOT execute anything.
Review each command and run it yourself.

================================================================================
 Commands to tear down — run in order
================================================================================

# 1. Destroy PhysaiClusterStack first (HyperPod, IAM, lifecycle bucket).
#    This releases the cluster's ENIs from the VPC.
cd infra
npx cdk destroy PhysaiClusterStack${AWS_ARGS_STR}

# 2. Delete the retained resources that hold VPC/SG/subnet dependencies.
#    FSx and RDS keep ENIs in the private subnets and references to the cluster
#    and DB security groups. They MUST go BEFORE PhysaiInfraStack can be destroyed.
#    RDS has deletion protection enabled by default on production-class setups;
#    disable it before deleting.
aws fsx delete-file-system --file-system-id ${FSX_ID:-<FSX_ID>}${AWS_ARGS_STR}

aws rds modify-db-instance --db-instance-identifier ${RDS_ID:-<RDS_ID>} \\
  --no-deletion-protection --apply-immediately${AWS_ARGS_STR}
aws rds delete-db-instance --db-instance-identifier ${RDS_ID:-<RDS_ID>} \\
  --skip-final-snapshot${AWS_ARGS_STR}

# Wait for both to finish deleting (FSx takes ~5 min, RDS ~5-10 min). Example:
aws fsx describe-file-systems --file-system-ids ${FSX_ID:-<FSX_ID>}${AWS_ARGS_STR} 2>&1 | grep -q NotFound && echo FSx gone
aws rds describe-db-instances --db-instance-identifier ${RDS_ID:-<RDS_ID>}${AWS_ARGS_STR} 2>&1 | grep -q DBInstanceNotFound && echo RDS gone

# 3. Empty and delete the retained S3 data bucket (no VPC dependency, but also
#    needs manual cleanup since it's RETAIN).
aws s3 rm s3://${DATA_BUCKET:-<DATA_BUCKET>} --recursive${AWS_ARGS_STR}
aws s3 rb s3://${DATA_BUCKET:-<DATA_BUCKET>}${AWS_ARGS_STR}

# 4. Disable termination protection on PhysaiInfraStack.
aws cloudformation update-termination-protection \\
  --stack-name PhysaiInfraStack --no-enable-termination-protection${AWS_ARGS_STR}

# 5. Destroy PhysaiInfraStack (VPC, SGs, subnets, NAT, etc.).
npx cdk destroy PhysaiInfraStack${AWS_ARGS_STR}

# 6. Optional: the DB secret is deleted by PhysaiInfraStack destroy, but
#    Secrets Manager holds it in a 7-30 day recovery window. Run this only if
#    you need to re-deploy immediately with the same secret name.
aws secretsmanager delete-secret --secret-id ${SECRET_ID:-<SECRET_ID>} \\
  --force-delete-without-recovery${AWS_ARGS_STR}

================================================================================
EOF
