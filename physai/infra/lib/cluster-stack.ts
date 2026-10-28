import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as fsx from 'aws-cdk-lib/aws-fsx';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { VPC_CIDR } from './infra-stack';

export const LOGIN_GROUP_NAME = 'login-group';

export interface GpuWorkerConfig {
  name: string;
  instanceType: string;
  count: number;
}

export interface ClusterStackProps extends cdk.StackProps {
  clusterName: string;
  vpc: ec2.Vpc;
  privateSubnet: ec2.ISubnet;
  clusterSg: ec2.SecurityGroup;
  dataBucket: s3.Bucket;
  fsxFileSystem: fsx.CfnFileSystem;
  fsxDnsName: string;
  fsxMountName: string;
  dbEndpoint: string;
  dbSecret: secretsmanager.ISecret;
  gpuWorkers: GpuWorkerConfig[];
  cpuWorkerType: string;
  cpuWorkerCount: number;
  jobIntakeEnabled: boolean;
}

function hashDirectory(dir: string): string {
  const hash = crypto.createHash('sha256');
  const entries = fs
    .readdirSync(dir, { withFileTypes: true })
    .sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (['.mypy_cache', '__pycache__'].includes(entry.name)) continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      hash.update(entry.name);
      hash.update(hashDirectory(p));
    } else if (entry.isFile() && !entry.name.endsWith('.pyc')) {
      hash.update(entry.name);
      hash.update(fs.readFileSync(p));
    }
  }
  return hash.digest('hex');
}

export class ClusterStack extends cdk.Stack {
  public readonly clusterName: string;
  public readonly jobIntakeKey?: ec2.KeyPair;

  constructor(scope: Construct, id: string, props: ClusterStackProps) {
    super(scope, id, props);
    const {
      clusterName: baseClusterName,
      vpc,
      privateSubnet,
      clusterSg,
      dataBucket,
      fsxDnsName,
      fsxMountName,
      dbEndpoint,
      dbSecret,
      gpuWorkers,
      cpuWorkerType,
      cpuWorkerCount,
      jobIntakeEnabled,
    } = props;

    const account = cdk.Stack.of(this).account;

    // Derive a per-stack-creation suffix from the first 8 chars of the
    // CloudFormation stack's GUID. AWS::StackId is stable across stack
    // updates but unique per stack creation, so every destroy+redeploy of
    // ClusterStack produces a new `clusterName` (and thus a distinct identity
    // in Slurm accounting). Simple stack updates keep the same name.
    // AWS::StackId format: arn:...:stack/<name>/<guid>
    const stackGuid = cdk.Fn.select(2, cdk.Fn.split('/', this.stackId));
    const stackSuffix = cdk.Fn.select(0, cdk.Fn.split('-', stackGuid));
    this.clusterName = `${baseClusterName}-${stackSuffix}`;

    // Here rather than in PhysaiJobIntakeStack, which deploys after this stack:
    // the login node installs the key's public half as it first boots.
    if (jobIntakeEnabled) {
      this.jobIntakeKey = new ec2.KeyPair(this, 'JobIntakeSshKey', {
        keyPairName: `${this.clusterName}-job-intake`,
        type: ec2.KeyPairType.ED25519,
      });
    }

    // ── Lifecycle Scripts Bucket ──

    const lifecycleBucket = new s3.Bucket(this, 'LifecycleBucket', {
      bucketName: `${this.clusterName}-lifecycle-${account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // Deploy lifecycle scripts from infra/lifecycle/ under a content-hashed
    // prefix so SourceS3Uri changes whenever script content changes. This
    // triggers CloudFormation to call UpdateCluster, which is required for
    // HyperPod to pick up new scripts (node replacement alone does not
    // re-fetch from S3). Old prefixes are left behind — they're tiny.
    const lifecycleDir = path.join(__dirname, '..', 'lifecycle');
    // Job intake changes physai-config.json without changing a script, so
    // turning it on or off has to change the prefix too.
    const lifecycleHash = crypto
      .createHash('sha256')
      .update(hashDirectory(lifecycleDir))
      .update(jobIntakeEnabled ? 'job-intake' : '')
      .digest('hex')
      .slice(0, 12);
    const lifecyclePrefix = `lifecycle/${lifecycleHash}`;

    const lifecycleDeployment = new s3deploy.BucketDeployment(this, 'DeployLifecycleScripts', {
      sources: [
        s3deploy.Source.asset(lifecycleDir, {
          exclude: ['.mypy_cache/**', '__pycache__/**', '*.pyc'],
        }),
        s3deploy.Source.jsonData('physai-config.json', {
          rds_endpoint: dbEndpoint,
          rds_port: 3306,
          rds_database: 'slurm_acct_db',
          secret_arn: dbSecret.secretArn,
          ...(this.jobIntakeKey && {
            job_intake_key_pair_id: this.jobIntakeKey.keyPairId,
            job_intake_key_name: this.jobIntakeKey.keyPairName,
            vpc_cidr: VPC_CIDR,
          }),
        }),
      ],
      destinationBucket: lifecycleBucket,
      destinationKeyPrefix: lifecyclePrefix,
      prune: false,
    });

    const lifecycleS3Uri = `s3://${lifecycleBucket.bucketName}/${lifecyclePrefix}/`;

    // ── IAM Execution Role ──

    const executionRole = new iam.Role(this, 'ExecutionRole', {
      roleName: `${this.clusterName}-ExecutionRole`,
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerClusterInstanceRolePolicy'),
      ],
    });

    executionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ec2:CreateNetworkInterface',
          'ec2:CreateNetworkInterfacePermission',
          'ec2:DeleteNetworkInterface',
          'ec2:DeleteNetworkInterfacePermission',
          'ec2:DescribeNetworkInterfaces',
          'ec2:DescribeVpcs',
          'ec2:DescribeDhcpOptions',
          'ec2:DescribeSubnets',
          'ec2:DescribeSecurityGroups',
          'ec2:DetachNetworkInterface',
          'ec2:CreateTags',
        ],
        resources: ['*'],
      }),
    );

    dataBucket.grantReadWrite(executionRole);
    lifecycleBucket.grantRead(executionRole);

    executionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['fsx:DescribeFileSystems'],
        resources: ['*'],
      }),
    );

    dbSecret.grantRead(executionRole);

    // For install_job_intake_key.sh. EC2 offers no resource-level permissions
    // for this action, so it cannot be narrowed to one key pair; the script
    // asks only for the id in its config.
    if (this.jobIntakeKey) {
      executionRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'JobIntakeDescribeKeyPair',
          actions: ['ec2:DescribeKeyPairs'],
          resources: ['*'],
        }),
      );
    }

    // DCV license bucket (automatic EC2 licensing)
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DcvLicense',
        actions: ['s3:GetObject'],
        resources: [`arn:aws:s3:::dcv-license.${this.region}/*`],
      }),
    );

    // CloudWatch Logs for cluster
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:DescribeLogStreams',
        ],
        resources: [`arn:aws:logs:${this.region}:${account}:log-group:/aws/sagemaker/*`],
      }),
    );

    // ── HyperPod Cluster ──

    const fsxConfig = {
      FsxLustreConfig: {
        DnsName: fsxDnsName,
        MountName: fsxMountName,
        MountPath: '/fsx',
      },
    };

    const lifecycleConfig = {
      SourceS3Uri: lifecycleS3Uri,
      OnCreate: 'on_create.sh',
    };

    const fixedGroups = [
      {
        InstanceGroupName: 'controller-machine',
        InstanceType: 'ml.c5.large',
        InstanceCount: 1,
        LifeCycleConfig: lifecycleConfig,
        ExecutionRole: executionRole.roleArn,
        ThreadsPerCore: 1,
        SlurmConfig: { NodeType: 'Controller' },
        InstanceStorageConfigs: [fsxConfig],
      },
      {
        InstanceGroupName: LOGIN_GROUP_NAME,
        InstanceType: 'ml.c5.large',
        InstanceCount: 1,
        LifeCycleConfig: lifecycleConfig,
        ExecutionRole: executionRole.roleArn,
        ThreadsPerCore: 1,
        SlurmConfig: { NodeType: 'Login' },
        InstanceStorageConfigs: [fsxConfig],
      },
    ];

    const gpuGroups = gpuWorkers.map((w) => ({
      InstanceGroupName: w.name,
      InstanceType: w.instanceType,
      InstanceCount: w.count,
      LifeCycleConfig: lifecycleConfig,
      ExecutionRole: executionRole.roleArn,
      ThreadsPerCore: 1,
      SlurmConfig: { NodeType: 'Compute', PartitionNames: ['gpu'] },
      InstanceStorageConfigs: [fsxConfig],
    }));

    const cpuGroup = {
      InstanceGroupName: 'cpu-workers',
      InstanceType: cpuWorkerType,
      InstanceCount: cpuWorkerCount,
      LifeCycleConfig: lifecycleConfig,
      ExecutionRole: executionRole.roleArn,
      ThreadsPerCore: 1,
      SlurmConfig: { NodeType: 'Compute', PartitionNames: ['cpu'] },
      InstanceStorageConfigs: [fsxConfig],
    };

    const cluster = new cdk.CfnResource(this, 'HyperPodCluster', {
      type: 'AWS::SageMaker::Cluster',
      properties: {
        ClusterName: this.clusterName,
        InstanceGroups: [...fixedGroups, ...gpuGroups, cpuGroup],
        VpcConfig: {
          SecurityGroupIds: [clusterSg.securityGroupId],
          Subnets: [privateSubnet.subnetId],
        },
        Orchestrator: {
          Slurm: {
            SlurmConfigStrategy: 'Merge',
          },
        },
        NodeRecovery: 'Automatic',
      },
    });
    cluster.node.addDependency(executionRole);
    // Nodes read the lifecycle scripts and physai-config.json as they boot.
    cluster.node.addDependency(lifecycleDeployment);

    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.clusterName,
      exportName: `${this.stackName}-ClusterName`,
    });

    // ── CloudWatch Alarm ──

    new cloudwatch.Alarm(this, 'FsxCapacityAlarm', {
      alarmName: `${this.clusterName}-fsx-low-capacity`,
      metric: new cloudwatch.Metric({
        namespace: 'AWS/FSx',
        metricName: 'FreeStorageCapacity',
        dimensionsMap: {
          FileSystemId: props.fsxFileSystem.ref,
        },
        period: cdk.Duration.minutes(5),
        statistic: 'Average',
      }),
      threshold: 100 * 1024 * 1024 * 1024, // 100 GiB in bytes
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.MISSING,
    });
  }
}
