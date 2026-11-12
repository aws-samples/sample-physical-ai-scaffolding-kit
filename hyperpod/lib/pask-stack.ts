import * as cdk from 'aws-cdk-lib';
import { aws_iam } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { S3Bucket } from './constructs/s3';
import { Configuration } from './types/configurations';
import { VpcOneAz } from './constructs/vpc';
import { Lustre } from './constructs/lustre';
import { HyperPod } from './constructs/hyperpod';
import { Mlflow } from './constructs/mlflow';

export interface PASKStackProps extends cdk.StackProps {
  config: Configuration;
}
export class PASKStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PASKStackProps) {
    super(scope, id, props);

    const bucket = new S3Bucket(this, 'Bucket');

    const clusterVpc = new VpcOneAz(this, 'Cluster', {
      config: props.config,
    });
    const lustre = new Lustre(this, 'Lustre', {
      config: props.config,
      dataBucket: bucket.dataBucket,
      vpc: clusterVpc.vpc,
      privateSubnet: clusterVpc.privateSubnet,
      lsfSecurityGroup: clusterVpc.lsfSecurityGroup,
    });
    const hyperpodCluster = new HyperPod(this, 'HyperPod', {
      config: props.config,
      vpc: clusterVpc.vpc,
      privateSubnet: clusterVpc.privateSubnet,
      lfsSecurityGroup: clusterVpc.lsfSecurityGroup,
      lfs: lustre.fs,
      accessLogsBucket: bucket.accessLogsBucket,
    });

    if (props.config.Mlflow?.Enabled === true) {
      const mlflow = new Mlflow(this, 'Mlflow', { accessLogsBucket: bucket.accessLogsBucket });
      hyperpodCluster.clusterExecutionRole.addToPolicy(
        new aws_iam.PolicyStatement({
          actions: ['sagemaker:CallMlflowAppApi', 'sagemaker:DescribeMlflowApp'],
          resources: [mlflow.appArn],
        }),
      );
      mlflow.artifactBucket.grantReadWrite(hyperpodCluster.clusterExecutionRole);
    }

    new cdk.CfnOutput(this, 'S3LinkUri', {
      value: `s3://${bucket.dataBucket.bucketName}/${props.config.Lustre.S3Prefix}`,
      description: 'S3 URI linked to the Lustre S3 link path',
    });

    new cdk.CfnOutput(this, 'S3LinkMountPath', {
      value: `/fsx${props.config.Lustre.FileSystemPath}`,
      description: 'Lustre path on cluster nodes that is linked to S3',
    });

    new cdk.CfnOutput(this, 'Region', {
      value: process.env.CDK_DEFAULT_REGION!,
      description: 'Deployed region',
    });
  }
}
