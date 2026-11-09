import boto3
import json
import logging
import os
import traceback
import urllib.parse
import urllib.request
import urllib.error

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# CloudFormation response constants
SUCCESS = "SUCCESS"
FAILED = "FAILED"


def require_https_url(url):
    """Raise ValueError unless the URL uses the https scheme.

    urllib.request.urlopen also opens file:// and other schemes, so every URL is
    checked before it is opened.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme != "https":
        raise ValueError(f"Refusing to open URL with scheme '{scheme}': only https is allowed")

fsx_client = boto3.client("fsx")


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
    require_https_url(response_url)

    logger.info(f"CFN response URL: {response_url}")

    response_body = {
        "Status": response_status,
        "Reason": reason
        or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id
        or context.log_stream_name,  # Noneの場合はlog_stream_nameをフォールバックとして使用
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


def handle_create(event, context):
    """Handle Create and Update events."""
    try:

        s3uri = f"s3://{event["ResourceProperties"]["S3BucketName"]}"
        if event["ResourceProperties"]["S3BucketPrefix"]:
            s3uri = f"{s3uri}/{event["ResourceProperties"]["S3BucketPrefix"]}"

        response = fsx_client.create_data_repository_association(
            FileSystemId=event["ResourceProperties"]["FileSystemId"],
            FileSystemPath=event["ResourceProperties"]["FileSystemPath"],
            DataRepositoryPath=s3uri,
            S3={
                "AutoImportPolicy": {
                    "Events": [
                        "NEW",
                        "CHANGED",
                        "DELETED",
                    ]
                },
                "AutoExportPolicy": {
                    "Events": [
                        "NEW",
                        "CHANGED",
                        "DELETED",
                    ]
                },
            },
        )
        logger.info(response)
        association_id = response["Association"]["AssociationId"]
        return True, "Successfully updated S3 link.", association_id

    except Exception as e:
        logger.error(f"Error in handle_create_update: {str(e)}")
        logger.error(traceback.format_exc())
        return False, str(e), None


def handle_delete(event, context):
    try:
        association_id = event.get("PhysicalResourceId")
        # Create失敗時はlog_stream_nameがPhysicalResourceIdになるためスキップ
        if not association_id or association_id == context.log_stream_name:
            return True, "No association to delete."

        fsx_client.delete_data_repository_association(
            AssociationId=association_id,
            DeleteDataInFileSystem=False,
        )
        return True, "Successfully deleted S3 link."

    except Exception as e:
        logger.error(f"Error in handle_delete: {str(e)}")
        logger.error(traceback.format_exc())
        return False, str(e)


def handler(event, context):
    """Main Lambda handler function."""
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        physical_resource_id = None

        if event["RequestType"] == "Create":
            success, message, physical_resource_id = handle_create(event, context)
        elif event["RequestType"] == "Update":
            success, message = True, "Nothing to do"
            physical_resource_id = event.get("PhysicalResourceId")
        elif event["RequestType"] == "Delete":
            success, message = handle_delete(event, context)
            physical_resource_id = event.get("PhysicalResourceId")
        else:
            success, message = False, f"Unsupported RequestType: {event['RequestType']}"

        status = SUCCESS if success else FAILED
        logger.info(f"Sending {status} response with message: {message}")
        send_cfn_response(
            event,
            context,
            status,
            {"Message": message},
            physical_resource_id=physical_resource_id,
        )

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(traceback.format_exc())
        send_cfn_response(event, context, FAILED, {"Error": str(e)})
