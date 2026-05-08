import boto3
import json
import logging
import traceback
import urllib.request

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# CloudFormation response constants
SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Instance Type may used in HyperPod
INSTANCE_TYPES = [
    "g5.xlarge",
    "g5.2xlarge",
    "g5.4xlarge",
    "g5.8xlarge",
    "g5.12xlarge",
    "g5.16xlarge",
    "g5.24xlarge",
    "g5.48xlarge",
    "g6.xlarge",
    "g6.2xlarge",
    "g6.4xlarge",
    "g6.8xlarge",
    "g6.12xlarge",
    "g6.16xlarge",
    "g6.24xlarge",
    "g6.48xlarge",
    "g6e.xlarge",
    "g6e.2xlarge",
    "g6e.4xlarge",
    "g6e.8xlarge",
    "g6e.12xlarge",
    "g6e.16xlarge",
    "g6e.24xlarge",
    "g6e.48xlarge",
    "g7e.2xlarge",
    "g7e.4xlarge",
    "g7e.8xlarge",
    "g7e.12xlarge",
    "g7e.24xlarge",
    "g7e.48xlarge",
    # "p4d.24xlarge",
    # "p4de.24xlarge",
    # "p5.4xlarge",
    # "p5.48xlarge",
    # "p5e.48xlarge",
    # "p5en.48xlarge",
]


def send_cfn_response(
    event,
    context,
    response_status,
    response_data,
    physical_resource_id=None,
    no_echo=False,
    reason=None,
):
    """
    Send a response to CloudFormation regarding the success or failure of a custom resource deployment.
    """
    response_url = event["ResponseURL"]

    logger.info(f"CFN response URL: {response_url}")

    response_body = {
        "Status": response_status,
        "Reason": reason
        or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": no_echo,
        "Data": response_data,
    }

    json_response_body = json.dumps(response_body)
    logger.info(f"Response body: {json_response_body}")

    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(json_response_body)),
    }

    try:
        req = urllib.request.Request(
            url=response_url,
            data=json_response_body.encode("utf-8"),
            headers=headers,
            method="PUT",
        )

        with urllib.request.urlopen(req) as response:
            logger.info(f"Status code: {response.getcode()}")
            logger.info(f"Status message: {response.msg}")

    except Exception as e:
        logger.error(f"Error sending CFN response: {str(e)}")
        logger.error(f"Response URL: {response_url}")
        logger.error(f"Response body: {json_response_body}")


def handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")
    try:
        ec2_client = boto3.client("ec2")

        if event["RequestType"] == "Delete":
            send_cfn_response(event, context, SUCCESS, {"Message": "Skip for delete"})
            return

        selected_az = event["ResourceProperties"]["selectedAZ"]

        if selected_az:
            response_data = {"AvailabilityZone": selected_az}
            send_cfn_response(event, context, SUCCESS, response_data)
            return
        else:
            offerings = ec2_client.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[
                    {"Name": "instance-type", "Values": INSTANCE_TYPES},
                ],
            )

            # Group by AZ
            az_types = {}
            for item in offerings["InstanceTypeOfferings"]:
                az = item["Location"]
                if az not in az_types:
                    az_types[az] = set()
                az_types[az].add(item["InstanceType"])

            # Find AZ with all instance types
            for az, available_types in az_types.items():
                if set(INSTANCE_TYPES).issubset(available_types):
                    response_data = {"AvailabilityZone": az}
                    send_cfn_response(event, context, SUCCESS, response_data)
                    return

            send_cfn_response(
                event,
                context,
                FAILED,
                {},
                reason="No AZ found supporting all instance types",
            )

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(traceback.format_exc())
        send_cfn_response(event, context, FAILED, {"Error": str(e)})
