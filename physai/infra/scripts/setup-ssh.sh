#!/usr/bin/env bash
# Set up SSH access to the HyperPod login node by uploading a public key via SSM.
# Usage: setup-ssh.sh [--cluster NAME] [--key PATH] [--profile PROFILE] [--region REGION]
set -euo pipefail

CLUSTER=""
KEY=""
AWS_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster) CLUSTER="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --profile) AWS_ARGS+=(--profile "$2"); shift 2 ;;
    --region) AWS_ARGS+=(--region "$2"); shift 2 ;;
    -h|--help)
      sed -n 's/^# //p' "$0" | head -3
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Resolve key
if [[ -z "$KEY" ]]; then
  for candidate in ~/.ssh/id_rsa.pub ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub; do
    if [[ -f "$candidate" ]]; then
      KEY="$candidate"
      break
    fi
  done
fi
if [[ ! -f "$KEY" ]]; then
  echo "ERROR: SSH public key not found. Checked ~/.ssh/id_rsa.pub, ~/.ssh/id_ed25519.pub, ~/.ssh/id_ecdsa.pub. Use --key PATH." >&2
  exit 1
fi

# Verify it's a public key (starts with known SSH key type)
FIRST_WORD=$(awk '{print $1; exit}' "$KEY")
case "$FIRST_WORD" in
  ssh-rsa|ssh-ed25519|ssh-dss|ecdsa-sha2-*|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) ;;
  *)
    echo "ERROR: $KEY does not look like an SSH public key (first word: '$FIRST_WORD'). Use the .pub file." >&2
    exit 1
    ;;
esac
echo "Using key: $KEY"

# Resolve cluster name from PhysaiClusterStack if not specified
if [[ -z "$CLUSTER" ]]; then
  echo -n "Querying PhysaiClusterStack for cluster name...  "
  CLUSTER=$(aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} cloudformation describe-stacks --stack-name PhysaiClusterStack \
    --query 'Stacks[0].Outputs[?OutputKey==`ClusterName`].OutputValue' --output text 2>/dev/null || echo "")
  if [[ -z "$CLUSTER" || "$CLUSTER" == "None" ]]; then
    echo "not found"
    echo "ERROR: --cluster not specified and PhysaiClusterStack not found. Pass --cluster <name>." >&2
    exit 1
  fi
  echo "found: $CLUSTER"
  read -rp "Use this cluster? [Y/n] " ans
  if [[ -n "$ans" && ! "$ans" =~ ^[Yy]$ ]]; then
    exit 1
  fi
fi

# Find login node
echo -n "Finding login node...  "
LOGIN_ID=$(aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} sagemaker list-cluster-nodes --cluster-name "$CLUSTER" \
  --query 'ClusterNodeSummaries[?InstanceGroupName==`login-group`].InstanceId' --output text)
if [[ -z "$LOGIN_ID" || "$LOGIN_ID" == "None" ]]; then
  echo "not found"
  echo "ERROR: no login-group instance in cluster $CLUSTER" >&2
  exit 1
fi
echo "$LOGIN_ID"

# Get cluster ID (for SSM target format)
CLUSTER_ID=$(aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} sagemaker describe-cluster --cluster-name "$CLUSTER" \
  --query 'ClusterArn' --output text | awk -F/ '{print $NF}')
SSM_TARGET="sagemaker-cluster:${CLUSTER_ID}_login-group-${LOGIN_ID}"

# Upload key via SSM
echo -n "Uploading public key via SSM...  "
KEY_CONTENT=$(cat "$KEY")
aws ${AWS_ARGS[@]+"${AWS_ARGS[@]}"} ssm start-session --target "$SSM_TARGET" \
  --document-name AWS-StartNonInteractiveCommand \
  --parameters "{\"command\":[\"sudo -u ubuntu bash -c 'mkdir -p /home/ubuntu/.ssh && chmod 700 /home/ubuntu/.ssh && echo \\\"${KEY_CONTENT}\\\" >> /home/ubuntu/.ssh/authorized_keys && sort -u /home/ubuntu/.ssh/authorized_keys -o /home/ubuntu/.ssh/authorized_keys && chmod 600 /home/ubuntu/.ssh/authorized_keys'\"]}" \
  >/dev/null
echo "done"

# Print SSH config snippet
PROFILE_ARG=""
REGION_ARG=""
for ((i=0; i<${#AWS_ARGS[@]}; i+=2)); do
  [[ "${AWS_ARGS[i]}" == "--profile" ]] && PROFILE_ARG=" --profile ${AWS_ARGS[i+1]}"
  [[ "${AWS_ARGS[i]}" == "--region" ]] && REGION_ARG=" --region ${AWS_ARGS[i+1]}"
done

cat <<EOF

Add this to ~/.ssh/config:

  Host physai-login
    User ubuntu
    ProxyCommand aws ssm start-session --target ${SSM_TARGET}${REGION_ARG}${PROFILE_ARG} --document-name AWS-StartSSHSession --parameters portNumber=%p

Then test: ssh physai-login
EOF
