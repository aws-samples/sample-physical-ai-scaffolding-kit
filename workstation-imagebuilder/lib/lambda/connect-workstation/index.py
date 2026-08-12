"""
Connect Workstation Lambda

Generates a DCV connection token for an existing workstation instance.
Writes a token file via SSM that the dcv-token-verifier service validates.

Input event:
{
  "instanceId": "i-xxxx"  # required: target instance ID
}

Returns:
{
  "instanceId": "i-xxxx",
  "publicIp": "x.x.x.x",
  "dcvUrl": "https://x.x.x.x:8443/?authToken=<token>#console",
  "dcvNativeCmd": "/Applications/DCV\\ Viewer.app/... <url> &>/dev/null &",
  "ssmCommand": "aws ssm start-session --target i-xxxx"
}
"""

import time

import boto3

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")


def handler(event, context):
    instance_id = event.get("instanceId")
    if not instance_id:
        raise ValueError("instanceId is required")

    desc = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = desc["Reservations"]
    if not reservations or not reservations[0]["Instances"]:
        raise ValueError(f"Instance {instance_id} not found")

    instance = reservations[0]["Instances"][0]
    state = instance["State"]["Name"]
    if state != "running":
        raise ValueError(
            f"Instance {instance_id} is in state '{state}'. Must be 'running'."
        )

    public_ip = instance.get("PublicIpAddress")
    if not public_ip:
        raise ValueError(f"Instance {instance_id} has no public IP address")

    import secrets

    token = secrets.token_urlsafe(32)
    validity_seconds = 600

    write_token_cmd = (
        f"mkdir -p /var/run/dcv-tokens && "
        f'echo \'{{"user":"ubuntu","expires":\'$(( $(date +%s) + {validity_seconds} ))\'}}\' '
        f"> /var/run/dcv-tokens/{token}"
    )

    command_response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [write_token_cmd]},
        TimeoutSeconds=30,
    )

    command_id = command_response["Command"]["CommandId"]

    for _ in range(20):
        time.sleep(2)
        result = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        if result["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
            break

    if result["Status"] != "Success":
        stderr = result.get("StandardErrorContent", "")
        raise RuntimeError(
            f"Failed to write DCV token. Status: {result['Status']}. "
            f"Error: {stderr}"
        )

    dcv_url = f"https://{public_ip}:8443/?authToken={token}#console"

    return {
        "instanceId": instance_id,
        "publicIp": public_ip,
        "dcvUrl": dcv_url,
        "dcvNativeCmd": (
            '/Applications/DCV\\ Viewer.app/Contents/MacOS/dcvviewer'
            ' --certificate-validation-policy=accept-untrusted'
            f' "{dcv_url}" &>/dev/null &'
        ),
        "ssmCommand": f"aws ssm start-session --target {instance_id}",
    }
