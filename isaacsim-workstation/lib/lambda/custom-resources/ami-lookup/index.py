import boto3
import json
import logging
import traceback
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send_cfn_response(
    event,
    context,
    response_status,
    response_data,
    physical_resource_id=None,
    no_echo=False,
    reason=None,
):
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
    except Exception as e:
        logger.error(f"Error sending CFN response: {str(e)}")


def handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")
    try:
        if event["RequestType"] == "Delete":
            send_cfn_response(event, context, SUCCESS, {"Message": "Skip for delete"})
            return

        ami_name = event["ResourceProperties"]["AmiName"]
        ec2_client = boto3.client("ec2")

        response = ec2_client.describe_images(
            Filters=[{"Name": "name", "Values": [ami_name]}],
        )

        images = response.get("Images", [])
        if not images:
            send_cfn_response(
                event,
                context,
                FAILED,
                {},
                reason=f"No AMI found with name: {ami_name}",
            )
            return

        # Sort by creation date descending to get the latest if multiple matches
        images.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
        ami_id = images[0]["ImageId"]
        logger.info(f"Found AMI: {ami_id} (name: {ami_name})")

        send_cfn_response(event, context, SUCCESS, {"AmiId": ami_id})

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(traceback.format_exc())
        send_cfn_response(event, context, FAILED, {"Error": str(e)})
