"""
Launch Workstation Lambda

Starts an EC2 instance from the Image Builder AMI with minimal parameters.

Input event:
{
  "imageName": "isaacsim6.0",    # required: image name (matches ImageNames in config)
  "instanceName": "my-ws",       # required: instance name tag
  "instanceType": "g6e.4xlarge", # optional: override instance type
  "diskSizeGb": 512,             # optional: override root volume size (GiB)
  "subnetId": "subnet-xxx"       # optional: specific subnet to launch in
}

Returns:
{
  "instanceId": "i-xxxx",
  "publicIp": "x.x.x.x",
  "waitCommand": "aws ec2 wait instance-status-ok ...",
  "connectCommand": "aws lambda invoke --function-name <connect-fn> ..."
}
"""

import os

import boto3

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")


def find_subnet_for_instance_type(subnet_ids, instance_type):
    """Find a subnet whose AZ supports the given instance type."""
    subnets_response = ec2.describe_subnets(SubnetIds=subnet_ids)
    subnets_by_az = {}
    for subnet in subnets_response["Subnets"]:
        az = subnet["AvailabilityZone"]
        subnets_by_az.setdefault(az, []).append(subnet["SubnetId"])

    offerings_response = ec2.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[
            {"Name": "instance-type", "Values": [instance_type]},
            {"Name": "location", "Values": list(subnets_by_az.keys())},
        ],
    )

    available_azs = {o["Location"] for o in offerings_response["InstanceTypeOfferings"]}

    for az in available_azs:
        if az in subnets_by_az:
            return subnets_by_az[az][0]

    raise ValueError(
        f"Instance type {instance_type} is not available in any of the "
        f"configured subnets. Available AZs for this instance type: "
        f"{sorted(available_azs)}, configured AZs: {sorted(subnets_by_az.keys())}"
    )


def handler(event, context):
    image_name = event.get("imageName")
    instance_name = event.get("instanceName")
    instance_type = event.get("instanceType", os.environ["DEFAULT_INSTANCE_TYPE"])
    disk_size_gb = event.get("diskSizeGb")
    subnet_id = event.get("subnetId")

    if not image_name or not instance_name:
        raise ValueError("imageName and instanceName are required")

    # Resolve AMI ID from SSM Parameter Store
    ami_parameter_prefix = os.environ["AMI_PARAMETER_PREFIX"]
    ami_parameter_name = f"{ami_parameter_prefix}{image_name}"

    try:
        ami_response = ssm.get_parameter(Name=ami_parameter_name)
        ami_id = ami_response["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        raise ValueError(
            f"AMI not found for image '{image_name}'. "
            f"Parameter {ami_parameter_name} does not exist. "
            f"Build the AMI first using Image Builder."
        )

    if ami_id == "NOT_BUILT_YET":
        raise ValueError(
            f"AMI for image '{image_name}' has not been built yet. "
            f"Run the Image Builder pipeline first."
        )

    # Resolve subnet
    security_group_id = os.environ["SECURITY_GROUP_ID"]
    instance_profile_arn = os.environ["INSTANCE_PROFILE_ARN"]
    s3files_fs_id = os.environ["S3FILES_FS_ID"]
    region = os.environ["AWS_REGION"]
    vpc_subnet_ids = os.environ["VPC_SUBNET_IDS"].split(",")

    if not subnet_id:
        subnet_id = find_subnet_for_instance_type(vpc_subnet_ids, instance_type)

    # UserData script to mount S3 Files
    userdata = f"""#!/bin/bash
set -euo pipefail
LOG="/var/log/workstation-bootstrap.log"
SUMMARY="/var/log/workstation-bootstrap.summary"
STATE_DIR="/var/lib/workstation-bootstrap"
mkdir -p "$STATE_DIR"
exec > >(tee -a "$LOG") 2>&1
export DEBIAN_FRONTEND=noninteractive

# Wait for network connectivity (DNS + outbound)
for i in $(seq 1 30); do
  if curl -s --max-time 3 https://checkip.amazonaws.com/ > /dev/null 2>&1; then
    break
  fi
  echo "Waiting for network... ($i/30)"
  sleep 2
done

if [[ ! -f "$STATE_DIR/mount-s3files.done" ]]; then
  echo "Installing EFS utils and mounting S3 Files..."
  apt-get update -yq
  curl -fsSL https://amazon-efs-utils.aws.com/efs-utils-installer.sh | sh -s -- --install
  mkdir -p /mnt/s3files
  for i in $(seq 1 10); do
    mount -t s3files {s3files_fs_id} /mnt/s3files && break
    echo "Retry $i/10 for S3 Files mount..."
    sleep 10
  done
  chown ubuntu:ubuntu /mnt/s3files || chmod 777 /mnt/s3files
  touch "$STATE_DIR/mount-s3files.done"
  echo "STEP_OK:mount-s3files" >> "$SUMMARY"
fi
echo "Bootstrap complete."
"""

    # Launch EC2 instance
    run_kwargs = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [security_group_id],
                "AssociatePublicIpAddress": True,
            }
        ],
        "IamInstanceProfile": {"Arn": instance_profile_arn},
        "UserData": userdata,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": instance_name},
                    {"Key": "ImageName", "Value": image_name},
                    {"Key": "ManagedBy", "Value": "workstation-imagebuilder"},
                ],
            }
        ],
    }

    if disk_size_gb:
        run_kwargs["BlockDeviceMappings"] = [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": int(disk_size_gb),
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ]

    run_response = ec2.run_instances(**run_kwargs)

    instance_id = run_response["Instances"][0]["InstanceId"]

    # Wait for instance to be running so we can retrieve its public IP
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})

    desc = ec2.describe_instances(InstanceIds=[instance_id])
    public_ip = desc["Reservations"][0]["Instances"][0].get("PublicIpAddress", "")

    connect_function_name = os.environ["CONNECT_FUNCTION_NAME"]

    return {
        "instanceId": instance_id,
        "subnetId": subnet_id,
        "publicIp": public_ip,
        "waitCommand": f"aws ec2 wait instance-status-ok --instance-ids {instance_id} --region {region}",
        "connectCommand": (
            f"aws lambda invoke --function-name {connect_function_name} "
            f'--payload \'{{"instanceId":"{instance_id}"}}\' '
            f"--cli-binary-format raw-in-base64-out /dev/stdout"
        ),
    }
