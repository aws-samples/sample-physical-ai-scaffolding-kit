import * as cdk from 'aws-cdk-lib';
import { aws_dynamodb, aws_iam, aws_s3, aws_sqs } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Configuration, Vams } from '../types/configurations';

export interface VamsIntegrationProps {
  config: Configuration;
  vamsConfig: Vams;
  dataBucket: aws_s3.Bucket;
}

const QUEUE_RETENTION = cdk.Duration.days(14);
const QUEUE_NAME_PATTERN = /^[A-Za-z0-9_-]+$/;
const QUEUE_NAME_MAX_LENGTH = 80;

/**
 * Return a required numeric setting of the VAMS configuration, failing when it is missing.
 *
 * @param value Value read from the configuration.
 * @param name Name of the setting, used in the error message.
 * @returns The value.
 */
export function requireNumber(value: number | undefined, name: string): number {
  if (value === undefined) {
    throw new Error(`config.Vams.${name} is required when config.Vams.Enabled is true`);
  }
  return value;
}

/**
 * Return a required non-empty string setting of the VAMS configuration, failing when it is empty.
 *
 * @param value Value read from the configuration.
 * @param name Name of the setting, used in the error message.
 * @returns The value.
 */
export function requireNonEmpty(value: string, name: string): string {
  if (value === '') {
    throw new Error(`config.Vams.${name} must not be empty when config.Vams.Enabled is true`);
  }
  return value;
}

/**
 * Return the staging prefix of the VAMS runs in the PASK data bucket.
 *
 * @param lustreS3Prefix config.Lustre.S3Prefix. A trailing slash is added when it is not empty.
 * @returns `<S3Prefix>vams/`.
 */
export function vamsStagingPrefix(lustreS3Prefix: string): string {
  if (lustreS3Prefix === '') {
    return 'vams/';
  }
  return lustreS3Prefix.endsWith('/') ? `${lustreS3Prefix}vams/` : `${lustreS3Prefix}/vams/`;
}

/**
 * Resources that receive VAMS workflow requests on the login node of the HyperPod cluster:
 * the request queue, the run state table, and the permissions of the login execution role.
 *
 * The queue and the table are created before the HyperPod construct, because the lifecycle
 * scripts of the login node need their names. The permissions are granted afterwards with
 * grantPollerAccess, because the login execution role belongs to the HyperPod construct.
 */
export class VamsIntegration extends Construct {
  public readonly queue: aws_sqs.Queue;
  public readonly deadLetterQueue: aws_sqs.Queue;
  public readonly table: aws_dynamodb.Table;
  private readonly pollerStatements: aws_iam.PolicyStatement[];

  constructor(scope: Construct, id: string, props: VamsIntegrationProps) {
    super(scope, id);

    const vams = props.vamsConfig;
    if (props.config.Mlflow?.Enabled !== true) {
      throw new Error('config.Mlflow.Enabled must be true when config.Vams.Enabled is true');
    }
    const configName = requireNonEmpty(vams.ConfigName, 'ConfigName');
    const workflowBucketName = requireNonEmpty(vams.WorkflowBucketName, 'WorkflowBucketName');
    if (vams.AssetBucketNames.length === 0) {
      throw new Error('config.Vams.AssetBucketNames must not be empty when config.Vams.Enabled is true');
    }
    const stagingPrefix = vamsStagingPrefix(props.config.Lustre.S3Prefix);

    this.deadLetterQueue = this.createQueue('DeadLetterQueue', `pask-${configName}-training-dlq`);
    this.queue = this.createQueue('Queue', `pask-${configName}-training`, {
      visibilityTimeout: cdk.Duration.seconds(
        requireNumber(vams.QueueVisibilityTimeoutSeconds, 'QueueVisibilityTimeoutSeconds'),
      ),
      deadLetterQueue: {
        queue: this.deadLetterQueue,
        maxReceiveCount: requireNumber(vams.MaxReceiveCount, 'MaxReceiveCount'),
      },
    });

    this.table = new aws_dynamodb.Table(this, 'Table', {
      partitionKey: { name: 'pipelineExecutionId', type: aws_dynamodb.AttributeType.STRING },
      billingMode: aws_dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.table.addGlobalSecondaryIndex({
      indexName: 'pending-index',
      partitionKey: { name: 'pending', type: aws_dynamodb.AttributeType.STRING },
      sortKey: { name: 'updatedAt', type: aws_dynamodb.AttributeType.STRING },
      projectionType: aws_dynamodb.ProjectionType.ALL,
    });

    props.dataBucket.addLifecycleRule({
      id: 'VamsStagingExpiration',
      prefix: stagingPrefix,
      expiration: cdk.Duration.days(
        requireNumber(vams.StagingExpirationDays, 'StagingExpirationDays'),
      ),
    });

    this.pollerStatements = [
      ...this.queueAndTableStatements(),
      ...this.vamsBucketStatements(vams.AssetBucketNames, workflowBucketName),
      ...this.stagingStatements(props.dataBucket, stagingPrefix),
      this.taskCallbackStatement(configName),
      ...this.kmsStatements(vams.KmsKeyArns ?? []),
    ];

    new cdk.CfnOutput(this, 'VamsQueueUrl', {
      value: this.queue.queueUrl,
      description: 'SQS queue URL the VAMS workflow sends training requests to',
    });
    new cdk.CfnOutput(this, 'VamsQueueArn', {
      value: this.queue.queueArn,
      description: 'SQS queue ARN the VAMS workflow sends training requests to',
    });
    new cdk.CfnOutput(this, 'VamsDeadLetterQueueUrl', {
      value: this.deadLetterQueue.queueUrl,
      description: 'Dead-letter queue URL of the VAMS training requests',
    });
    new cdk.CfnOutput(this, 'VamsTableName', {
      value: this.table.tableName,
      description: 'DynamoDB table holding the state of the VAMS training runs',
    });
  }

  /**
   * Grant the permissions the VAMS poller needs to the execution role of the login group.
   *
   * @param loginExecutionRole Execution role of the login group, where the poller runs.
   */
  public grantPollerAccess(loginExecutionRole: aws_iam.Role): void {
    for (const statement of this.pollerStatements) {
      loginExecutionRole.addToPolicy(statement);
    }
  }

  /**
   * Create a standard SQS queue encrypted with SSE-SQS that accepts only TLS requests.
   * SSE-SQS is used because the VAMS workflow role has no KMS permission.
   *
   * @param id Construct id of the queue.
   * @param queueName Name of the queue. The VAMS workflow role can only send to names containing its config name.
   * @param extraProps Additional queue properties.
   * @returns Created queue.
   */
  private createQueue(
    id: string,
    queueName: string,
    extraProps?: Pick<aws_sqs.QueueProps, 'visibilityTimeout' | 'deadLetterQueue'>,
  ): aws_sqs.Queue {
    if (!QUEUE_NAME_PATTERN.test(queueName) || queueName.length > QUEUE_NAME_MAX_LENGTH) {
      throw new Error(
        `Invalid SQS queue name "${queueName}": config.Vams.ConfigName must contain only alphanumerics, hyphens and underscores`,
      );
    }
    return new aws_sqs.Queue(this, id, {
      queueName,
      encryption: aws_sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: QUEUE_RETENTION,
      ...extraProps,
    });
  }

  /**
   * Build the statements for consuming the request queue and updating the run state table.
   * ConditionCheckItem is included because TransactWriteItems needs the per-item permissions.
   *
   * @returns Policy statements.
   */
  private queueAndTableStatements(): aws_iam.PolicyStatement[] {
    return [
      new aws_iam.PolicyStatement({
        actions: [
          'sqs:ReceiveMessage',
          'sqs:DeleteMessage',
          'sqs:ChangeMessageVisibility',
          'sqs:GetQueueAttributes',
        ],
        resources: [this.queue.queueArn],
      }),
      new aws_iam.PolicyStatement({
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:Query',
          'dynamodb:ConditionCheckItem',
        ],
        resources: [this.table.tableArn, `${this.table.tableArn}/index/*`],
      }),
    ];
  }

  /**
   * Build the statements for the VAMS buckets: read the assets and the workflow inputs,
   * and write only under the pipeline outputs prefix. No delete permission is granted.
   *
   * @param assetBucketNames Names of every VAMS asset bucket, including external ones.
   * @param workflowBucketName Name of the VAMS workflow execution input/output bucket.
   * @returns Policy statements.
   */
  private vamsBucketStatements(
    assetBucketNames: string[],
    workflowBucketName: string,
  ): aws_iam.PolicyStatement[] {
    const partition = cdk.Stack.of(this).partition;
    const bucketArn = (bucketName: string): string => `arn:${partition}:s3:::${bucketName}`;
    const workflowBucketArn = bucketArn(workflowBucketName);
    return [
      new aws_iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:GetObjectVersion'],
        resources: assetBucketNames.map((bucketName) => `${bucketArn(bucketName)}/*`),
      }),
      new aws_iam.PolicyStatement({
        actions: ['s3:ListBucket', 's3:ListBucketVersions'],
        resources: assetBucketNames.map(bucketArn),
      }),
      new aws_iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${workflowBucketArn}/pipelines/workflowExecutionInputs/*`],
      }),
      new aws_iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [`${workflowBucketArn}/*pipelines/*/output/*`],
      }),
    ];
  }

  /**
   * Build the statements for the staging copies of the inputs in the PASK data bucket.
   *
   * @param dataBucket PASK data bucket linked to Lustre.
   * @param stagingPrefix `<S3Prefix>vams/`.
   * @returns Policy statements.
   */
  private stagingStatements(
    dataBucket: aws_s3.Bucket,
    stagingPrefix: string,
  ): aws_iam.PolicyStatement[] {
    return [
      new aws_iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:AbortMultipartUpload'],
        resources: [dataBucket.arnForObjects(`${stagingPrefix}*`)],
      }),
      new aws_iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [dataBucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': [`${stagingPrefix}*`] } },
      }),
    ];
  }

  /**
   * Build the statement for the Step Functions task token callbacks.
   * The resources are limited to the VAMS state machine name patterns.
   *
   * @param configName VAMS config.name.
   * @returns Policy statement.
   */
  private taskCallbackStatement(configName: string): aws_iam.PolicyStatement {
    const stack = cdk.Stack.of(this);
    const stateMachineArn = (namePattern: string): string =>
      stack.formatArn({
        service: 'states',
        resource: 'stateMachine',
        resourceName: namePattern,
        arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
      });
    return new aws_iam.PolicyStatement({
      actions: ['states:SendTaskSuccess', 'states:SendTaskFailure', 'states:SendTaskHeartbeat'],
      resources: [stateMachineArn(`*${configName}*`), stateMachineArn('vams-*')],
    });
  }

  /**
   * Build the statement for the KMS keys of the VAMS buckets, if any.
   *
   * @param kmsKeyArns ARNs of the KMS keys encrypting the VAMS buckets.
   * @returns Policy statements. Empty when no key is given.
   */
  private kmsStatements(kmsKeyArns: string[]): aws_iam.PolicyStatement[] {
    if (kmsKeyArns.length === 0) {
      return [];
    }
    return [
      new aws_iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: kmsKeyArns,
      }),
    ];
  }
}
