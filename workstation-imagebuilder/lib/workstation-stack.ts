import * as path from "path";
import * as cdk from "aws-cdk-lib/core";
import {
  CfnOutput,
  Aws,
  aws_ec2,
  aws_iam,
  aws_s3,
  aws_s3files,
  aws_lambda,
  aws_logs,
} from "aws-cdk-lib";
import { Construct } from "constructs";
import { Configuration } from "./types/configurations";
import { Vpc } from "./constructs/vpc";

export interface WorkstationStackProps extends cdk.StackProps {
  config: Configuration;
  amiParameterPrefix: string;
}

export class WorkstationStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: WorkstationStackProps) {
    super(scope, id, props);

    const { config, amiParameterPrefix } = props;

    // VPC
    const simulatorVpc = new Vpc(this, "Simulator", {
      vpcId: config.VpcId,
    });

    // Security Group for workstation instances
    const securityGroup = new aws_ec2.SecurityGroup(this, "WorkstationSG", {
      vpc: simulatorVpc.vpc,
      description: "Allow DCV and TensorBoard access",
      allowAllOutbound: true,
    });
    securityGroup.addIngressRule(
      aws_ec2.Peer.anyIpv4(),
      aws_ec2.Port.tcp(8443),
      "Allow Amazon DCV access (TCP/WebSocket)",
    );
    securityGroup.addIngressRule(
      aws_ec2.Peer.anyIpv4(),
      aws_ec2.Port.udp(8443),
      "Allow Amazon DCV access (QUIC/UDP)",
    );
    securityGroup.addIngressRule(
      aws_ec2.Peer.anyIpv4(),
      aws_ec2.Port.tcp(6006),
      "Allow TensorBoard access",
    );

    // IAM Role for workstation instances
    const instanceRole = new aws_iam.Role(this, "InstanceRole", {
      assumedBy: new aws_iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonS3ReadOnlyAccess",
        ),
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonSSMManagedInstanceCore",
        ),
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonEC2ContainerRegistryPowerUser",
        ),
      ],
    });

    const instanceProfile = new aws_iam.CfnInstanceProfile(
      this,
      "InstanceProfile",
      {
        roles: [instanceRole.roleName],
      },
    );

    // S3 Files file system
    const s3FilesBucket = new aws_s3.Bucket(this, "S3FilesBucket", {
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const s3FilesRole = new aws_iam.Role(this, "S3FilesRole", {
      assumedBy: new aws_iam.ServicePrincipal(
        "elasticfilesystem.amazonaws.com",
      ),
    });
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["s3:ListBucket", "s3:ListBucketMultipartUploads"],
        resources: [s3FilesBucket.bucketArn],
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          "s3:AbortMultipartUpload",
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:GetObjectAttributes",
          "s3:ListMultipartUploadParts",
          "s3:PutObject",
        ],
        resources: [s3FilesBucket.arnForObjects("*")],
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          "events:DeleteRule",
          "events:DisableRule",
          "events:EnableRule",
          "events:PutRule",
          "events:PutTargets",
          "events:RemoveTargets",
        ],
        resources: [
          `arn:${Aws.PARTITION}:events:*:*:rule/DO-NOT-DELETE-S3-Files*`,
        ],
        conditions: {
          StringEquals: {
            "events:ManagedBy": "elasticfilesystem.amazonaws.com",
          },
        },
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          "events:DescribeRule",
          "events:ListRuleNamesByTarget",
          "events:ListRules",
          "events:ListTargetsByRule",
        ],
        resources: [`arn:${Aws.PARTITION}:events:*:*:rule/*`],
      }),
    );

    const s3FilesFs = new aws_s3files.CfnFileSystem(this, "S3FilesFileSystem", {
      bucket: s3FilesBucket.bucketArn,
      roleArn: s3FilesRole.roleArn,
    });

    // S3 Files mount targets
    const s3FilesMountSg = new aws_ec2.SecurityGroup(this, "S3FilesMountSG", {
      vpc: simulatorVpc.vpc,
      description: "Allow NFS access for S3 Files mount targets",
      allowAllOutbound: true,
    });
    s3FilesMountSg.addIngressRule(
      securityGroup,
      aws_ec2.Port.tcp(2049),
      "Allow NFS from workstation instances",
    );

    const publicSubnets = simulatorVpc.vpc.publicSubnets;
    publicSubnets.forEach(
      (subnet, i) =>
        new aws_s3files.CfnMountTarget(this, `S3FilesMountTarget${i}`, {
          fileSystemId: s3FilesFs.attrFileSystemId,
          subnetId: subnet.subnetId,
          securityGroups: [s3FilesMountSg.securityGroupId],
        }),
    );

    // Launch Lambda
    const launchLambdaLogGroup = new aws_logs.LogGroup(
      this,
      "LaunchLambdaLogGroup",
      {
        retention: aws_logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      },
    );

    const launchLambdaRole = new aws_iam.Role(this, "LaunchLambdaRole", {
      assumedBy: new aws_iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    launchLambdaLogGroup.grantWrite(launchLambdaRole);

    launchLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          "ec2:RunInstances",
          "ec2:CreateTags",
          "ec2:AllocateAddress",
          "ec2:AssociateAddress",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeSubnets",
          "ec2:DescribeInstanceTypeOfferings",
        ],
        resources: ["*"],
      }),
    );
    launchLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:${Aws.PARTITION}:ssm:${this.region}:${this.account}:parameter${amiParameterPrefix}*`,
        ],
      }),
    );
    launchLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [instanceRole.roleArn],
      }),
    );

    const vpcSubnetIds = publicSubnets.map((s) => s.subnetId).join(",");

    // Connect Lambda (DCV session token generation)
    const connectLambdaLogGroup = new aws_logs.LogGroup(
      this,
      "ConnectLambdaLogGroup",
      {
        retention: aws_logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      },
    );

    const connectLambdaRole = new aws_iam.Role(this, "ConnectLambdaRole", {
      assumedBy: new aws_iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    connectLambdaLogGroup.grantWrite(connectLambdaRole);

    connectLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["ec2:DescribeInstances"],
        resources: ["*"],
      }),
    );
    connectLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["ssm:SendCommand"],
        resources: [
          `arn:${Aws.PARTITION}:ssm:${this.region}::document/AWS-RunShellScript`,
          `arn:${Aws.PARTITION}:ec2:${this.region}:${this.account}:instance/*`,
        ],
      }),
    );
    connectLambdaRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ["ssm:GetCommandInvocation"],
        resources: ["*"],
      }),
    );

    const connectFunction = new aws_lambda.Function(this, "ConnectFunction", {
      runtime: aws_lambda.Runtime.PYTHON_3_14,
      handler: "index.handler",
      role: connectLambdaRole,
      code: aws_lambda.Code.fromAsset(
        path.join(__dirname, "lambda/connect-workstation"),
      ),
      timeout: cdk.Duration.minutes(2),
      memorySize: 128,
      logGroup: connectLambdaLogGroup,
    });

    // Launch Lambda
    const launchFunction = new aws_lambda.Function(this, "LaunchFunction", {
      runtime: aws_lambda.Runtime.PYTHON_3_14,
      handler: "index.handler",
      role: launchLambdaRole,
      code: aws_lambda.Code.fromAsset(
        path.join(__dirname, "lambda/launch-workstation"),
      ),
      timeout: cdk.Duration.minutes(10),
      memorySize: 256,
      logGroup: launchLambdaLogGroup,
      environment: {
        DEFAULT_INSTANCE_TYPE: config.DefaultInstanceType,
        AMI_PARAMETER_PREFIX: amiParameterPrefix,
        VPC_SUBNET_IDS: vpcSubnetIds,
        SECURITY_GROUP_ID: securityGroup.securityGroupId,
        INSTANCE_PROFILE_ARN: instanceProfile.attrArn,
        S3FILES_FS_ID: s3FilesFs.attrFileSystemId,
        CONNECT_FUNCTION_NAME: connectFunction.functionName,
      },
    });

    // Outputs
    new CfnOutput(this, "LaunchFunctionName", {
      value: launchFunction.functionName,
      description: "Lambda function name to launch workstation instances",
    });
    new CfnOutput(this, "ConnectFunctionName", {
      value: connectFunction.functionName,
      description: "Lambda function name to get DCV session token",
    });
    new CfnOutput(this, "S3FilesBucketName", {
      value: s3FilesBucket.bucketName,
      description: "S3 bucket backing the S3 Files file system",
    });
    new CfnOutput(this, "S3FilesFileSystemId", {
      value: s3FilesFs.attrFileSystemId,
      description: "S3 Files file system ID (mounted at /mnt/s3files)",
    });
    new CfnOutput(this, "SecurityGroupId", {
      value: securityGroup.securityGroupId,
      description: "Security group for workstation instances",
    });
  }
}
