#!/usr/bin/env bash
# cleanup-failed-stacks.sh — clean up Physai CDK stacks that NEVER completed
# their initial CREATE successfully.
#
# ⚠️  USE ONLY FOR NEVER-SUCCESSFULLY-CREATED STACKS.
# Targets stacks in CREATE_FAILED, ROLLBACK_COMPLETE, or ROLLBACK_FAILED state.
# These statuses can only occur during the initial CREATE phase — a stack that
# was ever successfully created and is being updated or destroyed is NOT in
# these states (it would be in UPDATE_* or DELETE_*).
#
# DO NOT use this script for:
#   - Stacks in UPDATE_ROLLBACK_* (failed UPDATE) — those had data and configs
#   - Stacks in DELETE_FAILED — use cleanup.sh instead
#   - Normal teardown of running deployments — use cleanup.sh
#
# What it does:
#   1. Lists matching stacks (PhysaiClusterStack first, then PhysaiInfraStack)
#      in the target statuses, with the retained resources they own.
#   2. Prompts for confirmation.
#   3. Executes cleanup: ClusterStack before InfraStack (ClusterStack imports
#      from InfraStack, so CloudFormation won't let InfraStack go first).
#      For each stack: delete retained resources, wait for FSx/RDS to actually
#      finish deleting (they hold VPC ENIs), then delete the stack entry.
#
# Usage: infra/scripts/cleanup-failed-stacks.sh [--profile PROFILE] [--region REGION]

set -euo pipefail

AWS_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) AWS_ARGS+=(--profile "$2"); shift 2 ;;
    --region)  AWS_ARGS+=(--region  "$2"); shift 2 ;;
    -h|--help)
      sed -n 's/^# \{0,1\}//p' "$0" | head -27
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

aws_ ()  { aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} --no-cli-pager "$@"; }
aws_q () { aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} --no-cli-pager --output text "$@" 2>/dev/null || true; }

STATUSES="CREATE_FAILED ROLLBACK_COMPLETE ROLLBACK_FAILED"

list_failed() {  # name-contains
  aws_q cloudformation list-stacks --stack-status-filter $STATUSES \
    --query "StackSummaries[?contains(StackName,'$1')].StackName"
}

stack_status() {  # stack-name
  aws_q cloudformation describe-stacks --stack-name "$1" --query 'Stacks[0].StackStatus'
}

list_retained() {  # stack-name
  aws_q cloudformation list-stack-resources --stack-name "$1" \
    --query "StackResourceSummaries[?ResourceType=='AWS::RDS::DBInstance' \
          || ResourceType=='AWS::FSx::FileSystem' \
          || ResourceType=='AWS::S3::Bucket' \
          || ResourceType=='AWS::SecretsManager::Secret'].[ResourceType,PhysicalResourceId]"
}

# ClusterStack must be deleted before InfraStack (ClusterStack imports exports
# from InfraStack; CloudFormation forbids destroying an exporter while importers exist).
CLUSTERS=$(list_failed PhysaiClusterStack)
INFRAS=$(list_failed PhysaiInfraStack)

if [[ -z "$CLUSTERS" && -z "$INFRAS" ]]; then
  echo "No failed Physai stacks found."
  exit 0
fi

echo "================================================================================"
echo " DANGER — this will DELETE resources owned by never-successfully-created stacks"
echo "================================================================================"
echo ""

print_stack() {
  local s status
  s="$1"
  status=$(stack_status "$s")
  echo "  - $s  [$status]"
  local r
  r=$(list_retained "$s")
  if [[ -n "$r" ]]; then
    echo "$r" | sed 's/^/      /'
  else
    echo "      (no retained resources)"
  fi
}

for s in $CLUSTERS; do print_stack "$s"; done
for s in $INFRAS; do print_stack "$s"; done

echo ""
read -rp "Proceed with deletion of the above stacks and their retained resources? [yes/NO] " ans
if [[ "$ans" != "yes" ]]; then
  echo "Aborted."
  exit 0
fi

# -- helpers for resource cleanup --

delete_retained() {  # stack-name
  local s="$1"
  while IFS=$'\t' read -r TYPE ID; do
    [[ -z "$TYPE" ]] && continue
    echo "  Deleting $TYPE: $ID"
    case "$TYPE" in
      AWS::RDS::DBInstance)
        aws_ rds modify-db-instance --db-instance-identifier "$ID" \
          --no-deletion-protection --apply-immediately >/dev/null 2>&1 || true
        aws_ rds delete-db-instance --db-instance-identifier "$ID" \
          --skip-final-snapshot >/dev/null 2>&1 || true
        ;;
      AWS::FSx::FileSystem)
        aws_ fsx delete-file-system --file-system-id "$ID" >/dev/null 2>&1 || true
        ;;
      AWS::S3::Bucket)
        aws_ s3 rm "s3://$ID" --recursive >/dev/null 2>&1 || true
        aws_ s3 rb "s3://$ID" >/dev/null 2>&1 || true
        ;;
      AWS::SecretsManager::Secret)
        aws_ secretsmanager delete-secret --secret-id "$ID" \
          --force-delete-without-recovery >/dev/null 2>&1 || true
        ;;
    esac
  done < <(list_retained "$s")
}

wait_fsx_rds_gone() {  # stack-name — block until FSx/RDS resources in this stack are gone
  local s="$1"
  local ids
  ids=$(list_retained "$s" | awk -F'\t' '$1=="AWS::FSx::FileSystem" || $1=="AWS::RDS::DBInstance" {print $1"\t"$2}')
  [[ -z "$ids" ]] && return 0
  echo "  Waiting for FSx/RDS to finish deleting (may take 5-10 minutes)..."
  while IFS=$'\t' read -r TYPE ID; do
    [[ -z "$TYPE" ]] && continue
    while true; do
      local still=""
      case "$TYPE" in
        AWS::FSx::FileSystem)
          still=$(aws_q fsx describe-file-systems --file-system-ids "$ID" \
            --query 'FileSystems[0].FileSystemId')
          ;;
        AWS::RDS::DBInstance)
          still=$(aws_q rds describe-db-instances --db-instance-identifier "$ID" \
            --query 'DBInstances[0].DBInstanceIdentifier')
          ;;
      esac
      if [[ -z "$still" || "$still" == "None" ]]; then
        echo "    $TYPE $ID gone"
        break
      fi
      sleep 15
    done
  done <<< "$ids"
}

delete_stack() {  # stack-name
  local s="$1"
  echo "  Disabling termination protection on $s (if any)"
  aws_ cloudformation update-termination-protection \
    --stack-name "$s" --no-enable-termination-protection >/dev/null 2>&1 || true
  echo "  Removing stack entry: $s"
  aws_ cloudformation delete-stack --stack-name "$s" >/dev/null 2>&1 || true
}

# -- execute: ClusterStack(s) first, then InfraStack(s) --

for s in $CLUSTERS; do
  echo ""
  echo "=== Cleaning $s ==="
  delete_retained "$s"
  delete_stack "$s"
done

for s in $INFRAS; do
  echo ""
  echo "=== Cleaning $s ==="
  delete_retained "$s"
  # InfraStack holds VPC+subnets+SGs that FSx/RDS reference. delete-stack
  # won't succeed until FSx/RDS are actually gone.
  wait_fsx_rds_gone "$s"
  delete_stack "$s"
done

echo ""
echo "=== Final status ==="
for s in $CLUSTERS $INFRAS; do
  status=$(stack_status "$s")
  echo "  $s: ${status:-gone}"
done
