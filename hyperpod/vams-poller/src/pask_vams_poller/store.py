"""DynamoDB execution items: conditional state transitions, leases, and concurrency slots.

Every write is a conditional ``UpdateItem`` (or ``TransactWriteItems`` when the
concurrency counter item changes together with an execution item). A failed condition
raises ``StaleItemError`` so that the caller reads the item again and decides what to do.
"""

import dataclasses
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from pask_vams_poller.constants import (
    CONCURRENCY_ITEM_ID,
    MAX_CAUSE_LENGTH,
    NON_TERMINAL_STATES,
    PENDING_ACTIVE,
    PENDING_CLEANUP,
    PENDING_INDEX,
    PIPELINE_EXECUTION_ID_PATTERN,
    TERMINAL_STATES,
    ErrorName,
    State,
)
from pask_vams_poller.errors import RequestMismatchError, StaleItemError, TransientError

PARTITION_KEY = "pipelineExecutionId"
CONCURRENCY_ACTIVE_ATTR = "active"

OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_FAILED = "FAILED"
OUTCOME_STATUSES: frozenset[str] = frozenset({OUTCOME_SUCCEEDED, OUTCOME_FAILED})

NOTIFY_ACCEPTED = "ACCEPTED"
NOTIFY_ALREADY_CLOSED = "ALREADY_CLOSED"
NOTIFY_RESULTS: frozenset[str] = frozenset({NOTIFY_ACCEPTED, NOTIFY_ALREADY_CLOSED})

SECONDS_PER_DAY = 86400
ISO8601_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

FINALIZING_SOURCE_STATES: frozenset[State] = frozenset(
    {State.RECEIVED, State.STAGING, State.SUBMITTING, State.SUBMITTED, State.RUNNING}
)
ABORT_SUSPECTABLE_STATES: frozenset[State] = NON_TERMINAL_STATES - {State.ABORT_SUSPECTED}

REQUEST_IDENTITY_FIELDS: tuple[str, ...] = (
    "workflowDatabaseId",
    "workflowId",
    "workflowExecutionId",
    "inputManifestS3Location",
)

RECEIVED_INPUT_FIELDS: frozenset[str] = frozenset(
    {
        "pipelineExecutionId",
        "workflowDatabaseId",
        "workflowId",
        "workflowExecutionId",
        "inputManifestS3Location",
        "inputConfigurationS3Location",
        "inputMetadataS3Location",
        "executingUserName",
    }
)

MANAGED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "pipelineExecutionId",
        "state",
        "version",
        "taskToken",
        "tokenSentAt",
        "leaseOwner",
        "leaseExpiresAt",
        "outcome",
        "notifyResult",
        "prevState",
        "suspectedAt",
        "createdAt",
        "updatedAt",
        "terminalAt",
        "pending",
        "ttl",
        "holdsSlot",
        "resendPending",
    }
)

CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
TRANSACTION_CANCELED = "TransactionCanceledException"
CANCELLATION_CONDITION_FAILED = "ConditionalCheckFailed"
CANCELLATION_TRANSACTION_CONFLICT = "TransactionConflict"
REPLACE_TOKEN_ATTEMPTS = 3

ExtraCondition = tuple[str, Mapping[str, str], Mapping[str, Any]]

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


@dataclasses.dataclass
class ExecutionItem:
    """One VAMS pipeline execution as stored in the executions table.

    Attribute names match the DynamoDB attribute names. ``tokenSentAt`` is the SQS
    ``SentTimestamp`` in epoch milliseconds. ``leaseExpiresAt``, ``suspectedAt``, and
    ``ttl`` are epoch seconds. ``createdAt``, ``updatedAt``, and ``terminalAt`` are
    ISO 8601 UTC strings. ``outcome`` is ``{"status", "errorName", "cause"}``.
    """

    pipelineExecutionId: str
    state: State
    version: int
    taskToken: str | None = None
    tokenSentAt: int | None = None
    leaseOwner: str | None = None
    leaseExpiresAt: int | None = None
    workflowDatabaseId: str | None = None
    workflowId: str | None = None
    workflowExecutionId: str | None = None
    inputManifestS3Location: str | None = None
    inputConfigurationS3Location: str | None = None
    inputMetadataS3Location: str | None = None
    executingUserName: str | None = None
    slurmJobId: str | None = None
    jobName: str | None = None
    stagingPrefix: str | None = None
    runDir: str | None = None
    outputDir: str | None = None
    outcome: dict[str, str | None] | None = None
    notifyResult: str | None = None
    outputs: list[str] | None = None
    mlflowRunId: str | None = None
    mlflowExperimentId: str | None = None
    prevState: State | None = None
    suspectedAt: int | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    terminalAt: str | None = None
    pending: str | None = None
    ttl: int | None = None
    holdsSlot: bool = False
    resendPending: bool = False

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "ExecutionItem":
        """Build an execution from a DynamoDB item in the low level attribute value format.

        Args:
            item: Item as returned by the DynamoDB client, for example ``{"state": {"S": ...}}``.

        Returns:
            The execution. Attributes that the dataclass does not know are ignored.

        Raises:
            ValueError: A required attribute is missing.
        """
        plain = {
            name: _from_dynamo_value(_deserializer.deserialize(value))
            for name, value in item.items()
        }
        for required in ("pipelineExecutionId", "state", "version"):
            if required not in plain:
                raise ValueError(f"DynamoDB item has no {required}: {sorted(plain)}")
        known = {field.name for field in dataclasses.fields(cls)}
        values = {name: value for name, value in plain.items() if name in known}
        values["state"] = State(values["state"])
        if values.get("prevState") is not None:
            values["prevState"] = State(values["prevState"])
        if values.get("outcome") is not None:
            values["outcome"] = _normalize_stored_outcome(values["outcome"])
        return cls(**values)

    def to_item(self) -> dict[str, Any]:
        """Convert the execution to a DynamoDB item in the low level attribute value format.

        Attributes whose value is None are left out. ``holdsSlot`` and ``resendPending``
        are written only when True, so that their absence means False.

        Returns:
            The item for ``PutItem``.
        """
        item: dict[str, Any] = {}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value is None or value is False:
                continue
            if field.name == "outcome":
                value = {key: entry for key, entry in value.items() if entry is not None}
            item[field.name] = _to_dynamo_value(value)
        return item


def format_iso8601_utc(epoch_seconds: float) -> str:
    """Format epoch seconds as an ISO 8601 UTC string.

    Args:
        epoch_seconds: Seconds since the Unix epoch.

    Returns:
        ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(ISO8601_UTC_FORMAT)


def truncate_cause(cause: str) -> str:
    """Cut a failure cause to the length that ``SendTaskFailure`` accepts.

    Args:
        cause: Human readable failure reason.

    Returns:
        The first ``MAX_CAUSE_LENGTH`` characters of ``cause``.
    """
    return cause[:MAX_CAUSE_LENGTH]


def build_outcome(status: str, error_name: str | None, cause: str | None) -> dict[str, str | None]:
    """Build a validated ``outcome`` map.

    Args:
        status: ``SUCCEEDED`` or ``FAILED``.
        error_name: One of the ``ErrorName`` values. Required when ``status`` is FAILED.
        cause: Failure reason. Required when ``status`` is FAILED. Truncated to
            ``MAX_CAUSE_LENGTH``.

    Returns:
        ``{"status", "errorName", "cause"}``.

    Raises:
        ValueError: The status is unknown, or a FAILED outcome has no error name or cause.
    """
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"Unknown outcome status: {status!r}")
    if status == OUTCOME_FAILED and (error_name is None or cause is None):
        raise ValueError("A FAILED outcome needs both errorName and cause")
    return {
        "status": status,
        "errorName": None if error_name is None else ErrorName(error_name).value,
        "cause": None if cause is None else truncate_cause(cause),
    }


def _normalize_stored_outcome(outcome: Mapping[str, Any]) -> dict[str, str | None]:
    """Return a stored outcome map with all three keys present.

    Args:
        outcome: Outcome map read from DynamoDB, which omits null entries.

    Returns:
        ``{"status", "errorName", "cause"}`` with None for missing entries.
    """
    return {
        "status": outcome.get("status"),
        "errorName": outcome.get("errorName"),
        "cause": outcome.get("cause"),
    }


def _to_dynamo_value(value: Any) -> dict[str, Any]:
    """Serialize a Python value to a DynamoDB attribute value.

    Args:
        value: str, int, bool, State, list, or dict. Enums are stored as their value.

    Returns:
        The attribute value, for example ``{"S": "RECEIVED"}``.
    """
    return _serializer.serialize(_plain_value(value))


def _plain_value(value: Any) -> Any:
    """Replace enums with their values, recursively through lists and dicts.

    Args:
        value: Value to store.

    Returns:
        A value that ``TypeSerializer`` accepts.
    """
    if isinstance(value, State | ErrorName):
        return value.value
    if isinstance(value, list):
        return [_plain_value(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _plain_value(entry) for key, entry in value.items()}
    return value


def _from_dynamo_value(value: Any) -> Any:
    """Convert ``Decimal`` numbers read from DynamoDB to ``int``, recursively.

    Args:
        value: Value returned by ``TypeDeserializer``.

    Returns:
        The value with integral numbers as ``int``.

    Raises:
        ValueError: A number is not integral. The table stores only integers.
    """
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"Non integral number in DynamoDB item: {value}")
        return int(value)
    if isinstance(value, list):
        return [_from_dynamo_value(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _from_dynamo_value(entry) for key, entry in value.items()}
    return value


def _validate_pipeline_execution_id(pipeline_execution_id: str) -> None:
    """Check that a pipeline execution id is safe to use in keys, paths, and job names.

    Args:
        pipeline_execution_id: Id to check.

    Raises:
        ValueError: The id does not match ``PIPELINE_EXECUTION_ID_PATTERN``.
    """
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(pipeline_execution_id):
        raise ValueError(f"Invalid pipelineExecutionId: {pipeline_execution_id!r}")


def _is_conditional_check_failure(error: ClientError) -> bool:
    """Tell whether a client error is a failed ConditionExpression.

    Args:
        error: Error raised by the DynamoDB client.

    Returns:
        True for ``ConditionalCheckFailedException``.
    """
    return error.response.get("Error", {}).get("Code") == CONDITIONAL_CHECK_FAILED


def _cancellation_codes(error: ClientError) -> list[str] | None:
    """Return the per item cancellation codes of a canceled transaction.

    Args:
        error: Error raised by ``TransactWriteItems``.

    Returns:
        One code per transaction item, or None when the error is not a cancellation.
    """
    if error.response.get("Error", {}).get("Code") != TRANSACTION_CANCELED:
        return None
    reasons = error.response.get("CancellationReasons", [])
    return [reason.get("Code", "None") for reason in reasons]


class _Expression:
    """Collect attribute name and value placeholders for one DynamoDB request."""

    def __init__(self) -> None:
        """Start with no placeholders."""
        self.names: dict[str, str] = {}
        self.values: dict[str, Any] = {}
        self.set_clauses: list[str] = []
        self.remove_clauses: list[str] = []
        self.conditions: list[str] = []

    def name(self, attribute: str) -> str:
        """Return the placeholder ``#<attribute>`` for an attribute name.

        Args:
            attribute: Attribute name made of letters, digits, and underscores.

        Returns:
            The name placeholder.
        """
        placeholder = f"#{attribute}"
        self._add_name(placeholder, attribute)
        return placeholder

    def value(self, key: str, value: Any) -> str:
        """Register a value under the placeholder ``:<key>``.

        Args:
            key: Placeholder name without the colon. Must be unique in the request.
            value: Python value to serialize.

        Returns:
            The value placeholder.

        Raises:
            ValueError: The placeholder is already used.
        """
        placeholder = f":{key}"
        if placeholder in self.values:
            raise ValueError(f"Duplicate expression value placeholder {placeholder}")
        self.values[placeholder] = _to_dynamo_value(value)
        return placeholder

    def set(self, attribute: str, value: Any) -> None:
        """Add ``SET #attribute = :set_attribute``.

        Args:
            attribute: Attribute to set.
            value: New value.
        """
        self.set_clauses.append(f"{self.name(attribute)} = {self.value(f'set_{attribute}', value)}")

    def remove(self, attribute: str) -> None:
        """Add ``REMOVE #attribute``.

        Args:
            attribute: Attribute to remove.
        """
        self.remove_clauses.append(self.name(attribute))

    def add_condition(self, extra: ExtraCondition) -> None:
        """AND a caller supplied condition with its own placeholders.

        Args:
            extra: ``(expression, names, values)``. Values are Python values.

        Raises:
            ValueError: A placeholder conflicts with one that is already registered.
        """
        expression, names, values = extra
        for placeholder, attribute in names.items():
            self._add_name(placeholder, attribute)
        for placeholder, value in values.items():
            if placeholder in self.values:
                raise ValueError(f"Extra condition reuses value placeholder {placeholder}")
            self.values[placeholder] = _to_dynamo_value(value)
        self.conditions.append(f"({expression})")

    def update_expression(self) -> str:
        """Join the SET and REMOVE clauses.

        Returns:
            The ``UpdateExpression``.
        """
        parts = []
        if self.set_clauses:
            parts.append("SET " + ", ".join(self.set_clauses))
        if self.remove_clauses:
            parts.append("REMOVE " + ", ".join(self.remove_clauses))
        return " ".join(parts)

    def condition_expression(self) -> str:
        """Join the conditions with AND.

        Returns:
            The ``ConditionExpression``.
        """
        return " AND ".join(self.conditions)

    def update_request(self, table_name: str, key: Mapping[str, Any]) -> dict[str, Any]:
        """Build the shared parameters of ``UpdateItem`` and a transaction ``Update``.

        Args:
            table_name: Table to update.
            key: Primary key in attribute value format.

        Returns:
            Request parameters without ``ReturnValues``.
        """
        request: dict[str, Any] = {
            "TableName": table_name,
            "Key": dict(key),
            "UpdateExpression": self.update_expression(),
            "ExpressionAttributeNames": self.names,
        }
        if self.conditions:
            request["ConditionExpression"] = self.condition_expression()
        if self.values:
            request["ExpressionAttributeValues"] = self.values
        return request

    def _add_name(self, placeholder: str, attribute: str) -> None:
        """Register a name placeholder.

        Args:
            placeholder: ``#name`` placeholder.
            attribute: Attribute it stands for.

        Raises:
            ValueError: The placeholder already stands for another attribute.
        """
        existing = self.names.get(placeholder)
        if existing is not None and existing != attribute:
            raise ValueError(
                f"Name placeholder {placeholder} maps to both {existing} and {attribute}"
            )
        self.names[placeholder] = attribute


class ExecutionStore:
    """Read and conditionally update execution items in the executions table."""

    def __init__(
        self,
        table_name: str,
        *,
        region: str,
        owner_id: str,
        lease_seconds: int,
        ttl_days: int,
        clock: Callable[[], float] = time.time,
        dynamodb_client: Any = None,
    ) -> None:
        """Create the store.

        Args:
            table_name: Executions table name.
            region: AWS region of the table.
            owner_id: Lease owner id of this poller process.
            lease_seconds: Lease length in seconds. Must be positive.
            ttl_days: Days after ``terminalAt`` at which DynamoDB TTL deletes the item.
                Must be positive.
            clock: Function that returns the current epoch seconds.
            dynamodb_client: DynamoDB client. A new one for ``region`` is created when None.

        Raises:
            ValueError: An argument is empty or not positive.
        """
        if not table_name:
            raise ValueError("table_name must not be empty")
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")
        if ttl_days <= 0:
            raise ValueError(f"ttl_days must be positive, got {ttl_days}")
        self.table_name = table_name
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.ttl_days = ttl_days
        self._clock = clock
        self._client = dynamodb_client or boto3.client("dynamodb", region_name=region)

    def create_received(
        self, fields: Mapping[str, str], task_token: str, token_sent_at: int
    ) -> bool:
        """Create a new execution in RECEIVED.

        Args:
            fields: ``pipelineExecutionId`` and the message and manifest values listed in
                ``RECEIVED_INPUT_FIELDS``.
            task_token: Step Functions task token of the message.
            token_sent_at: SQS ``SentTimestamp`` of the message in epoch milliseconds.

        Returns:
            True when the item was created, False when it already exists.

        Raises:
            ValueError: ``fields`` has an unknown key or an invalid id.
            TypeError: A value in ``fields`` is not a string.
        """
        unknown = set(fields) - RECEIVED_INPUT_FIELDS
        if unknown:
            raise ValueError(f"Unknown fields for a received execution: {sorted(unknown)}")
        if "pipelineExecutionId" not in fields:
            raise ValueError("fields must contain pipelineExecutionId")
        for name, value in fields.items():
            if not isinstance(value, str):
                raise TypeError(f"Field {name} must be a string, got {type(value).__name__}")
        _validate_pipeline_execution_id(fields["pipelineExecutionId"])
        now_iso = format_iso8601_utc(self._clock())
        item = ExecutionItem(
            state=State.RECEIVED,
            version=1,
            taskToken=task_token,
            tokenSentAt=token_sent_at,
            createdAt=now_iso,
            updatedAt=now_iso,
            pending=PENDING_ACTIVE,
            **fields,
        )
        try:
            self._client.put_item(
                TableName=self.table_name,
                Item=item.to_item(),
                ConditionExpression="attribute_not_exists(#pipelineExecutionId)",
                ExpressionAttributeNames={"#pipelineExecutionId": PARTITION_KEY},
            )
        except ClientError as error:
            if _is_conditional_check_failure(error):
                return False
            raise
        return True

    def replace_token(
        self,
        pipeline_execution_id: str,
        task_token: str,
        token_sent_at: int,
        request_identity: Mapping[str, str],
    ) -> ExecutionItem | None:
        """Replace the task token with the token of a newer message of the same request.

        The version is incremented so that the poller working on the item fails its next
        conditional update and reads the new token. On a terminal execution the same
        update sets ``resendPending`` and puts the item back into ``pending=CLEANUP`` so
        that the reconciler sends the stored outcome again with the new token.

        Args:
            pipeline_execution_id: Execution to update.
            task_token: New task token.
            token_sent_at: SQS ``SentTimestamp`` of the new message in epoch milliseconds.
            request_identity: The ``REQUEST_IDENTITY_FIELDS`` of the new message. The token
                is replaced only when they equal the stored values.

        Returns:
            The updated execution, or None when the item is missing or its token is from
            the same or a newer message.

        Raises:
            ValueError: ``request_identity`` does not have exactly the identity fields.
            RequestMismatchError: The stored execution belongs to another workflow request.
            StaleItemError: Concurrent updates kept changing the item.
        """
        _validate_pipeline_execution_id(pipeline_execution_id)
        if set(request_identity) != set(REQUEST_IDENTITY_FIELDS):
            raise ValueError(
                f"request_identity must have exactly {', '.join(REQUEST_IDENTITY_FIELDS)}"
            )
        for _ in range(REPLACE_TOKEN_ATTEMPTS):
            current = self.get(pipeline_execution_id)
            if current is None:
                return None
            mismatched = [
                name
                for name in REQUEST_IDENTITY_FIELDS
                if getattr(current, name) != request_identity[name]
            ]
            if mismatched:
                raise RequestMismatchError(
                    f"Execution {pipeline_execution_id} belongs to another request: "
                    f"{', '.join(mismatched)} differ"
                )
            if current.tokenSentAt is None:
                return None
            if current.tokenSentAt >= token_sent_at:
                return None
            is_terminal = current.state in TERMINAL_STATES
            request = self._replace_token_request(
                pipeline_execution_id, task_token, token_sent_at, is_terminal, request_identity
            )
            try:
                response = self._client.update_item(**request, ReturnValues="ALL_NEW")
            except ClientError as error:
                if _is_conditional_check_failure(error):
                    continue
                raise
            return ExecutionItem.from_item(response["Attributes"])
        raise StaleItemError(f"Could not replace the task token of {pipeline_execution_id}")

    def get(self, pipeline_execution_id: str, consistent: bool = True) -> ExecutionItem | None:
        """Read one execution.

        Args:
            pipeline_execution_id: Execution to read.
            consistent: Use a strongly consistent read.

        Returns:
            The execution, or None when it does not exist.
        """
        _validate_pipeline_execution_id(pipeline_execution_id)
        response = self._client.get_item(
            TableName=self.table_name,
            Key=self._key(pipeline_execution_id),
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        return None if item is None else ExecutionItem.from_item(item)

    def query_pending(self, pending: str) -> list[str]:
        """List the executions with the given ``pending`` value from ``pending-index``.

        The index is eventually consistent, so callers read each item again with ``get``.

        Args:
            pending: ``ACTIVE`` or ``CLEANUP``.

        Returns:
            Pipeline execution ids ordered by ``updatedAt``.
        """
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self.table_name,
            IndexName=PENDING_INDEX,
            KeyConditionExpression="#pending = :pending",
            ProjectionExpression="#pipelineExecutionId",
            ExpressionAttributeNames={"#pending": "pending", "#pipelineExecutionId": PARTITION_KEY},
            ExpressionAttributeValues={":pending": {"S": pending}},
        )
        return [item[PARTITION_KEY]["S"] for page in pages for item in page.get("Items", [])]

    def active_runs(self) -> int:
        """Read the number of executions that hold a concurrency slot.

        Returns:
            The ``active`` value of the counter item, or 0 when the item does not exist.
        """
        response = self._client.get_item(
            TableName=self.table_name,
            Key={PARTITION_KEY: {"S": CONCURRENCY_ITEM_ID}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None or CONCURRENCY_ACTIVE_ATTR not in item:
            return 0
        return int(item[CONCURRENCY_ACTIVE_ATTR]["N"])

    def acquire_lease(self, pipeline_execution_id: str) -> ExecutionItem | None:
        """Take the lease of an execution that is free, expired, or already ours.

        Args:
            pipeline_execution_id: Execution to lease.

        Returns:
            The execution with the new lease, or None when another owner holds the lease.
        """
        _validate_pipeline_execution_id(pipeline_execution_id)
        now = self._now_seconds()
        expression = _Expression()
        lease_owner = expression.name("leaseOwner")
        lease_expires_at = expression.name("leaseExpiresAt")
        me = expression.value("me", self.owner_id)
        expression.set_clauses.append(f"{lease_owner} = {me}")
        expression.set("leaseExpiresAt", now + self.lease_seconds)
        expression.conditions.append(f"attribute_exists({expression.name(PARTITION_KEY)})")
        expression.conditions.append(
            f"(attribute_not_exists({lease_owner}) OR {lease_expires_at} < "
            f"{expression.value('now', now)} OR {lease_owner} = {me})"
        )
        try:
            response = self._client.update_item(
                **expression.update_request(self.table_name, self._key(pipeline_execution_id)),
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _is_conditional_check_failure(error):
                return None
            raise
        return ExecutionItem.from_item(response["Attributes"])

    def renew_lease(self, item: ExecutionItem) -> bool:
        """Extend the lease that this poller holds.

        Args:
            item: Execution whose lease to extend.

        Returns:
            True when the lease was extended, False when another owner holds it.
        """
        expression = _Expression()
        expression.set("leaseExpiresAt", self._now_seconds() + self.lease_seconds)
        expression.conditions.append(
            f"{expression.name('leaseOwner')} = {expression.value('me', self.owner_id)}"
        )
        return self._update_if(expression, item.pipelineExecutionId)

    def release_lease(self, item: ExecutionItem) -> bool:
        """Remove the lease that this poller holds.

        Args:
            item: Execution whose lease to release.

        Returns:
            True when the lease was removed, False when another owner holds it.
        """
        expression = _Expression()
        expression.remove("leaseOwner")
        expression.remove("leaseExpiresAt")
        expression.conditions.append(
            f"{expression.name('leaseOwner')} = {expression.value('me', self.owner_id)}"
        )
        return self._update_if(expression, item.pipelineExecutionId)

    def transition(
        self,
        item: ExecutionItem,
        to_state: State,
        *,
        set_attrs: Mapping[str, Any] | None = None,
        remove_attrs: Iterable[str] = (),
        extra_condition: ExtraCondition | None = None,
    ) -> ExecutionItem:
        """Move an execution to another state under the common condition ``C(item.state)``.

        ``C(s)`` is ``#state = :s AND #version = :v AND leaseOwner = :me AND
        leaseExpiresAt > :now``. The version is incremented and ``updatedAt`` is set.
        Attributes that the store manages (state, version, lease, token, outcome,
        terminal and cleanup markers) cannot be set here; use the dedicated methods.

        Args:
            item: Execution as last read by the caller.
            to_state: Target state. Terminal states and RECEIVED to STAGING have dedicated
                methods and are rejected.
            set_attrs: Additional attributes to set.
            remove_attrs: Additional attributes to remove.
            extra_condition: ``(expression, names, values)`` ANDed with ``C(s)``. Values
                are Python values.

        Returns:
            The updated execution.

        Raises:
            ValueError: The target state or an attribute is not allowed here.
            StaleItemError: The condition failed.
        """
        if to_state in TERMINAL_STATES:
            raise ValueError(f"Use the dedicated method to move to {to_state.value}")
        if item.state is State.RECEIVED and to_state is State.STAGING:
            raise ValueError("Use start_staging to move from RECEIVED to STAGING")
        set_values = dict(set_attrs or {})
        removed = list(remove_attrs)
        _check_caller_attributes(set_values, removed)
        return self._transition(item, to_state, set_values, removed, extra_condition)

    def start_staging(
        self, item: ExecutionItem, set_attrs: Mapping[str, Any], max_concurrent: int
    ) -> bool:
        """Move RECEIVED to STAGING while taking a concurrency slot.

        The counter item ``#CONCURRENCY`` is incremented under ``active < :max`` (a missing
        counter counts as 0) in the same transaction that moves the execution under
        ``C(RECEIVED)`` and marks it with ``holdsSlot=true``.

        Args:
            item: Execution in RECEIVED.
            set_attrs: Attributes to set, for example ``stagingPrefix``, ``runDir``, and
                ``outputDir``.
            max_concurrent: Maximum number of executions that hold a slot. Must be positive.

        Returns:
            True when the execution moved to STAGING, False when all slots are taken.

        Raises:
            ValueError: ``max_concurrent`` is not positive or an attribute is not allowed.
            StaleItemError: The execution item condition failed.
            TransientError: The transaction conflicted with another transaction.
        """
        if max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be positive, got {max_concurrent}")
        set_values = dict(set_attrs)
        _check_caller_attributes(set_values, [])
        counter = _Expression()
        active = counter.name(CONCURRENCY_ACTIVE_ATTR)
        zero = counter.value("zero", 0)
        counter.set_clauses.append(
            f"{active} = if_not_exists({active}, {zero}) + {counter.value('one', 1)}"
        )
        counter.conditions.append(
            f"(attribute_not_exists({active}) OR {active} < {counter.value('max', max_concurrent)})"
        )
        execution = self._transition_expression(item, State.STAGING, set_values, [], None)
        holds_slot = execution.name("holdsSlot")
        execution.set_clauses.append(f"{holds_slot} = {execution.value('holdsSlotTrue', True)}")
        execution.conditions.append(f"attribute_not_exists({holds_slot})")
        codes = self._transact_updates(
            [
                counter.update_request(
                    self.table_name, {PARTITION_KEY: {"S": CONCURRENCY_ITEM_ID}}
                ),
                execution.update_request(self.table_name, self._key(item.pipelineExecutionId)),
            ]
        )
        if codes is None:
            return True
        counter_code, execution_code = codes
        if execution_code == CANCELLATION_CONDITION_FAILED:
            raise StaleItemError(
                f"{item.pipelineExecutionId} is no longer RECEIVED v{item.version}"
            )
        if counter_code == CANCELLATION_CONDITION_FAILED:
            return False
        raise AssertionError(f"Unexpected cancellation codes: {codes}")

    def to_finalizing(
        self,
        item: ExecutionItem,
        outcome: Mapping[str, str | None],
        set_attrs: Mapping[str, Any] | None = None,
    ) -> ExecutionItem:
        """Move an execution to FINALIZING with its decided outcome.

        Args:
            item: Execution in RECEIVED, STAGING, SUBMITTING, SUBMITTED, or RUNNING.
            outcome: ``{"status", "errorName", "cause"}``. FAILED needs errorName and cause.
            set_attrs: Additional attributes, for example ``mlflowRunId``.

        Returns:
            The updated execution.

        Raises:
            ValueError: The source state or the outcome is not valid.
            StaleItemError: The condition failed.
        """
        if item.state not in FINALIZING_SOURCE_STATES:
            raise ValueError(f"Cannot move {item.state.value} to FINALIZING")
        set_values = dict(set_attrs or {})
        _check_caller_attributes(set_values, [])
        set_values["outcome"] = _stored_outcome(
            build_outcome(outcome.get("status"), outcome.get("errorName"), outcome.get("cause"))
        )
        return self._transition(item, State.FINALIZING, set_values, [], None)

    def record_outputs(self, item: ExecutionItem, outputs: list[str]) -> ExecutionItem:
        """Record the S3 keys written back to VAMS, staying in FINALIZING.

        Args:
            item: Execution in FINALIZING.
            outputs: Written S3 keys.

        Returns:
            The updated execution.

        Raises:
            ValueError: The execution is not in FINALIZING or a key is not a string.
            StaleItemError: The condition failed.
        """
        _require_state(item, State.FINALIZING)
        if not all(isinstance(key, str) for key in outputs):
            raise ValueError("outputs must be a list of strings")
        return self._transition(item, State.FINALIZING, {"outputs": list(outputs)}, [], None)

    def rewrite_outcome_failed(
        self, item: ExecutionItem, error_name: ErrorName | str, cause: str
    ) -> ExecutionItem:
        """Replace the outcome with FAILED, staying in FINALIZING.

        Args:
            item: Execution in FINALIZING.
            error_name: Error name for ``SendTaskFailure``.
            cause: Failure reason. Truncated to ``MAX_CAUSE_LENGTH``.

        Returns:
            The updated execution.

        Raises:
            ValueError: The execution is not in FINALIZING or the error name is unknown.
            StaleItemError: The condition failed.
        """
        _require_state(item, State.FINALIZING)
        outcome = _stored_outcome(build_outcome(OUTCOME_FAILED, error_name, cause))
        return self._transition(item, State.FINALIZING, {"outcome": outcome}, [], None)

    def to_notified_terminal(self, item: ExecutionItem, notify_result: str) -> ExecutionItem:
        """Move FINALIZING to SUCCEEDED or FAILED after Step Functions took the outcome.

        The target state is the stored ``outcome.status``, and the condition also checks
        that the stored status did not change.

        Args:
            item: Execution in FINALIZING with an outcome.
            notify_result: ``ACCEPTED`` or ``ALREADY_CLOSED``.

        Returns:
            The updated execution.

        Raises:
            ValueError: The execution is not in FINALIZING, has no outcome, or the notify
                result is unknown.
            StaleItemError: The condition failed.
        """
        _require_state(item, State.FINALIZING)
        _check_notify_result(notify_result)
        if item.outcome is None or item.outcome.get("status") not in OUTCOME_STATUSES:
            raise ValueError(f"{item.pipelineExecutionId} has no valid outcome")
        status = item.outcome["status"]
        set_values: dict[str, Any] = {"notifyResult": notify_result}
        set_values.update(self._terminal_attributes())
        extra = (
            "#outcome.#status = :outcomeStatus",
            {"#outcome": "outcome", "#status": "status"},
            {":outcomeStatus": status},
        )
        return self._transition(item, State(status), set_values, [], extra)

    def to_abort_suspected(self, item: ExecutionItem, used_token: str) -> ExecutionItem:
        """Move a non terminal execution to ABORT_SUSPECTED after a heartbeat failed.

        Args:
            item: Execution in a non terminal state other than ABORT_SUSPECTED.
            used_token: Token that the failed heartbeat used. It must still be the latest.

        Returns:
            The updated execution with ``prevState`` and ``suspectedAt`` (epoch seconds).

        Raises:
            ValueError: The execution cannot be suspected from its state.
            StaleItemError: The condition failed, for example because the token changed.
        """
        if item.state not in ABORT_SUSPECTABLE_STATES:
            raise ValueError(f"Cannot move {item.state.value} to ABORT_SUSPECTED")
        set_values = {"prevState": item.state, "suspectedAt": self._now_seconds()}
        extra = ("#taskToken = :usedToken", {"#taskToken": "taskToken"}, {":usedToken": used_token})
        return self._transition(item, State.ABORT_SUSPECTED, set_values, [], extra)

    def restore_from_abort(self, item: ExecutionItem, suspected_token: str) -> ExecutionItem:
        """Return ABORT_SUSPECTED to ``prevState`` after a new token was delivered.

        Args:
            item: Execution in ABORT_SUSPECTED.
            suspected_token: Token that the failed heartbeat used.

        Returns:
            The updated execution.

        Raises:
            ValueError: The execution is not in ABORT_SUSPECTED or has no ``prevState``.
            StaleItemError: The condition failed, for example because the token is the same.
        """
        _require_state(item, State.ABORT_SUSPECTED)
        if item.prevState is None:
            raise ValueError(f"{item.pipelineExecutionId} has no prevState")
        extra = (
            "#taskToken <> :suspectedToken",
            {"#taskToken": "taskToken"},
            {":suspectedToken": suspected_token},
        )
        return self._transition(item, item.prevState, {}, ["prevState", "suspectedAt"], extra)

    def to_aborted(
        self, item: ExecutionItem, suspected_token: str, grace_limit: int
    ) -> ExecutionItem:
        """Move ABORT_SUSPECTED to ABORTED after the grace period and the scancel.

        Args:
            item: Execution in ABORT_SUSPECTED.
            suspected_token: Token that the failed heartbeat used. It must still be the latest.
            grace_limit: Epoch seconds. ``suspectedAt`` must be earlier than this value.

        Returns:
            The updated execution.

        Raises:
            ValueError: The execution is not in ABORT_SUSPECTED.
            StaleItemError: The condition failed.
        """
        _require_state(item, State.ABORT_SUSPECTED)
        extra = (
            "#taskToken = :suspectedToken AND #suspectedAt < :graceLimit",
            {"#taskToken": "taskToken", "#suspectedAt": "suspectedAt"},
            {":suspectedToken": suspected_token, ":graceLimit": int(grace_limit)},
        )
        return self._transition(item, State.ABORTED, self._terminal_attributes(), [], extra)

    def update_notify_result(self, item: ExecutionItem, notify_result: str) -> ExecutionItem:
        """Record the result of sending the stored outcome again to a terminal execution.

        Args:
            item: Terminal execution.
            notify_result: ``ACCEPTED`` or ``ALREADY_CLOSED``.

        Returns:
            The updated execution without ``resendPending``.

        Raises:
            ValueError: The execution is not terminal or the notify result is unknown.
            StaleItemError: The condition failed, for example because the token changed.
        """
        if item.state not in TERMINAL_STATES:
            raise ValueError(f"{item.pipelineExecutionId} is not terminal: {item.state.value}")
        _check_notify_result(notify_result)
        return self._transition(
            item, item.state, {"notifyResult": notify_result}, ["resendPending"], None
        )

    def complete_cleanup(self, item: ExecutionItem) -> None:
        """Remove ``pending`` after the cleanup of a terminal execution.

        When the execution holds a concurrency slot, the counter is decremented under
        ``active > 0`` and ``holdsSlot`` is removed in the same transaction.

        Args:
            item: Terminal execution with ``pending=CLEANUP``.

        Raises:
            StaleItemError: The execution is not terminal, not pending cleanup, waits for a
                resend, or its ``holdsSlot`` differs from ``item``.
            RuntimeError: The execution holds a slot but the counter is already 0.
            TransientError: The transaction conflicted with another transaction.
        """
        expression = _Expression()
        state_name = expression.name("state")
        terminal_values = ", ".join(
            expression.value(f"terminal{state.value}", state) for state in sorted(TERMINAL_STATES)
        )
        pending = expression.name("pending")
        holds_slot = expression.name("holdsSlot")
        expression.conditions.append(f"{state_name} IN ({terminal_values})")
        expression.conditions.append(f"{pending} = {expression.value('cleanup', PENDING_CLEANUP)}")
        expression.conditions.append(f"attribute_not_exists({expression.name('resendPending')})")
        expression.remove("pending")
        key = self._key(item.pipelineExecutionId)
        if not item.holdsSlot:
            expression.conditions.append(f"attribute_not_exists({holds_slot})")
            if not self._update_if(expression, item.pipelineExecutionId):
                raise StaleItemError(f"{item.pipelineExecutionId} is not pending cleanup")
            return
        expression.conditions.append(f"{holds_slot} = {expression.value('holdsSlotTrue', True)}")
        expression.remove("holdsSlot")
        counter = _Expression()
        active = counter.name(CONCURRENCY_ACTIVE_ATTR)
        counter.set_clauses.append(f"{active} = {active} - {counter.value('one', 1)}")
        counter.conditions.append(f"{active} > {counter.value('zero', 0)}")
        codes = self._transact_updates(
            [
                counter.update_request(
                    self.table_name, {PARTITION_KEY: {"S": CONCURRENCY_ITEM_ID}}
                ),
                expression.update_request(self.table_name, key),
            ]
        )
        if codes is None:
            return
        counter_code, execution_code = codes
        if execution_code == CANCELLATION_CONDITION_FAILED:
            raise StaleItemError(f"{item.pipelineExecutionId} is not pending cleanup with a slot")
        if counter_code == CANCELLATION_CONDITION_FAILED:
            raise RuntimeError(
                f"{item.pipelineExecutionId} holds a concurrency slot but the counter is 0"
            )
        raise AssertionError(f"Unexpected cancellation codes: {codes}")

    def _transition(
        self,
        item: ExecutionItem,
        to_state: State,
        set_values: Mapping[str, Any],
        remove_attrs: list[str],
        extra_condition: ExtraCondition | None,
    ) -> ExecutionItem:
        """Run a transition without the caller attribute checks.

        Args:
            item: Execution as last read by the caller.
            to_state: Target state.
            set_values: Attributes to set.
            remove_attrs: Attributes to remove.
            extra_condition: Condition ANDed with ``C(item.state)``.

        Returns:
            The updated execution.

        Raises:
            StaleItemError: The condition failed.
        """
        expression = self._transition_expression(
            item, to_state, set_values, remove_attrs, extra_condition
        )
        try:
            response = self._client.update_item(
                **expression.update_request(self.table_name, self._key(item.pipelineExecutionId)),
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _is_conditional_check_failure(error):
                raise StaleItemError(
                    f"{item.pipelineExecutionId} changed from {item.state.value} v{item.version} "
                    f"or its lease is not held by {self.owner_id}"
                ) from error
            raise
        return ExecutionItem.from_item(response["Attributes"])

    def _transition_expression(
        self,
        item: ExecutionItem,
        to_state: State,
        set_values: Mapping[str, Any],
        remove_attrs: list[str],
        extra_condition: ExtraCondition | None,
    ) -> _Expression:
        """Build the update and the condition ``C(item.state)`` of a transition.

        Args:
            item: Execution as last read by the caller.
            to_state: Target state.
            set_values: Attributes to set.
            remove_attrs: Attributes to remove.
            extra_condition: Condition ANDed with ``C(item.state)``.

        Returns:
            The expression with SET, REMOVE, and condition clauses.
        """
        now = self._clock()
        expression = _Expression()
        state_name = expression.name("state")
        version_name = expression.name("version")
        expression.set_clauses.append(f"{state_name} = {expression.value('toState', to_state)}")
        expression.set_clauses.append(
            f"{version_name} = {version_name} + {expression.value('one', 1)}"
        )
        expression.set("updatedAt", format_iso8601_utc(now))
        for attribute, value in set_values.items():
            expression.set(attribute, value)
        for attribute in remove_attrs:
            expression.remove(attribute)
        expression.conditions.append(
            f"{state_name} = {expression.value('expectedState', item.state)}"
        )
        expression.conditions.append(
            f"{version_name} = {expression.value('expectedVersion', item.version)}"
        )
        expression.conditions.append(
            f"{expression.name('leaseOwner')} = {expression.value('me', self.owner_id)}"
        )
        expression.conditions.append(
            f"{expression.name('leaseExpiresAt')} > {expression.value('now', int(now))}"
        )
        if extra_condition is not None:
            expression.add_condition(extra_condition)
        return expression

    def _replace_token_request(
        self,
        pipeline_execution_id: str,
        task_token: str,
        token_sent_at: int,
        is_terminal: bool,
        request_identity: Mapping[str, str],
    ) -> dict[str, Any]:
        """Build the ``UpdateItem`` request of ``replace_token``.

        Args:
            pipeline_execution_id: Execution to update.
            task_token: New task token.
            token_sent_at: SQS ``SentTimestamp`` of the new message in epoch milliseconds.
            is_terminal: Whether the item was terminal when read. The condition checks it.
            request_identity: Identity fields of the new message. The condition requires
                the stored values to be equal.

        Returns:
            Request parameters without ``ReturnValues``.
        """
        expression = _Expression()
        version_name = expression.name("version")
        state_name = expression.name("state")
        sent_at = expression.value("sentAt", token_sent_at)
        expression.set_clauses.append(
            f"{expression.name('taskToken')} = {expression.value('token', task_token)}"
        )
        expression.set_clauses.append(f"{expression.name('tokenSentAt')} = {sent_at}")
        expression.set_clauses.append(
            f"{version_name} = {version_name} + {expression.value('one', 1)}"
        )
        expression.set("updatedAt", format_iso8601_utc(self._clock()))
        expression.conditions.append(f"attribute_exists({expression.name(PARTITION_KEY)})")
        expression.conditions.append(f"{expression.name('tokenSentAt')} < {sent_at}")
        for name in REQUEST_IDENTITY_FIELDS:
            expression.conditions.append(
                f"{expression.name(name)} = "
                f"{expression.value(f'identity_{name}', request_identity[name])}"
            )
        terminal_values = ", ".join(
            expression.value(f"terminal{state.value}", state) for state in sorted(TERMINAL_STATES)
        )
        if is_terminal:
            pending = expression.name("pending")
            expression.set("resendPending", True)
            cleanup = expression.value("cleanup", PENDING_CLEANUP)
            expression.set_clauses.append(f"{pending} = if_not_exists({pending}, {cleanup})")
            expression.conditions.append(f"{state_name} IN ({terminal_values})")
        else:
            expression.conditions.append(f"NOT ({state_name} IN ({terminal_values}))")
        return expression.update_request(self.table_name, self._key(pipeline_execution_id))

    def _terminal_attributes(self) -> dict[str, Any]:
        """Build the attributes written when an execution becomes terminal.

        Returns:
            ``terminalAt``, ``ttl`` (``terminalAt`` plus ``ttl_days``), and ``pending=CLEANUP``.
        """
        now = self._now_seconds()
        return {
            "terminalAt": format_iso8601_utc(now),
            "ttl": now + self.ttl_days * SECONDS_PER_DAY,
            "pending": PENDING_CLEANUP,
        }

    def _update_if(self, expression: _Expression, pipeline_execution_id: str) -> bool:
        """Run a conditional ``UpdateItem``.

        Args:
            expression: Update and condition.
            pipeline_execution_id: Execution to update.

        Returns:
            True when the item was updated, False when the condition failed.
        """
        try:
            self._client.update_item(
                **expression.update_request(self.table_name, self._key(pipeline_execution_id))
            )
        except ClientError as error:
            if _is_conditional_check_failure(error):
                return False
            raise
        return True

    def _transact_updates(self, updates: list[dict[str, Any]]) -> list[str] | None:
        """Run updates in one ``TransactWriteItems``.

        Args:
            updates: ``Update`` parameters, one per item.

        Returns:
            None when the transaction succeeded, or the per item cancellation codes when a
            condition failed.

        Raises:
            TransientError: The transaction conflicted with another transaction.
        """
        try:
            self._client.transact_write_items(
                TransactItems=[{"Update": update} for update in updates]
            )
        except ClientError as error:
            codes = _cancellation_codes(error)
            if codes is None:
                raise
            if CANCELLATION_TRANSACTION_CONFLICT in codes:
                raise TransientError(f"Transaction conflict: {codes}") from error
            if CANCELLATION_CONDITION_FAILED not in codes:
                raise
            return codes
        return None

    def _now_seconds(self) -> int:
        """Return the current epoch seconds as an integer.

        Returns:
            ``int(clock())``.
        """
        return int(self._clock())

    def _key(self, pipeline_execution_id: str) -> dict[str, Any]:
        """Build the primary key of an execution.

        Args:
            pipeline_execution_id: Execution id.

        Returns:
            The key in attribute value format.
        """
        return {PARTITION_KEY: {"S": pipeline_execution_id}}


def _stored_outcome(outcome: Mapping[str, str | None]) -> dict[str, str]:
    """Drop null entries from an outcome before storing it.

    Args:
        outcome: ``{"status", "errorName", "cause"}``.

    Returns:
        The outcome without None values.
    """
    return {key: value for key, value in outcome.items() if value is not None}


def _check_caller_attributes(set_values: Mapping[str, Any], remove_attrs: list[str]) -> None:
    """Reject attributes that callers of the generic transitions must not change.

    Args:
        set_values: Attributes to set.
        remove_attrs: Attributes to remove.

    Raises:
        ValueError: An attribute is managed by the store, unknown, or set to None.
    """
    known = {field.name for field in dataclasses.fields(ExecutionItem)}
    for attribute in [*set_values, *remove_attrs]:
        if attribute in MANAGED_ATTRIBUTES:
            raise ValueError(f"Attribute {attribute} is managed by ExecutionStore")
        if attribute not in known:
            raise ValueError(f"Unknown execution attribute {attribute}")
    for attribute, value in set_values.items():
        if value is None:
            raise ValueError(f"Attribute {attribute} is None; remove it instead")


def _require_state(item: ExecutionItem, state: State) -> None:
    """Check the state of the caller's copy of an execution.

    Args:
        item: Execution.
        state: Required state.

    Raises:
        ValueError: The execution is in another state.
    """
    if item.state is not state:
        raise ValueError(
            f"{item.pipelineExecutionId} must be {state.value}, got {item.state.value}"
        )


def _check_notify_result(notify_result: str) -> None:
    """Check a ``notifyResult`` value.

    Args:
        notify_result: Value to check.

    Raises:
        ValueError: The value is not ``ACCEPTED`` or ``ALREADY_CLOSED``.
    """
    if notify_result not in NOTIFY_RESULTS:
        raise ValueError(f"Unknown notify result: {notify_result!r}")
