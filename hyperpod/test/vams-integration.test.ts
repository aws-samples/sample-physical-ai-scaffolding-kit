import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { PASKStack } from '../lib/pask-stack';
import { Configuration } from '../lib/types/configurations';

const HYPERPOD_DIR = path.resolve(__dirname, '..');
const SAMPLE_CONFIG = JSON.parse(fs.readFileSync(path.join(HYPERPOD_DIR, 'cdk.json'), 'utf-8')).context
  .config as Configuration;

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const CONFIG_NAME = 'vamsdev';
const ASSET_BUCKET = 'vams-assets-example';
const EXTERNAL_ASSET_BUCKET = 'vams-external-assets-example';
const WORKFLOW_BUCKET = 'vams-workflow-example';
const PARTITION = '${AWS::Partition}';
const KMS_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/00000000-0000-0000-0000-000000000000`;

type CfnResource = { Type: string; Properties: Record<string, any> };
type PolicyStatementJson = {
  Effect: string;
  Action: string | string[];
  Resource: unknown;
  Condition?: Record<string, Record<string, unknown>>;
};
type RenderedStatement = { actions: string[]; resources: string[]; condition: unknown };

/**
 * Return a deep copy of the sample configuration of cdk.json modified by the given function.
 *
 * @param modify Function that changes the copied configuration in place.
 * @returns Modified configuration.
 */
function configWith(modify: (config: Configuration) => void): Configuration {
  const config = structuredClone(SAMPLE_CONFIG);
  modify(config);
  return config;
}

/**
 * Return the sample configuration with the VAMS integration enabled.
 *
 * @param lustreS3Prefix config.Lustre.S3Prefix.
 * @returns Configuration.
 */
function vamsEnabledConfig(lustreS3Prefix: string): Configuration {
  return configWith((config) => {
    config.Lustre.S3Prefix = lustreS3Prefix;
    const vams = config.Vams!;
    vams.Enabled = true;
    vams.ConfigName = CONFIG_NAME;
    vams.AssetBucketNames = [ASSET_BUCKET, EXTERNAL_ASSET_BUCKET];
    vams.WorkflowBucketName = WORKFLOW_BUCKET;
    vams.KmsKeyArns = [KMS_KEY_ARN];
  });
}

/**
 * Synthesize the PASK stack with an explicit environment.
 *
 * @param config Configuration of the stack.
 * @returns Stack template.
 */
function synth(config: Configuration): Template {
  return Template.fromStack(createStack(config));
}

/**
 * Create the PASK stack with an explicit environment.
 *
 * @param config Configuration of the stack.
 * @returns Stack.
 */
function createStack(config: Configuration): PASKStack {
  // Same as cdk.json: server access logs are delivered through a bucket policy.
  const app = new cdk.App({ context: { '@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy': true } });
  return new PASKStack(app, config.StackName, {
    config,
    env: { account: ACCOUNT, region: REGION },
  });
}

/**
 * Render a CloudFormation value to a string, keeping references readable
 * (Ref X -> ${X}, Fn::GetAtt [X, A] -> ${X.A}).
 *
 * @param value CloudFormation value.
 * @returns Rendered string.
 */
function render(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  const intrinsic = value as Record<string, any>;
  if ('Ref' in intrinsic) {
    return `\${${intrinsic.Ref}}`;
  }
  if ('Fn::GetAtt' in intrinsic) {
    return `\${${intrinsic['Fn::GetAtt'].join('.')}}`;
  }
  if ('Fn::Join' in intrinsic) {
    const [separator, parts] = intrinsic['Fn::Join'] as [string, unknown[]];
    return parts.map(render).join(separator);
  }
  throw new Error(`Unsupported CloudFormation value: ${JSON.stringify(value)}`);
}

/**
 * Normalize a string-or-array field to an array.
 *
 * @param value String or array.
 * @returns Array.
 */
function asArray<T>(value: T | T[]): T[] {
  return Array.isArray(value) ? value : [value];
}

/**
 * Return the logical IDs of the HyperPod execution roles (roles of the HyperPod construct assumed by SageMaker).
 *
 * @param template Stack template.
 * @returns Logical IDs.
 */
function executionRoleIds(template: Template): string[] {
  const sagemakerRoleIds = Object.keys(
    template.findResources('AWS::IAM::Role', {
      Properties: {
        AssumeRolePolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({ Principal: { Service: 'sagemaker.amazonaws.com' } }),
          ]),
        },
      },
    }),
  );
  return sagemakerRoleIds.filter((roleId) => roleId.startsWith('HyperPod'));
}

/**
 * Return the logical ID of the role used by the instance group with the given name.
 *
 * @param template Stack template.
 * @param groupName Instance group name.
 * @returns Logical ID of the role.
 */
function groupRoleId(template: Template, groupName: string): string {
  const clusters = Object.values(template.findResources('AWS::SageMaker::Cluster')) as CfnResource[];
  expect(clusters).toHaveLength(1);
  const group = (clusters[0].Properties.InstanceGroups as Record<string, any>[]).find(
    (instanceGroup) => instanceGroup.InstanceGroupName === groupName,
  );
  expect(group).toBeDefined();
  return group!.ExecutionRole['Fn::GetAtt'][0];
}

/**
 * Return the statements of the policies attached to the given role, with rendered resources.
 *
 * @param template Stack template.
 * @param roleId Logical ID of the role.
 * @returns Actions, rendered resources and condition of every statement.
 */
function roleStatements(template: Template, roleId: string): RenderedStatement[] {
  const policies = Object.values(
    template.findResources('AWS::IAM::Policy', { Properties: { Roles: [{ Ref: roleId }] } }),
  ) as CfnResource[];
  return policies
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement as PolicyStatementJson[])
    .map((statement) => ({
      actions: asArray(statement.Action),
      resources: asArray(statement.Resource).map(render),
      condition: statement.Condition,
    }));
}

/**
 * Return the statements that contain an action of the given service prefix.
 *
 * @param statements Rendered statements.
 * @param servicePrefix Prefix such as `sqs:`.
 * @returns Matching statements.
 */
function statementsFor(statements: RenderedStatement[], servicePrefix: string): RenderedStatement[] {
  return statements.filter((statement) =>
    statement.actions.some((action) => action.startsWith(servicePrefix)),
  );
}

const LOADER_RESOURCE_TYPE = 'Custom::LoadLifeCycleScript';
const BUCKET_DEPLOYMENT_TYPE = 'Custom::CDKBucketDeployment';
const POLLER_ENVIRONMENT_KEYS = [
  'VAMS_ENABLED',
  'LOGIN_GROUP_NAME',
  'VAMS_QUEUE_URL',
  'VAMS_TABLE_NAME',
  'VAMS_DATA_BUCKET',
  'LUSTRE_S3_PREFIX',
  'VAMS_REGION',
  'VAMS_CONFIG_NAME',
  'VAMS_VISIBILITY_TIMEOUT_SECONDS',
  'VAMS_HEARTBEAT_INTERVAL_SECONDS',
  'VAMS_ABORT_GRACE_SECONDS',
  'VAMS_LEASE_SECONDS',
  'VAMS_ITEM_TTL_DAYS',
  'VAMS_INGEST_WAIT_SECONDS',
  'VAMS_MAX_CONCURRENT_RUNS',
  'VAMS_SLURM_PARTITION',
  'VAMS_MLFLOW_EXPERIMENT_NAME',
];

/**
 * Return the environment variables of the lifecycle loader function.
 *
 * @param template Stack template.
 * @returns Environment variables.
 */
function loaderEnvironment(template: Template): Record<string, unknown> {
  const functions = Object.values(
    template.findResources('AWS::Lambda::Function', {
      Properties: { Environment: { Variables: Match.objectLike({ GITHUB_PATH: Match.anyValue() }) } },
    }),
  ) as CfnResource[];
  expect(functions).toHaveLength(1);
  return functions[0].Properties.Environment.Variables;
}

/**
 * Return the properties of the lifecycle loader custom resource.
 *
 * @param template Stack template.
 * @returns Properties.
 */
function loaderProperties(template: Template): Record<string, unknown> {
  const resources = Object.values(template.findResources(LOADER_RESOURCE_TYPE)) as CfnResource[];
  expect(resources).toHaveLength(1);
  return resources[0].Properties;
}

/**
 * Return the paths of the files under a directory, relative to it.
 *
 * @param directory Directory to list.
 * @returns Relative file paths with forward slashes.
 */
function listFiles(directory: string): string[] {
  return fs
    .readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => path.relative(directory, path.join(entry.parentPath, entry.name)).split(path.sep).join('/'));
}

process.env.CDK_DEFAULT_REGION = REGION;

describe('VAMS integration disabled (sample configuration)', () => {
  const template = synth(SAMPLE_CONFIG);

  test('the sample configuration keeps the integration disabled', () => {
    expect(SAMPLE_CONFIG.Vams?.Enabled).toBe(false);
  });

  test('creates no SQS queue and no DynamoDB table', () => {
    template.resourceCountIs('AWS::SQS::Queue', 0);
    template.resourceCountIs('AWS::DynamoDB::Table', 0);
  });

  test('keeps a single execution role for every instance group', () => {
    const roleIds = executionRoleIds(template);
    expect(roleIds).toHaveLength(1);
    expect(roleIds[0]).toMatch(/^HyperPodExecutionRole/);
    expect(groupRoleId(template, SAMPLE_CONFIG.Cluster.LoginGroup.Name)).toBe(roleIds[0]);
    expect(groupRoleId(template, SAMPLE_CONFIG.Cluster.ControllerGroup.Name)).toBe(roleIds[0]);
  });

  test('does not output the login execution role', () => {
    expect(Object.keys(template.findOutputs('*', {})).some((id) => id.includes('LoginExecutionRole'))).toBe(
      false,
    );
  });

  test('does not deploy the poller sources', () => {
    template.resourceCountIs(BUCKET_DEPLOYMENT_TYPE, 0);
  });

  test('the lifecycle loader gets no poller settings', () => {
    expect(Object.keys(loaderEnvironment(template)).sort()).toEqual(
      ['BUCKET_NAME', 'BUCKET_PATH', 'GITHUB_BRANCH', 'GITHUB_PATH', 'GITHUB_REPO_URL', 'LUSTRE_FILESYSTEM_PATH'].sort(),
    );
    expect(Object.keys(loaderProperties(template)).sort()).toEqual(
      ['GitHubRef', 'LoaderFingerprint', 'LustreFileSystemPath', 'ServiceToken'].sort(),
    );
  });
});

describe('VAMS integration enabled', () => {
  const config = vamsEnabledConfig('');
  const template = synth(config);
  const loginRoleId = groupRoleId(template, config.Cluster.LoginGroup.Name);
  const clusterRoleId = groupRoleId(template, config.Cluster.ControllerGroup.Name);
  const loginStatements = roleStatements(template, loginRoleId);
  const clusterStatements = roleStatements(template, clusterRoleId);

  test('the login group uses a separate execution role', () => {
    expect(executionRoleIds(template)).toHaveLength(2);
    expect(loginRoleId).not.toBe(clusterRoleId);
    expect(loginRoleId).toContain('LoginExecutionRole');
    for (const worker of config.Cluster.WorkerGroup) {
      expect(groupRoleId(template, worker.Name)).toBe(clusterRoleId);
    }
    expect(Object.keys(template.findOutputs('*', {})).some((id) => id.includes('LoginExecutionRoleARN'))).toBe(
      true,
    );
  });

  test('the login role has the same managed and inline policies as the cluster role', () => {
    const roles = template.toJSON().Resources as Record<string, CfnResource>;
    const loginRole = roles[loginRoleId].Properties;
    const clusterRole = roles[clusterRoleId].Properties;
    expect(loginRole.ManagedPolicyArns).toEqual(clusterRole.ManagedPolicyArns);
    expect(loginRole.Policies).toEqual(clusterRole.Policies);
  });

  test('queues contain the config name and use SSE-SQS without a KMS key', () => {
    template.resourceCountIs('AWS::SQS::Queue', 2);
    const queues = Object.values(template.findResources('AWS::SQS::Queue')) as CfnResource[];
    for (const queue of queues) {
      expect(queue.Properties.QueueName).toContain(CONFIG_NAME);
      expect(queue.Properties.SqsManagedSseEnabled).toBe(true);
      expect(queue.Properties.KmsMasterKeyId).toBeUndefined();
      expect(queue.Properties.MessageRetentionPeriod).toBe(14 * 24 * 60 * 60);
    }
  });

  test('the request queue redrives to the DLQ with the configured maxReceiveCount', () => {
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: `pask-${CONFIG_NAME}-training`,
      VisibilityTimeout: config.Vams!.QueueVisibilityTimeoutSeconds,
      RedrivePolicy: {
        deadLetterTargetArn: Match.anyValue(),
        maxReceiveCount: config.Vams!.MaxReceiveCount,
      },
    });
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: `pask-${CONFIG_NAME}-training-dlq`,
    });
  });

  test('queues deny requests without TLS', () => {
    template.resourcePropertiesCountIs(
      'AWS::SQS::QueuePolicy',
      {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: 'Deny',
              Condition: { Bool: { 'aws:SecureTransport': 'false' } },
            }),
          ]),
        },
      },
      2,
    );
  });

  test('the table has the pending GSI, TTL and point-in-time recovery, and is retained', () => {
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: {
        KeySchema: [{ AttributeName: 'pipelineExecutionId', KeyType: 'HASH' }],
        BillingMode: 'PAY_PER_REQUEST',
        TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
        GlobalSecondaryIndexes: [
          {
            IndexName: 'pending-index',
            KeySchema: [
              { AttributeName: 'pending', KeyType: 'HASH' },
              { AttributeName: 'updatedAt', KeyType: 'RANGE' },
            ],
            Projection: { ProjectionType: 'ALL' },
          },
        ],
      },
    });
  });

  test('only the login role has the SQS, DynamoDB and Step Functions permissions', () => {
    for (const servicePrefix of ['sqs:', 'dynamodb:', 'states:']) {
      expect(statementsFor(loginStatements, servicePrefix).length).toBeGreaterThan(0);
      expect(statementsFor(clusterStatements, servicePrefix)).toHaveLength(0);
    }
    const dynamodbActions = statementsFor(loginStatements, 'dynamodb:').flatMap((statement) => statement.actions);
    expect(dynamodbActions).toEqual(
      expect.arrayContaining([
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:ConditionCheckItem',
      ]),
    );
    const dynamodbResources = statementsFor(loginStatements, 'dynamodb:').flatMap(
      (statement) => statement.resources,
    );
    expect(dynamodbResources.some((resource) => resource.endsWith('/index/*'))).toBe(true);
  });

  test('states:SendTask* is limited to the VAMS state machine name patterns', () => {
    const statesStatements = statementsFor(loginStatements, 'states:');
    expect(statesStatements).toHaveLength(1);
    expect(statesStatements[0].actions.sort()).toEqual([
      'states:SendTaskFailure',
      'states:SendTaskHeartbeat',
      'states:SendTaskSuccess',
    ]);
    expect(statesStatements[0].resources).not.toContain('*');
    expect(statesStatements[0].resources.sort()).toEqual([
      `arn:${PARTITION}:states:${REGION}:${ACCOUNT}:stateMachine:*${CONFIG_NAME}*`,
      `arn:${PARTITION}:states:${REGION}:${ACCOUNT}:stateMachine:vams-*`,
    ]);
  });

  test('no VAMS bucket gets s3:DeleteObject, and outputs are writable only under the pipeline outputs prefix', () => {
    const vamsBucketNames = [ASSET_BUCKET, EXTERNAL_ASSET_BUCKET, WORKFLOW_BUCKET];
    const vamsS3Statements = statementsFor(loginStatements, 's3:').filter((statement) =>
      statement.resources.some((resource) =>
        vamsBucketNames.some((bucketName) => resource.startsWith(`arn:${PARTITION}:s3:::${bucketName}`)),
      ),
    );
    expect(vamsS3Statements.length).toBeGreaterThan(0);
    for (const statement of vamsS3Statements) {
      expect(statement.actions).not.toContain('s3:DeleteObject');
      expect(statement.actions).not.toContain('s3:*');
    }
    const putResources = vamsS3Statements
      .filter((statement) => statement.actions.includes('s3:PutObject'))
      .flatMap((statement) => statement.resources);
    expect(putResources).toEqual([`arn:${PARTITION}:s3:::${WORKFLOW_BUCKET}/*pipelines/*/output/*`]);
    const getResources = vamsS3Statements
      .filter((statement) => statement.actions.includes('s3:GetObject'))
      .flatMap((statement) => statement.resources);
    expect(getResources.sort()).toEqual(
      [
        `arn:${PARTITION}:s3:::${ASSET_BUCKET}/*`,
        `arn:${PARTITION}:s3:::${EXTERNAL_ASSET_BUCKET}/*`,
        `arn:${PARTITION}:s3:::${WORKFLOW_BUCKET}/pipelines/workflowExecutionInputs/*`,
      ].sort(),
    );
  });

  test('every asset bucket is readable by object version and listable by version', () => {
    const assetBucketArns = [ASSET_BUCKET, EXTERNAL_ASSET_BUCKET].map(
      (bucketName) => `arn:${PARTITION}:s3:::${bucketName}`,
    );
    const s3Statements = statementsFor(loginStatements, 's3:');
    const objectReadStatements = s3Statements.filter((statement) =>
      statement.actions.includes('s3:GetObjectVersion'),
    );
    expect(objectReadStatements).toHaveLength(1);
    expect(objectReadStatements[0].actions.sort()).toEqual(['s3:GetObject', 's3:GetObjectVersion']);
    expect(objectReadStatements[0].resources.sort()).toEqual(assetBucketArns.map((arn) => `${arn}/*`).sort());
    const listStatements = s3Statements.filter((statement) => statement.actions.includes('s3:ListBucketVersions'));
    expect(listStatements).toHaveLength(1);
    expect(listStatements[0].actions.sort()).toEqual(['s3:ListBucket', 's3:ListBucketVersions']);
    expect(listStatements[0].resources.sort()).toEqual([...assetBucketArns].sort());
    expect(listStatements[0].condition).toBeUndefined();
  });

  test('the staging prefix of the data bucket is readable, writable and deletable', () => {
    const stagingStatements = statementsFor(loginStatements, 's3:').filter((statement) =>
      statement.resources.some((resource) => resource.endsWith('/vams/*')),
    );
    expect(stagingStatements).toHaveLength(1);
    expect(stagingStatements[0].actions.sort()).toEqual([
      's3:AbortMultipartUpload',
      's3:DeleteObject',
      's3:GetObject',
      's3:PutObject',
    ]);
    const listStatements = statementsFor(loginStatements, 's3:').filter(
      (statement) => statement.condition !== undefined,
    );
    expect(listStatements).toHaveLength(1);
    expect(listStatements[0].actions).toEqual(['s3:ListBucket']);
    expect(listStatements[0].condition).toEqual({ StringLike: { 's3:prefix': ['vams/*'] } });
  });

  test('KMS permissions are granted for the configured keys', () => {
    const kmsStatements = statementsFor(loginStatements, 'kms:');
    expect(kmsStatements).toHaveLength(1);
    expect(kmsStatements[0].actions.sort()).toEqual(['kms:Decrypt', 'kms:GenerateDataKey']);
    expect(kmsStatements[0].resources).toEqual([KMS_KEY_ARN]);
  });

  test('both roles keep the MLflow permissions', () => {
    for (const statements of [loginStatements, clusterStatements]) {
      const mlflowActions = statementsFor(statements, 'sagemaker:').flatMap((statement) => statement.actions);
      expect(mlflowActions).toEqual(
        expect.arrayContaining(['sagemaker:CallMlflowAppApi', 'sagemaker:DescribeMlflowApp']),
      );
    }
  });

  test('the data bucket expires the staging prefix', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: [
          Match.objectLike({
            Prefix: 'vams/',
            ExpirationInDays: config.Vams!.StagingExpirationDays,
            Status: 'Enabled',
          }),
        ],
      },
    });
  });

  test('outputs the queue, DLQ and table', () => {
    const outputIds = Object.keys(template.findOutputs('*', {}));
    for (const name of ['VamsQueueUrl', 'VamsQueueArn', 'VamsDeadLetterQueueUrl', 'VamsTableName']) {
      expect(outputIds.some((id) => id.includes(name))).toBe(true);
    }
  });

  test('deploys the poller sources to vams_poller/ of the lifecycle bucket without pruning', () => {
    template.resourceCountIs(BUCKET_DEPLOYMENT_TYPE, 1);
    const deployment = Object.values(template.findResources(BUCKET_DEPLOYMENT_TYPE))[0] as CfnResource;
    expect(deployment.Properties.DestinationBucketKeyPrefix).toBe('vams_poller/');
    expect(deployment.Properties.Prune).toBe(false);
    const destinationBucketId = deployment.Properties.DestinationBucketName.Ref as string;
    expect(destinationBucketId).toMatch(/^HyperPodLifeCycleScript/);
  });

  test('the cluster waits for the poller sources', () => {
    const deploymentId = Object.keys(template.findResources(BUCKET_DEPLOYMENT_TYPE))[0];
    const cluster = Object.values(template.findResources('AWS::SageMaker::Cluster'))[0] as { DependsOn: string[] };
    expect(cluster.DependsOn).toContain(deploymentId);
  });

  test('the poller asset holds only src/, pyproject.toml and requirements.txt', () => {
    const stack = createStack(config);
    const stagings = stack.node
      .findAll()
      .filter(
        (construct): construct is cdk.AssetStaging =>
          construct instanceof cdk.AssetStaging && construct.sourcePath === path.join(HYPERPOD_DIR, 'vams-poller'),
      );
    expect(stagings).toHaveLength(1);
    const files = listFiles(stagings[0].absoluteStagedPath);
    expect(files).toEqual(expect.arrayContaining(['pyproject.toml', 'requirements.txt', 'src/pask_vams_poller/config.py']));
    for (const file of files) {
      expect(
        file === 'pyproject.toml' || file === 'requirements.txt' || (file.startsWith('src/') && !file.includes('__pycache__')),
      ).toBe(true);
    }
  });

  test('the lifecycle loader environment carries the poller settings', () => {
    const environment = loaderEnvironment(template);
    const vams = config.Vams!;
    expect(environment).toMatchObject({
      VAMS_ENABLED: 'true',
      LOGIN_GROUP_NAME: config.Cluster.LoginGroup.Name,
      LUSTRE_S3_PREFIX: config.Lustre.S3Prefix,
      LUSTRE_FILESYSTEM_PATH: config.Lustre.FileSystemPath,
      VAMS_REGION: REGION,
      VAMS_CONFIG_NAME: CONFIG_NAME,
      VAMS_VISIBILITY_TIMEOUT_SECONDS: String(vams.QueueVisibilityTimeoutSeconds),
      VAMS_HEARTBEAT_INTERVAL_SECONDS: String(vams.HeartbeatIntervalSeconds),
      VAMS_ABORT_GRACE_SECONDS: String(vams.AbortGraceSeconds),
      VAMS_LEASE_SECONDS: String(vams.LeaseSeconds),
      VAMS_ITEM_TTL_DAYS: String(vams.ItemTtlDays),
      VAMS_INGEST_WAIT_SECONDS: String(vams.IngestWaitSeconds),
      VAMS_MAX_CONCURRENT_RUNS: String(vams.MaxConcurrentRuns),
      VAMS_SLURM_PARTITION: vams.SlurmPartition,
      VAMS_MLFLOW_EXPERIMENT_NAME: vams.MlflowExperimentName,
    });
    expect(render(environment.VAMS_QUEUE_URL)).toMatch(/^\$\{VamsQueue[0-9A-F]+\}$/);
    expect(render(environment.VAMS_TABLE_NAME)).toMatch(/^\$\{VamsTable[0-9A-F]+\}$/);
    expect(render(environment.VAMS_DATA_BUCKET)).toMatch(/^\$\{Bucket/);
  });

  test('the lifecycle loader properties carry the same settings, so that a change re-uploads the scripts', () => {
    const environment = loaderEnvironment(template);
    const settings = loaderProperties(template).VamsPollerSettings as Record<string, unknown>;
    expect(Object.keys(settings).sort()).toEqual([...POLLER_ENVIRONMENT_KEYS].sort());
    for (const key of POLLER_ENVIRONMENT_KEYS) {
      expect(settings[key]).toEqual(environment[key]);
    }
  });
});

describe('VAMS integration enabled with a Lustre S3 prefix', () => {
  const config = vamsEnabledConfig('datasets');
  const template = synth(config);
  const loginStatements = roleStatements(
    template,
    groupRoleId(template, config.Cluster.LoginGroup.Name),
  );

  test('the lifecycle rule and the IAM prefix use <S3Prefix>vams/', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      LifecycleConfiguration: {
        Rules: [Match.objectLike({ Prefix: 'datasets/vams/' })],
      },
    });
    const listStatements = statementsFor(loginStatements, 's3:').filter(
      (statement) => statement.condition !== undefined,
    );
    expect(listStatements[0].condition).toEqual({ StringLike: { 's3:prefix': ['datasets/vams/*'] } });
    expect(
      statementsFor(loginStatements, 's3:').some((statement) =>
        statement.resources.some((resource) => resource.endsWith('/datasets/vams/*')),
      ),
    ).toBe(true);
  });
});

describe('VAMS integration enabled with a shared login role', () => {
  const config = vamsEnabledConfig('');
  config.Vams!.SeparateLoginRole = false;
  const template = synth(config);
  const roleIds = executionRoleIds(template);

  test('every instance group keeps the single cluster execution role', () => {
    expect(roleIds).toHaveLength(1);
    expect(roleIds[0]).toMatch(/^HyperPodExecutionRole/);
    expect(groupRoleId(template, config.Cluster.LoginGroup.Name)).toBe(roleIds[0]);
    expect(groupRoleId(template, config.Cluster.ControllerGroup.Name)).toBe(roleIds[0]);
    for (const worker of config.Cluster.WorkerGroup) {
      expect(groupRoleId(template, worker.Name)).toBe(roleIds[0]);
    }
    expect(Object.keys(template.findOutputs('*', {})).some((id) => id.includes('LoginExecutionRole'))).toBe(
      false,
    );
  });

  test('the cluster role gets the poller permissions', () => {
    const statements = roleStatements(template, roleIds[0]);
    for (const servicePrefix of ['sqs:', 'dynamodb:', 'states:']) {
      expect(statementsFor(statements, servicePrefix).length).toBeGreaterThan(0);
    }
  });
});

describe('VAMS integration enabled with missing settings', () => {
  test('fails when ConfigName is empty', () => {
    const config = vamsEnabledConfig('');
    config.Vams!.ConfigName = '';
    expect(() => synth(config)).toThrow(/ConfigName/);
  });

  test('fails when AssetBucketNames is empty', () => {
    const config = vamsEnabledConfig('');
    config.Vams!.AssetBucketNames = [];
    expect(() => synth(config)).toThrow(/AssetBucketNames/);
  });

  test('fails when a required numeric setting is missing', () => {
    const config = vamsEnabledConfig('');
    delete config.Vams!.MaxReceiveCount;
    expect(() => synth(config)).toThrow(/MaxReceiveCount/);
  });

  test('fails when a poller setting is missing', () => {
    const config = vamsEnabledConfig('');
    delete config.Vams!.LeaseSeconds;
    expect(() => synth(config)).toThrow(/LeaseSeconds/);
  });

  test('fails when MlflowExperimentName is missing', () => {
    const config = vamsEnabledConfig('');
    delete config.Vams!.MlflowExperimentName;
    expect(() => synth(config)).toThrow(/MlflowExperimentName/);
  });
});
