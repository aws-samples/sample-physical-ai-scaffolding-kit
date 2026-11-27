"""Shared pytest fixtures: moto backed AWS resources, a fixed clock, and poller settings."""

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import PENDING_INDEX

TEST_REGION = "us-west-2"
TEST_TABLE_NAME = "pask-vams-test-executions"
TEST_DATA_BUCKET = "pask-vams-test-data"
TEST_QUEUE_NAME = "pask-vams-test-queue"
FIXED_EPOCH_SECONDS = 1_790_000_000.0


@pytest.fixture(autouse=True)
def aws_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point boto3 at a fixed region with dummy credentials so no real account is used."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def aws_mock() -> Iterator[None]:
    """Run the test inside moto's ``mock_aws``."""
    with mock_aws():
        yield


@pytest.fixture
def dynamodb_client(aws_mock: None) -> Any:
    """Return a DynamoDB client backed by moto."""
    return boto3.client("dynamodb", region_name=TEST_REGION)


@pytest.fixture
def s3_client(aws_mock: None) -> Any:
    """Return an S3 client backed by moto."""
    return boto3.client("s3", region_name=TEST_REGION)


@pytest.fixture
def sqs_client(aws_mock: None) -> Any:
    """Return an SQS client backed by moto."""
    return boto3.client("sqs", region_name=TEST_REGION)


@pytest.fixture
def executions_table(dynamodb_client: Any) -> str:
    """Create the executions table with the sparse ``pending-index`` GSI.

    Returns:
        The table name.
    """
    dynamodb_client.create_table(
        TableName=TEST_TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pipelineExecutionId", "AttributeType": "S"},
            {"AttributeName": "pending", "AttributeType": "S"},
            {"AttributeName": "updatedAt", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pipelineExecutionId", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": PENDING_INDEX,
                "KeySchema": [
                    {"AttributeName": "pending", "KeyType": "HASH"},
                    {"AttributeName": "updatedAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    return TEST_TABLE_NAME


@pytest.fixture
def data_bucket(s3_client: Any) -> str:
    """Create the PASK data bucket.

    Returns:
        The bucket name.
    """
    s3_client.create_bucket(
        Bucket=TEST_DATA_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": TEST_REGION},
    )
    return TEST_DATA_BUCKET


@pytest.fixture
def queue_url(sqs_client: Any) -> str:
    """Create the VAMS request queue.

    Returns:
        The queue URL.
    """
    response = sqs_client.create_queue(
        QueueName=TEST_QUEUE_NAME,
        Attributes={"VisibilityTimeout": "300"},
    )
    return response["QueueUrl"]


@pytest.fixture
def fixed_clock() -> Callable[[], float]:
    """Return a clock that always reports the same epoch seconds."""
    return lambda: FIXED_EPOCH_SECONDS


@pytest.fixture
def poller_env() -> dict[str, str]:
    """Return a minimal ``PASK_VAMS_*`` environment that names the test resources."""
    return {
        "PASK_VAMS_QUEUE_URL": f"https://sqs.{TEST_REGION}.amazonaws.com/000000000000/"
        f"{TEST_QUEUE_NAME}",
        "PASK_VAMS_TABLE_NAME": TEST_TABLE_NAME,
        "PASK_VAMS_DATA_BUCKET": TEST_DATA_BUCKET,
        "PASK_VAMS_REGION": TEST_REGION,
        "PASK_VAMS_CONFIG_NAME": "test",
        "PASK_VAMS_LUSTRE_FS_PATH": "/s3link",
    }


@pytest.fixture
def poller_config(poller_env: dict[str, str]) -> PollerConfig:
    """Return a configuration built from ``poller_env`` with the default values."""
    return PollerConfig.from_env(poller_env)


@pytest.fixture
def training_env() -> dict[str, str]:
    """Return training environment values in the shape of the lerobot-openpi env file."""
    return {
        "LEROBOT_PI_BASE_DIR": "/fsx/ubuntu/lerobot-openpi",
        "LEROBOT_PI_LOG_DIR": "/fsx/ubuntu/lerobot-openpi/logs",
        "LEROBOT_PI_OUTPUT_DIR": "/fsx/ubuntu/lerobot-openpi/outputs",
        "LEROBOT_PI_CACHE_DIR": "/fsx/ubuntu/lerobot-openpi/cache",
        "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:<account-id>:mlflow-app/test",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }
