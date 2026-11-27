import * as cdk from 'aws-cdk-lib';
import {
  aws_logs,
  aws_s3,
  aws_iam,
  aws_ec2,
  aws_lambda,
  aws_fsx,
  aws_sagemaker,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Configuration } from '../types/configurations';
import * as path from 'path';

export interface HyperPodProps {
  config: Configuration;
  vpc: aws_ec2.IVpc;
  privateSubnet: aws_ec2.PrivateSubnet;
  lfsSecurityGroup: aws_ec2.SecurityGroup;
  lfs: aws_fsx.LustreFileSystem;
  /** Destination of the S3 server access logs of the lifecycle script bucket. */
  accessLogsBucket: aws_s3.IBucket;
}

/**
 * Commit of awslabs/awsome-distributed-ai that the lifecycle scripts are fetched from.
 * The scripts run as root on every cluster node, so a reviewed commit is pinned instead
 * of a branch. Update this value to take in upstream changes.
 */
const LIFECYCLE_SCRIPTS_GITHUB_REF = '240279b4a8f9c12297c72ef1bee37ad3a2558884';

export class HyperPod extends Construct {
  constructor(scope: Construct, id: string, props: HyperPodProps) {
    super(scope, id);

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
        },
      },
    );
    loadLifeCycleScriptResource.node.addDependency(lifeCycleScriptBucket);

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
    const clusterExecutionRole = new aws_iam.Role(this, 'ExecutionRole', {
      assumedBy: new aws_iam.ServicePrincipal('sagemaker.amazonaws.com'),
      managedPolicies: [
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerClusterInstanceRolePolicy'),
      ],
      inlinePolicies: {
        AllowInterface: interfacePolicy,
        AllowClusterUpdate: clusterPolicy,
        AllowEcrRepositoryPolicy: ecrPolicy,
      },
    });

    clusterLogsGroup.grantWrite(clusterExecutionRole);
    lifeCycleScriptBucket.grantReadWrite(clusterExecutionRole);

    // https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-sagemaker-cluster-clusterinstancegroup.html
    const loginGroup = {
      executionRole: clusterExecutionRole.roleArn,
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
    new cdk.CfnOutput(this, 'ClusterSecurityGroup', {
      value: props.lfsSecurityGroup.securityGroupId,
      description: 'Security group attached to the instance',
    });
    new cdk.CfnOutput(this, 'ClusterSubnet', {
      value: props.privateSubnet.subnetId,
      description: 'Subnet Id to launch the node',
    });
  }
}
