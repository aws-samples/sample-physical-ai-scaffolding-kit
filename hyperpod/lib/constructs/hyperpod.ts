import * as cdk from 'aws-cdk-lib';
import {
  aws_logs,
  aws_s3,
  aws_iam,
  aws_ec2,
  aws_lambda,
  aws_fsx,
  aws_s3_deployment,
  aws_sagemaker,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Configuration, Vams } from '../types/configurations';
import { requireNonEmpty, requireNumber } from './vams-integration';
import * as path from 'path';

/** Values the VAMS poller on the login node needs. Given only when the VAMS integration is enabled. */
export interface VamsPollerProps {
  /** URL of the queue the VAMS workflow sends training requests to. */
  queueUrl: string;
  /** Name of the table holding the state of the VAMS training runs. */
  tableName: string;
  /** Name of the PASK data bucket linked to Lustre. */
  dataBucketName: string;
  /** VAMS integration settings (config.Vams). */
  config: Vams;
}

export interface HyperPodProps {
  config: Configuration;
  vpc: aws_ec2.IVpc;
  privateSubnet: aws_ec2.PrivateSubnet;
  lfsSecurityGroup: aws_ec2.SecurityGroup;
  lfs: aws_fsx.LustreFileSystem;
  /** Destination of the S3 server access logs of the lifecycle script bucket. */
  accessLogsBucket: aws_s3.IBucket;
  vamsPoller?: VamsPollerProps;
}

/**
 * Commit of awslabs/awsome-distributed-ai that the lifecycle scripts are fetched from.
 * The scripts run as root on every cluster node, so a reviewed commit is pinned instead
 * of a branch. Update this value to take in upstream changes.
 */
const LIFECYCLE_SCRIPTS_GITHUB_REF = '240279b4a8f9c12297c72ef1bee37ad3a2558884';

const VAMS_POLLER_DIR = path.join(__dirname, '../../vams-poller');
/** Key prefix of the poller sources in the lifecycle script bucket, read by install_vams_poller.sh. */
const VAMS_POLLER_KEY_PREFIX = 'vams_poller/';
/** Files of hyperpod/vams-poller that the nodes do not need. Only src/, pyproject.toml and requirements.txt remain. */
const VAMS_POLLER_EXCLUDES = [
  '.gitignore',
  '.venv',
  '.pytest_cache',
  '.ruff_cache',
  'tests',
  'uv.lock',
  '**/__pycache__',
];

export class HyperPod extends Construct {
  public readonly clusterExecutionRole: aws_iam.Role;
  /** Execution role of the login group. Same object as clusterExecutionRole unless VAMS is enabled. */
  public readonly loginExecutionRole: aws_iam.Role;

  constructor(scope: Construct, id: string, props: HyperPodProps) {
    super(scope, id);

    const isVamsEnabled = props.config.Vams?.Enabled === true;
    if (isVamsEnabled !== (props.vamsPoller !== undefined)) {
      throw new Error('HyperPodProps.vamsPoller must be given exactly when config.Vams.Enabled is true');
    }
    // Empty when the VAMS integration is disabled, so that the loader environment and
    // properties, and thus the template, stay the same as without the integration.
    const vamsPollerEnvironment =
      props.vamsPoller !== undefined ? this.vamsPollerEnvironment(props.config, props.vamsPoller) : {};

    const lifeCycleScriptBucket = new aws_s3.Bucket(this, 'LifeCycleScript', {
      accessControl: aws_s3.BucketAccessControl.PRIVATE,
      blockPublicAccess: aws_s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      encryption: aws_s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: props.accessLogsBucket,
      serverAccessLogsPrefix: 'lifecycle-script/',
    });

    new cdk.CfnOutput(this, 'LifeCycleScriptBucketName', {
      value: lifeCycleScriptBucket.bucketName,
      description: 'Life Cycle scripts Bucket',
    });

    // Custom resource to download lifecycle scripts from github
    // https://github.com/awslabs/awsome-distributed-ai/tree/main/1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config
    const loadLifeCycleScriptLogGroup = new aws_logs.LogGroup(this, 'LoadLifeCycleScriptLogGroup', {
      retention: aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const loadLifeCycleScriptFunctionRole = new aws_iam.Role(
      this,
      'LoadLifeCycleScriptFunctionRole',
      {
        assumedBy: new aws_iam.ServicePrincipal('lambda.amazonaws.com'),
      },
    );
    loadLifeCycleScriptLogGroup.grantWrite(loadLifeCycleScriptFunctionRole);
    lifeCycleScriptBucket.grantReadWrite(loadLifeCycleScriptFunctionRole);

    const lifeCycleLoaderPath = path.join(
      __dirname,
      '../lambda/custom-resources/lifecycle-loader',
    );

    const loadLifeCycleScriptFunction = new aws_lambda.Function(
      this,
      'LoadLifeCycleScriptFunction',
      {
        runtime: aws_lambda.Runtime.PYTHON_3_14,
        memorySize: 1024,
        handler: 'index.handler',
        role: loadLifeCycleScriptFunctionRole,
        code: aws_lambda.Code.fromAsset(lifeCycleLoaderPath),
        timeout: cdk.Duration.minutes(15),
        logGroup: loadLifeCycleScriptLogGroup,
        environment: {
          BUCKET_NAME: lifeCycleScriptBucket.bucketName,
          BUCKET_PATH: '',
          GITHUB_BRANCH: LIFECYCLE_SCRIPTS_GITHUB_REF,
          GITHUB_PATH: '1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config',
          GITHUB_REPO_URL: 'https://github.com/awslabs/awsome-distributed-ai',
          // Injected into the overrides/*.sh placeholders at upload time.
          LUSTRE_FILESYSTEM_PATH: props.config.Lustre.FileSystemPath,
          ...vamsPollerEnvironment,
        },
      },
    );
    const loadLifeCycleScriptResource = new cdk.CustomResource(
      this,
      'LoadLifeCycleScriptResource',
      {
        serviceToken: loadLifeCycleScriptFunction.functionArn,
        resourceType: 'Custom::LoadLifeCycleScript',
        // CloudFormation only invokes the resource when its properties change, so
        // the loader inputs are passed here to re-upload the scripts whenever the
        // loader code, the overrides, or the substituted Lustre path change.
        properties: {
          LoaderFingerprint: cdk.FileSystem.fingerprint(lifeCycleLoaderPath),
          GitHubRef: LIFECYCLE_SCRIPTS_GITHUB_REF,
          LustreFileSystemPath: props.config.Lustre.FileSystemPath,
          ...(props.vamsPoller !== undefined ? { VamsPollerSettings: vamsPollerEnvironment } : {}),
        },
      },
    );
    loadLifeCycleScriptResource.node.addDependency(lifeCycleScriptBucket);

    // The poller sources sit next to the lifecycle scripts, so that install_vams_poller.sh
    // finds them in the directory HyperPod copies to the node. prune is off so that the
    // files uploaded by the lifecycle loader are kept.
    const vamsPollerDeployment =
      props.vamsPoller !== undefined
        ? new aws_s3_deployment.BucketDeployment(this, 'VamsPollerDeployment', {
            sources: [aws_s3_deployment.Source.asset(VAMS_POLLER_DIR, { exclude: VAMS_POLLER_EXCLUDES })],
            destinationBucket: lifeCycleScriptBucket,
            destinationKeyPrefix: VAMS_POLLER_KEY_PREFIX,
            prune: false,
          })
        : undefined;

    // Cluster
    const clusterLogsGroup = new aws_logs.LogGroup(this, 'LogGroup', {
      retention: aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const interfacePolicy = new aws_iam.PolicyDocument({
      statements: [
        new aws_iam.PolicyStatement({
          actions: [
            'ec2:DescribeNetworkInterfaces',
            'ec2:DescribeVpcs',
            'ec2:DescribeDhcpOptions',
            'ec2:DescribeSubnets',
            'ec2:DescribeSecurityGroups',
            'ec2:CreateNetworkInterface',
            'ec2:CreateNetworkInterfacePermission',
            'ec2:CreateTags',
            'ec2:DetachNetworkInterface',
            'ec2:DeleteNetworkInterface',
            'ec2:DeleteNetworkInterfacePermission',
          ],
          resources: ['*'],
        }),
      ],
    });
    const clusterPolicy = new aws_iam.PolicyDocument({
      statements: [
        new aws_iam.PolicyStatement({
          actions: [
            'sagemaker:DeleteCluster',
            'sagemaker:DescribeCluster',
            'sagemaker:DescribeClusterNode',
            'sagemaker:ListClusterNodes',
            'sagemaker:UpdateCluster',
            'sagemaker:UpdateClusterSoftware',
            'sagemaker:BatchDeleteClusterNodes',
          ],
          resources: [
            `arn:aws:sagemaker:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:cluster/*`,
          ],
        }),
        new aws_iam.PolicyStatement({
          actions: ['sagemaker:ListClusters', 'cloudformation:DescribeStacks'],
          resources: ['*'],
        }),
      ],
    });
    const ecrPolicy = new aws_iam.PolicyDocument({
      statements: [
        new aws_iam.PolicyStatement({
          actions: [
            'ecr:CreateRepository',
            'ecr:BatchCheckLayerAvailability',
            'ecr:GetDownloadUrlForLayer',
            'ecr:GetRepositoryPolicy',
            'ecr:DescribeRepositories',
            'ecr:ListImages',
            'ecr:DescribeImages',
            'ecr:BatchGetImage',
            'ecr:InitiateLayerUpload',
            'ecr:UploadLayerPart',
            'ecr:CompleteLayerUpload',
            'ecr:PutImage',
          ],
          resources: [
            `arn:aws:ecr:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:repository/*`,
          ],
        }),
        new aws_iam.PolicyStatement({
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        }),
      ],
    });
    const inlinePolicies = {
      AllowInterface: interfacePolicy,
      AllowClusterUpdate: clusterPolicy,
      AllowEcrRepositoryPolicy: ecrPolicy,
    };
    this.clusterExecutionRole = this.createExecutionRole(
      'ExecutionRole',
      inlinePolicies,
      clusterLogsGroup,
      lifeCycleScriptBucket,
    );
    const clusterExecutionRole = this.clusterExecutionRole;

    // The login group gets its own role only when the VAMS integration is enabled,
    // so that the poller permissions are not granted to the controller and worker nodes.
    // When disabled, the template stays identical to the one without the integration.
    // HyperPod rejects changing the execution role of an existing instance group, so an
    // existing cluster sets Vams.SeparateLoginRole to false and keeps the shared role.
    this.loginExecutionRole =
      isVamsEnabled && props.config.Vams?.SeparateLoginRole !== false
        ? this.createExecutionRole(
            'LoginExecutionRole',
            inlinePolicies,
            clusterLogsGroup,
            lifeCycleScriptBucket,
          )
        : clusterExecutionRole;

    // https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-sagemaker-cluster-clusterinstancegroup.html
    const loginGroup = {
      executionRole: this.loginExecutionRole.roleArn,
      instanceCount: props.config.Cluster.LoginGroup.Count,
      instanceGroupName: props.config.Cluster.LoginGroup.Name,
      instanceType: props.config.Cluster.LoginGroup.InstanceType,
      lifeCycleConfig: {
        onInitComplete: 'on_init_complete.sh',
        sourceS3Uri: `s3://${lifeCycleScriptBucket.bucketName}`,
      },
      instanceStorageConfigs: [
        {
          ebsVolumeConfig: {
            rootVolume: false,
            volumeSizeInGb: 500,
          },
        },
        {
          fsxLustreConfig: {
            dnsName: props.lfs.dnsName,
            mountName: props.lfs.mountName,
            mountPath: '/fsx',
          },
        },
      ],
      overrideVpcConfig: {
        securityGroupIds: [props.lfsSecurityGroup.securityGroupId],
        subnets: [props.privateSubnet.subnetId],
      },
      slurmConfig: {
        nodeType: 'Login',
      },
      threadsPerCore: 2,
    };
    const controllerGroup = {
      executionRole: clusterExecutionRole.roleArn,
      instanceCount: props.config.Cluster.ControllerGroup.Count,
      instanceGroupName: props.config.Cluster.ControllerGroup.Name,
      instanceType: props.config.Cluster.ControllerGroup.InstanceType,
      lifeCycleConfig: {
        onInitComplete: 'on_init_complete.sh',
        sourceS3Uri: `s3://${lifeCycleScriptBucket.bucketName}`,
      },
      instanceStorageConfigs: [
        {
          ebsVolumeConfig: {
            rootVolume: false,
            volumeSizeInGb: 500,
          },
        },
        {
          fsxLustreConfig: {
            dnsName: props.lfs.dnsName,
            mountName: props.lfs.mountName,
            mountPath: '/fsx',
          },
        },
      ],
      overrideVpcConfig: {
        securityGroupIds: [props.lfsSecurityGroup.securityGroupId],
        subnets: [props.privateSubnet.subnetId],
      },
      slurmConfig: {
        nodeType: 'Controller',
      },
      threadsPerCore: 2,
    };

    const workerGroup = props.config.Cluster.WorkerGroup.map((value, index) => ({
      executionRole: clusterExecutionRole.roleArn,
      instanceCount: value.Count,
      instanceGroupName: value.Name,
      instanceType: value.InstanceType,
      lifeCycleConfig: {
        onInitComplete: 'on_init_complete.sh',
        sourceS3Uri: `s3://${lifeCycleScriptBucket.bucketName}`,
      },
      instanceStorageConfigs: [
        {
          ebsVolumeConfig: {
            rootVolume: false,
            volumeSizeInGb: 500,
          },
        },
        {
          fsxLustreConfig: {
            dnsName: props.lfs.dnsName,
            mountName: props.lfs.mountName,
            mountPath: '/fsx',
          },
        },
      ],
      overrideVpcConfig: {
        securityGroupIds: [props.lfsSecurityGroup.securityGroupId],
        subnets: [props.privateSubnet.subnetId],
      },
      slurmConfig: {
        nodeType: 'Compute',
        partitionNames: [value.Name],
      },
      threadsPerCore: 2,
    }));

    const cfnCluster = new aws_sagemaker.CfnCluster(this, 'Model', {
      clusterName: props.config.Cluster.Name,
      instanceGroups: [loginGroup, controllerGroup, ...workerGroup],
      nodeRecovery: 'Automatic',
      vpcConfig: {
        securityGroupIds: [props.lfsSecurityGroup.securityGroupId],
        subnets: [props.privateSubnet.subnetId],
      },
    });

    // Ensure cluster is created after lifecycle scripts are uploaded
    cfnCluster.node.addDependency(loadLifeCycleScriptResource);
    if (vamsPollerDeployment !== undefined) {
      cfnCluster.node.addDependency(vamsPollerDeployment);
    }

    new cdk.CfnOutput(this, 'ClusterId', {
      value: props.config.Cluster.Name,
      description: 'SageMaker HyperPod cluster name',
    });
    new cdk.CfnOutput(this, 'LoginGroupName', {
      value: props.config.Cluster.LoginGroup.Name,
      description: 'SageMaker HyperPod login group node name',
    });
    new cdk.CfnOutput(this, 'ClusterExecutionRoleARN', {
      value: clusterExecutionRole.roleArn,
      description: 'Role ARN for group',
    });
    if (this.loginExecutionRole !== clusterExecutionRole) {
      new cdk.CfnOutput(this, 'LoginExecutionRoleARN', {
        value: this.loginExecutionRole.roleArn,
        description: 'Role ARN for the login group',
      });
    }
    new cdk.CfnOutput(this, 'ClusterSecurityGroup', {
      value: props.lfsSecurityGroup.securityGroupId,
      description: 'Security group attached to the instance',
    });
    new cdk.CfnOutput(this, 'ClusterSubnet', {
      value: props.privateSubnet.subnetId,
      description: 'Subnet Id to launch the node',
    });
  }

  /**
   * Build the lifecycle loader environment that fills the placeholders of install_vams_poller.sh.
   * Each key NAME replaces the placeholder __NAME__. The numeric settings are required, so that a
   * missing value fails the synthesis instead of falling back to a value the user did not choose.
   *
   * @param config Stack configuration.
   * @param vamsPoller Values of the resources created by the VAMS integration.
   * @returns Environment variables of the lifecycle loader.
   */
  private vamsPollerEnvironment(config: Configuration, vamsPoller: VamsPollerProps): Record<string, string> {
    const vams = vamsPoller.config;
    const requiredNumberString = (value: number | undefined, name: string): string =>
      String(requireNumber(value, name));
    return {
      VAMS_ENABLED: 'true',
      LOGIN_GROUP_NAME: config.Cluster.LoginGroup.Name,
      VAMS_QUEUE_URL: vamsPoller.queueUrl,
      VAMS_TABLE_NAME: vamsPoller.tableName,
      VAMS_DATA_BUCKET: vamsPoller.dataBucketName,
      LUSTRE_S3_PREFIX: config.Lustre.S3Prefix,
      VAMS_REGION: cdk.Stack.of(this).region,
      VAMS_CONFIG_NAME: requireNonEmpty(vams.ConfigName, 'ConfigName'),
      VAMS_VISIBILITY_TIMEOUT_SECONDS: requiredNumberString(
        vams.QueueVisibilityTimeoutSeconds,
        'QueueVisibilityTimeoutSeconds',
      ),
      VAMS_HEARTBEAT_INTERVAL_SECONDS: requiredNumberString(vams.HeartbeatIntervalSeconds, 'HeartbeatIntervalSeconds'),
      VAMS_ABORT_GRACE_SECONDS: requiredNumberString(vams.AbortGraceSeconds, 'AbortGraceSeconds'),
      VAMS_LEASE_SECONDS: requiredNumberString(vams.LeaseSeconds, 'LeaseSeconds'),
      VAMS_ITEM_TTL_DAYS: requiredNumberString(vams.ItemTtlDays, 'ItemTtlDays'),
      VAMS_INGEST_WAIT_SECONDS: requiredNumberString(vams.IngestWaitSeconds, 'IngestWaitSeconds'),
      VAMS_MAX_CONCURRENT_RUNS: requiredNumberString(vams.MaxConcurrentRuns, 'MaxConcurrentRuns'),
      // An empty partition lets Slurm use its default partition.
      VAMS_SLURM_PARTITION: vams.SlurmPartition ?? '',
      VAMS_MLFLOW_EXPERIMENT_NAME: requireNonEmpty(vams.MlflowExperimentName ?? '', 'MlflowExperimentName'),
    };
  }

  /**
   * Create an execution role for HyperPod instance groups with the permissions shared by all groups.
   *
   * @param id Construct id of the role.
   * @param inlinePolicies Inline policies attached to the role.
   * @param clusterLogsGroup Log group the nodes write to.
   * @param lifeCycleScriptBucket Bucket holding the lifecycle scripts.
   * @returns Created role.
   */
  private createExecutionRole(
    id: string,
    inlinePolicies: Record<string, aws_iam.PolicyDocument>,
    clusterLogsGroup: aws_logs.LogGroup,
    lifeCycleScriptBucket: aws_s3.Bucket,
  ): aws_iam.Role {
    const role = new aws_iam.Role(this, id, {
      assumedBy: new aws_iam.ServicePrincipal('sagemaker.amazonaws.com'),
      managedPolicies: [
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerClusterInstanceRolePolicy'),
      ],
      inlinePolicies,
    });
    clusterLogsGroup.grantWrite(role);
    lifeCycleScriptBucket.grantReadWrite(role);
    return role;
  }
}
