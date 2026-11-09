import * as cdk from 'aws-cdk-lib';
import { aws_s3, aws_iam, aws_sagemaker } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface MlflowProps {
  /** Destination of the S3 server access logs of the artifact bucket. */
  accessLogsBucket: aws_s3.IBucket;
  appName?: string;
  weeklyMaintenanceWindowStart?: string;
  artifactPrefix?: string;
}

export class Mlflow extends Construct {
  public readonly artifactBucket: aws_s3.Bucket;
  public readonly app: aws_sagemaker.CfnMlflowApp;
  public readonly appArn: string;

  constructor(scope: Construct, id: string, props: MlflowProps) {
    super(scope, id);

    const appName = props.appName ?? 'pask-mlflow';
    const weeklyMaintenanceWindowStart =
      props.weeklyMaintenanceWindowStart ?? 'Sun:18:00';
    const artifactPrefix = props.artifactPrefix ?? 'mlflow';

    this.artifactBucket = new aws_s3.Bucket(this, 'Artifacts', {
      accessControl: aws_s3.BucketAccessControl.PRIVATE,
      blockPublicAccess: aws_s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: aws_s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: props.accessLogsBucket,
      serverAccessLogsPrefix: 'mlflow-artifacts/',
    });

    const appRole = new aws_iam.Role(this, 'AppRole', {
      assumedBy: new aws_iam.ServicePrincipal('sagemaker.amazonaws.com'),
    });
    this.artifactBucket.grantReadWrite(appRole);

    // Changing name or roleArn replaces the MLflow App and its tracking server.
    this.app = new aws_sagemaker.CfnMlflowApp(this, 'App', {
      name: appName,
      artifactStoreUri: `s3://${this.artifactBucket.bucketName}/${artifactPrefix}`,
      roleArn: appRole.roleArn,
      modelRegistrationMode: 'AutoModelRegistrationDisabled',
      weeklyMaintenanceWindowStart,
    });
    this.app.node.addDependency(appRole);
    this.appArn = this.app.attrArn;

    const appArnOutput = new cdk.CfnOutput(this, 'MlflowAppArn', {
      value: this.app.attrArn,
      description: 'SageMaker MLflow App ARN (MLFLOW_TRACKING_URI)',
    });
    appArnOutput.overrideLogicalId('MlflowAppArn');

    const artifactBucketOutput = new cdk.CfnOutput(
      this,
      'MlflowArtifactBucketName',
      {
        value: this.artifactBucket.bucketName,
        description: 'MLflow artifact store bucket',
      },
    );
    artifactBucketOutput.overrideLogicalId('MlflowArtifactBucketName');
  }
}
