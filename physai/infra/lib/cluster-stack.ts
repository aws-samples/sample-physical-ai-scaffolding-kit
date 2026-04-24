import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as fsx from "aws-cdk-lib/aws-fsx";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

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
}

function hashDirectory(dir: string): string {
  const hash = crypto.createHash("sha256");
  const entries = fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if ([".mypy_cache", "__pycache__"].includes(entry.name)) continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      hash.update(entry.name);
      hash.update(hashDirectory(p));
    } else if (entry.isFile() && !entry.name.endsWith(".pyc")) {
      hash.update(entry.name);
      hash.update(fs.readFileSync(p));
    }
  }
  return hash.digest("hex");
}

export class ClusterStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ClusterStackProps) {
    super(scope, id, props);
    const {
      clusterName: baseClusterName, vpc, privateSubnet, clusterSg,
      dataBucket, fsxDnsName, fsxMountName,
      dbEndpoint, dbSecret,
      gpuWorkers, cpuWorkerType, cpuWorkerCount,
    } = props;

    const account = cdk.Stack.of(this).account;

    // Derive a per-stack-creation suffix from the first 8 chars of the
    // CloudFormation stack's GUID. AWS::StackId is stable across stack
    // updates but unique per stack creation, so every destroy+redeploy of
    // ClusterStack produces a new `clusterName` (and thus a distinct identity
    // in Slurm accounting). Simple stack updates keep the same name.
    // AWS::StackId format: arn:...:stack/<name>/<guid>
    const stackGuid = cdk.Fn.select(2, cdk.Fn.split("/", this.stackId));
    const stackSuffix = cdk.Fn.select(0, cdk.Fn.split("-", stackGuid));
    const clusterName = `${baseClusterName}-${stackSuffix}`;

    // ── Lifecycle Scripts Bucket ──

    const lifecycleBucket = new s3.Bucket(this, "LifecycleBucket", {
      bucketName: `${clusterName}-lifecycle-${account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // Deploy lifecycle scripts from infra/lifecycle/ under a content-hashed
    // prefix so SourceS3Uri changes whenever script content changes. This
    // triggers CloudFormation to call UpdateCluster, which is required for
    // HyperPod to pick up new scripts (node replacement alone does not
    // re-fetch from S3). Old prefixes are left behind — they're tiny.
    const lifecycleDir = path.join(__dirname, "..", "lifecycle");
    const lifecycleHash = hashDirectory(lifecycleDir).slice(0, 12);
    const lifecyclePrefix = `lifecycle/${lifecycleHash}`;

    new s3deploy.BucketDeployment(this, "DeployLifecycleScripts", {
      sources: [
        s3deploy.Source.asset(lifecycleDir, {
          exclude: [".mypy_cache/**", "__pycache__/**", "*.pyc"],
        }),
        s3deploy.Source.jsonData("physai-config.json", {
          rds_endpoint: dbEndpoint,
          rds_port: 3306,
          rds_database: "slurm_acct_db",
          secret_arn: dbSecret.secretArn,
        }),
      ],
      destinationBucket: lifecycleBucket,
      destinationKeyPrefix: lifecyclePrefix,
      prune: false,
    });

    const lifecycleS3Uri = `s3://${lifecycleBucket.bucketName}/${lifecyclePrefix}/`;

    // ── IAM Execution Role ──

    const executionRole = new iam.Role(this, "ExecutionRole", {
      roleName: `${clusterName}-ExecutionRole`,
      assumedBy: new iam.ServicePrincipal("sagemaker.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSageMakerClusterInstanceRolePolicy"),
      ],
    });

    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission",
        "ec2:DeleteNetworkInterface", "ec2:DeleteNetworkInterfacePermission",
        "ec2:DescribeNetworkInterfaces", "ec2:DescribeVpcs", "ec2:DescribeDhcpOptions",
        "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DetachNetworkInterface",
        "ec2:CreateTags",
      ],
      resources: ["*"],
    }));

    dataBucket.grantReadWrite(executionRole);
    lifecycleBucket.grantRead(executionRole);

    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["fsx:DescribeFileSystems"],
      resources: ["*"],
    }));

    dbSecret.grantRead(executionRole);

    // CloudWatch Logs for cluster
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        "logs:CreateLogGroup", "logs:CreateLogStream",
        "logs:PutLogEvents", "logs:DescribeLogStreams",
      ],
      resources: [`arn:aws:logs:${this.region}:${account}:log-group:/aws/sagemaker/*`],
    }));

    // ── HyperPod Cluster ──

    const fsxConfig = {
      FsxLustreConfig: {
        DnsName: fsxDnsName,
        MountName: fsxMountName,
        MountPath: "/fsx",
      },
    };

    const lifecycleConfig = {
      SourceS3Uri: lifecycleS3Uri,
      OnCreate: "on_create.sh",
    };

    const fixedGroups = [
      {
        InstanceGroupName: "controller-machine",
        InstanceType: "ml.c5.large",
        InstanceCount: 1,
        LifeCycleConfig: lifecycleConfig,
        ExecutionRole: executionRole.roleArn,
        ThreadsPerCore: 1,
        SlurmConfig: { NodeType: "Controller" },
        InstanceStorageConfigs: [fsxConfig],
      },
      {
        InstanceGroupName: "login-group",
        InstanceType: "ml.c5.large",
        InstanceCount: 1,
        LifeCycleConfig: lifecycleConfig,
        ExecutionRole: executionRole.roleArn,
        ThreadsPerCore: 1,
        SlurmConfig: { NodeType: "Login" },
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
      SlurmConfig: { NodeType: "Compute", PartitionNames: ["gpu"] },
      InstanceStorageConfigs: [fsxConfig],
    }));

    const cpuGroup = {
      InstanceGroupName: "cpu-workers",
      InstanceType: cpuWorkerType,
      InstanceCount: cpuWorkerCount,
      LifeCycleConfig: lifecycleConfig,
      ExecutionRole: executionRole.roleArn,
      ThreadsPerCore: 1,
      SlurmConfig: { NodeType: "Compute", PartitionNames: ["cpu"] },
      InstanceStorageConfigs: [fsxConfig],
    };

    const cluster = new cdk.CfnResource(this, "HyperPodCluster", {
      type: "AWS::SageMaker::Cluster",
      properties: {
        ClusterName: clusterName,
        InstanceGroups: [...fixedGroups, ...gpuGroups, cpuGroup],
        VpcConfig: {
          SecurityGroupIds: [clusterSg.securityGroupId],
          Subnets: [privateSubnet.subnetId],
        },
        Orchestrator: {
          Slurm: {
            SlurmConfigStrategy: "Merge",
          },
        },
        NodeRecovery: "Automatic",
      },
    });
    cluster.node.addDependency(executionRole);

    new cdk.CfnOutput(this, "ClusterName", {
      value: clusterName,
      exportName: `${this.stackName}-ClusterName`,
    });

    // ── CloudWatch Alarm ──

    new cloudwatch.Alarm(this, "FsxCapacityAlarm", {
      alarmName: `${clusterName}-fsx-low-capacity`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/FSx",
        metricName: "FreeStorageCapacity",
        dimensionsMap: {
          FileSystemId: props.fsxFileSystem.ref,
        },
        period: cdk.Duration.minutes(5),
        statistic: "Average",
      }),
      threshold: 100 * 1024 * 1024 * 1024, // 100 GiB in bytes
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.MISSING,
    });
  }
}
