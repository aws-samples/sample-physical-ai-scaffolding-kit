import * as cdk from 'aws-cdk-lib';
import { aws_s3 } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export class S3Bucket extends Construct {
  public readonly dataBucket: aws_s3.Bucket;
  /** Destination of the S3 server access logs of the buckets in this stack. */
  public readonly accessLogsBucket: aws_s3.Bucket;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.accessLogsBucket = new aws_s3.Bucket(this, 'AccessLogs', {
      blockPublicAccess: aws_s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: aws_s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      objectOwnership: aws_s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
    });

    this.dataBucket = new aws_s3.Bucket(this, 'Data', {
      accessControl: aws_s3.BucketAccessControl.PRIVATE,
      blockPublicAccess: aws_s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: aws_s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: 'data/',
    });

    new cdk.CfnOutput(this, 'DataBucketName', {
      value: this.dataBucket.bucketName,
      description: 'Data Bucket',
    });
  }
}
