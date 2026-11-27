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

GITHUB_API_HOST = "api.github.com"
GITHUB_RAW_CONTENT_HOST = "raw.githubusercontent.com"


def require_https_url(url):
    """Raise ValueError unless the URL uses the https scheme.

    urllib.request.urlopen also opens file:// and other schemes, so every URL is
    checked before it is opened.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme != "https":
        raise ValueError(f"Refusing to open URL with scheme '{scheme}': only https is allowed")


def require_https_url_on_host(url, expected_host):
    """Raise ValueError unless the URL uses https and points at the expected host."""
    require_https_url(url)
    host = urllib.parse.urlparse(url).hostname
    if host != expected_host:
        raise ValueError(f"Refusing to open URL on host '{host}': expected '{expected_host}'")


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


def determine_content_type(file_name):
    """Determine content type based on file extension."""
    content_type = "text/plain"
    if file_name.endswith(".sh"):
        content_type = "text/x-sh"
    elif file_name.endswith(".py"):
        content_type = "text/x-python"
    elif file_name.endswith(".json"):
        content_type = "application/json"
    return content_type


def upload_file_to_s3(
    s3_client, bucket, bucket_path, file_content, s3_key, content_type
):
    """Upload a file to S3 bucket."""
    logger.info(f"Uploading {s3_key} to S3 bucket {bucket}")
    if bucket_path:
        key_prefix = f"{bucket_path}/"
    else:
        key_prefix = ""
    s3_client.put_object(
        Bucket=bucket,
        Key=f"{key_prefix}{s3_key}",
        Body=file_content,
        ContentType=content_type,
    )


def render_override(file_content):
    """Substitute deploy-time placeholders in a lifecycle override script.

    The scripts run on the cluster nodes and cannot read the CDK context, so the
    values they need are injected here from this function's environment.
    The VAMS poller variables are set only when the VAMS integration is enabled;
    otherwise their placeholders become empty and install_vams_poller.sh skips.
    """
    substitutions = {
        "__LUSTRE_FILESYSTEM_PATH__": os.environ.get("LUSTRE_FILESYSTEM_PATH", ""),
        "__LUSTRE_S3_PREFIX__": os.environ.get("LUSTRE_S3_PREFIX", ""),
        "__VAMS_ENABLED__": os.environ.get("VAMS_ENABLED", ""),
        "__LOGIN_GROUP_NAME__": os.environ.get("LOGIN_GROUP_NAME", ""),
        "__VAMS_QUEUE_URL__": os.environ.get("VAMS_QUEUE_URL", ""),
        "__VAMS_TABLE_NAME__": os.environ.get("VAMS_TABLE_NAME", ""),
        "__VAMS_DATA_BUCKET__": os.environ.get("VAMS_DATA_BUCKET", ""),
        "__VAMS_REGION__": os.environ.get("VAMS_REGION", ""),
        "__VAMS_CONFIG_NAME__": os.environ.get("VAMS_CONFIG_NAME", ""),
        "__VAMS_VISIBILITY_TIMEOUT_SECONDS__": os.environ.get(
            "VAMS_VISIBILITY_TIMEOUT_SECONDS", ""
        ),
        "__VAMS_HEARTBEAT_INTERVAL_SECONDS__": os.environ.get(
            "VAMS_HEARTBEAT_INTERVAL_SECONDS", ""
        ),
        "__VAMS_ABORT_GRACE_SECONDS__": os.environ.get("VAMS_ABORT_GRACE_SECONDS", ""),
        "__VAMS_LEASE_SECONDS__": os.environ.get("VAMS_LEASE_SECONDS", ""),
        "__VAMS_ITEM_TTL_DAYS__": os.environ.get("VAMS_ITEM_TTL_DAYS", ""),
        "__VAMS_INGEST_WAIT_SECONDS__": os.environ.get("VAMS_INGEST_WAIT_SECONDS", ""),
        "__VAMS_MAX_CONCURRENT_RUNS__": os.environ.get("VAMS_MAX_CONCURRENT_RUNS", ""),
        "__VAMS_SLURM_PARTITION__": os.environ.get("VAMS_SLURM_PARTITION", ""),
        "__VAMS_MLFLOW_EXPERIMENT_NAME__": os.environ.get(
            "VAMS_MLFLOW_EXPERIMENT_NAME", ""
        ),
    }
    text = file_content.decode("utf-8")
    for placeholder, value in substitutions.items():
        if placeholder in text:
            logger.info(f"Substituting {placeholder} with {value!r}")
            text = text.replace(placeholder, value)
    return text.encode("utf-8")


def upload_local_overrides(s3_client, bucket, bucket_path):
    """Upload local lifecycle script overrides after the upstream scripts."""
    overrides_dir = os.path.join(os.path.dirname(__file__), "overrides")
    if not os.path.isdir(overrides_dir):
        logger.info("No local lifecycle overrides found")
        return

    for root, _, files in os.walk(overrides_dir):
        rel_dir = os.path.relpath(root, overrides_dir)
        for file_name in files:
            file_path = os.path.join(root, file_name)
            if rel_dir == ".":
                s3_key = file_name
            else:
                s3_key = f"{rel_dir}/{file_name}"

            logger.info(f"Uploading local override {s3_key}")
            with open(file_path, "rb") as f:
                file_content = f.read()
            if file_name.endswith(".sh"):
                file_content = render_override(file_content)
            upload_file_to_s3(
                s3_client,
                bucket,
                bucket_path,
                file_content,
                s3_key,
                determine_content_type(file_name),
            )


def download_github_file(download_url):
    """Download file content from GitHub."""
    require_https_url_on_host(download_url, GITHUB_RAW_CONTENT_HOST)
    file_req = urllib.request.Request(download_url)
    file_req.add_header("User-Agent", "AWS-Lambda-SlurmLifecycleScriptLoader")

    with urllib.request.urlopen(file_req) as file_response:
        return file_response.read()


def process_directory(s3_client, bucket, bucket_path, repo_url, branch, path, prefix):
    """Process a directory recursively and upload all files to S3."""
    api_url = f"https://api.github.com/repos/{repo_url.split('github.com/')[1]}/contents/{path}?ref={branch}"
    logger.info(f"Fetching directory contents from: {api_url}")

    require_https_url_on_host(api_url, GITHUB_API_HOST)
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "AWS-Lambda-SlurmLifecycleScriptLoader")

    try:
        with urllib.request.urlopen(req) as response:
            contents = json.loads(response.read().decode("utf-8"))

        for item in contents:
            if item["type"] == "file":
                file_name = item["name"]
                download_url = item["download_url"]
                s3_key = f"{prefix}/{file_name}"

                logger.info(f"Downloading {file_name} from {download_url}")
                file_content = download_github_file(download_url)
                content_type = determine_content_type(file_name)
                upload_file_to_s3(
                    s3_client, bucket, bucket_path, file_content, s3_key, content_type
                )

            elif item["type"] == "dir":
                dir_name = item["name"]
                new_path = f"{path}/{dir_name}"
                new_prefix = f"{prefix}/{dir_name}"

                logger.info(
                    f"Found nested directory: {dir_name}, processing recursively"
                )
                process_directory(
                    s3_client,
                    bucket,
                    bucket_path,
                    repo_url,
                    branch,
                    new_path,
                    new_prefix,
                )

    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error processing directory {path}: {e.code} - {e.reason}")
        raise


def delete_s3_objects_recursively(s3_client, bucket, prefix):
    """Delete all objects under a specific prefix recursively."""
    try:
        # List all objects with the given prefix
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        objects_to_delete = []
        for page in pages:
            if "Contents" in page:
                for obj in page["Contents"]:
                    objects_to_delete.append({"Key": obj["Key"]})

        if objects_to_delete:
            # Delete objects in batches of 1000 (AWS limit)
            for i in range(0, len(objects_to_delete), 1000):
                batch = objects_to_delete[i : i + 1000]
                logger.info(f"Deleting batch of {len(batch)} objects")
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            logger.info(f"Successfully deleted {len(objects_to_delete)} objects")
        else:
            logger.info("No objects found to delete")

    except Exception as e:
        logger.error(f"Error deleting objects: {str(e)}")
        raise


def handle_create_update(event, context):
    """Handle Create and Update request types."""
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET_NAME"]
    bucket_path = os.environ.get("BUCKET_PATH", "")
    repo_url = os.environ["GITHUB_REPO_URL"]
    branch = os.environ["GITHUB_BRANCH"]
    path = os.environ["GITHUB_PATH"].strip("/")

    api_url = f"https://api.github.com/repos/{repo_url.split('github.com/')[1]}/contents/{path}?ref={branch}"
    logger.info(f"Fetching repository contents from: {api_url}")

    try:
        s3.head_bucket(Bucket=bucket)
        require_https_url_on_host(api_url, GITHUB_API_HOST)
        req = urllib.request.Request(api_url)
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("User-Agent", "AWS-Lambda-SlurmLifecycleScriptLoader")

        with urllib.request.urlopen(req) as response:
            contents = json.loads(response.read().decode("utf-8"))

        for item in contents:
            if item["type"] == "file":
                file_name = item["name"]
                download_url = item["download_url"]
                file_content = download_github_file(download_url)
                content_type = determine_content_type(file_name)
                upload_file_to_s3(
                    s3, bucket, bucket_path, file_content, file_name, content_type
                )

            elif item["type"] == "dir":
                dir_name = item["name"]
                process_directory(
                    s3,
                    bucket,
                    bucket_path,
                    repo_url,
                    branch,
                    f"{path}/{dir_name}",
                    dir_name,
                )
        upload_local_overrides(s3, bucket, bucket_path)
        return True, "Files uploaded successfully"

    except s3.exceptions.NoSuchBucket:
        logger.error(f"Bucket {bucket} does not exist")
        return False, "Bucket does not exist"
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP Error: {e.code} - {e.reason}")
        return False, f"HTTP Error: {e.code} - {e.reason}"
    except Exception as e:
        logger.error(f"Error uploading files: {str(e)}")
        return False, f"Error uploading files: {str(e)}"


def handler(event, context):
    """Main Lambda handler function."""
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        if event["RequestType"] in ["Create", "Update"]:
            success, message = handle_create_update(event, context)
        elif event["RequestType"] == "Delete":
            success = SUCCESS
            message = "Bucket will be removed. Skip delete objects."
        else:
            success, message = False, f"Unsupported RequestType: {event['RequestType']}"

        status = SUCCESS if success else FAILED
        logger.info(f"Sending {status} response with message: {message}")
        send_cfn_response(event, context, status, {"Message": message})

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        logger.error(traceback.format_exc())
        send_cfn_response(event, context, FAILED, {"Error": str(e)})
