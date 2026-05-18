import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import {
  Stack,
  CfnOutput,
  Aws,
  aws_ec2,
  aws_iam,
  aws_s3,
  aws_s3files,
  aws_lambda,
  aws_logs,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

/**
 * Properties for DcvWorkstation construct.
 */
export interface IsaacSimProps {
  stackPrefix: string;
  readonly vpc: aws_ec2.IVpc;
  instanceType: string;
}

export class IsaacSimWorkstation extends Construct {
  public readonly instanceRole: aws_iam.Role;
  public readonly securityGroup: aws_ec2.SecurityGroup;
  public readonly instance: aws_ec2.Instance;
  public readonly elasticIp: string;

  constructor(scope: Construct, id: string, props: IsaacSimProps) {
    super(scope, id);

    // IAM Role for EC2 instance
    this.instanceRole = new aws_iam.Role(this, 'InstanceRole', {
      assumedBy: new aws_iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonS3ReadOnlyAccess'),
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        aws_iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonEC2ContainerRegistryPowerUser'),
      ],
    });

    // UserData Script
    const userDataPath = path.join(__dirname, 'userdata_script.sh');
    let userDataScript = fs.readFileSync(userDataPath, 'utf-8');

    const region = Stack.of(this).region;
    // S3 Files file system
    const s3FilesBucket = new aws_s3.Bucket(this, 'S3FilesBucket', {
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // IAM role for S3 Files service
    const s3FilesRole = new aws_iam.Role(this, 'S3FilesRole', {
      assumedBy: new aws_iam.ServicePrincipal('elasticfilesystem.amazonaws.com'),
    });
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: ['s3:ListBucket', 's3:ListBucketMultipartUploads'],
        resources: [s3FilesBucket.bucketArn],
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          's3:AbortMultipartUpload',
          's3:DeleteObject',
          's3:GetObject',
          's3:GetObjectAttributes',
          's3:ListMultipartUploadParts',
          's3:PutObject',
        ],
        resources: [s3FilesBucket.arnForObjects('*')],
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          'events:DeleteRule',
          'events:DisableRule',
          'events:EnableRule',
          'events:PutRule',
          'events:PutTargets',
          'events:RemoveTargets',
        ],
        resources: [`arn:${Aws.PARTITION}:events:*:*:rule/DO-NOT-DELETE-S3-Files*`],
        conditions: {
          StringEquals: {
            'events:ManagedBy': 'elasticfilesystem.amazonaws.com',
          },
        },
      }),
    );
    s3FilesRole.addToPolicy(
      new aws_iam.PolicyStatement({
        actions: [
          'events:DescribeRule',
          'events:ListRuleNamesByTarget',
          'events:ListRules',
          'events:ListTargetsByRule',
        ],
        resources: [`arn:${Aws.PARTITION}:events:*:*:rule/*`],
      }),
    );

    // Create S3 Files FileSystem
    const s3FilesFs = new aws_s3files.CfnFileSystem(this, 'S3FilesFileSystem', {
      bucket: s3FilesBucket.bucketArn,
      roleArn: s3FilesRole.roleArn,
    });

    // Inject S3 Files filesystem ID into UserData script
    userDataScript = userDataScript.replace(/__S3FILES_FS_ID__/g, s3FilesFs.attrFileSystemId);

    // Validate no unresolved placeholders
    const unresolved = userDataScript.match(/__[A-Z_]+__/g);
    if (unresolved) {
      throw new Error(
        `Unresolved placeholders in UserData script: ${[...new Set(unresolved)].join(', ')}. ` +
          'This indicates a bug in version replacement logic.',
      );
    }

    // Create UserData object
    const userData = aws_ec2.UserData.forLinux();
    userData.addCommands(userDataScript);

    // Security Group
    this.securityGroup = new aws_ec2.SecurityGroup(this, 'SecurityGroup', {
      vpc: props.vpc,
      description: 'Allow DCV access',
      allowAllOutbound: true,
    });
    this.securityGroup.addIngressRule(
      aws_ec2.Peer.anyIpv4(),
      aws_ec2.Port.tcp(8443),
      'Allow Amazon DCV access',
    );
    this.securityGroup.addIngressRule(
      aws_ec2.Peer.anyIpv4(),
      aws_ec2.Port.tcp(6006),
      'Allow TensorBoard access',
    );

    // S3 Files mount targets (require security group to exist)
    const s3FilesMountSg = new aws_ec2.SecurityGroup(this, 'S3FilesMountSG', {
      vpc: props.vpc,
      description: 'Allow NFS access for S3 Files mount targets',
      allowAllOutbound: true,
    });
    s3FilesMountSg.addIngressRule(
      this.securityGroup,
      aws_ec2.Port.tcp(2049),
      'Allow NFS from DCV instance',
    );

    const publicSubnets = props.vpc.publicSubnets;
    const mountTargets = publicSubnets.map(
      (subnet, i) =>
        new aws_s3files.CfnMountTarget(this, `S3FilesMountTarget${i}`, {
          fileSystemId: s3FilesFs.attrFileSystemId,
          subnetId: subnet.subnetId,
          securityGroups: [s3FilesMountSg.securityGroupId],
        }),
    );

    // AMI Lookup via Custom Resource
    const amiLookupLogGroup = new aws_logs.LogGroup(this, 'AmiLookupLogGroup', {
      retention: aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const amiLookupRole = new aws_iam.Role(this, 'AmiLookupRole', {
      assumedBy: new aws_iam.ServicePrincipal('lambda.amazonaws.com'),
      inlinePolicies: {
        EC2DescribeImages: new aws_iam.PolicyDocument({
          statements: [
            new aws_iam.PolicyStatement({
              actions: ['ec2:DescribeImages'],
              resources: ['*'],
            }),
          ],
        }),
      },
    });
    amiLookupLogGroup.grantWrite(amiLookupRole);

    const amiLookupFunction = new aws_lambda.Function(this, 'AmiLookupFunction', {
      runtime: aws_lambda.Runtime.PYTHON_3_14,
      handler: 'index.handler',
      role: amiLookupRole,
      code: aws_lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/custom-resources/ami-lookup'),
      ),
      timeout: cdk.Duration.minutes(1),
      logGroup: amiLookupLogGroup,
    });

    const amiLookupResource = new cdk.CustomResource(this, 'AmiLookupResource', {
      serviceToken: amiLookupFunction.functionArn,
      resourceType: 'Custom::AmiLookup',
      properties: {
        AmiName: 'OV-Template-aws-ubuntu-isaac_sim-20260206T111303-prod-l4r5drddssotm',
      },
    });

    const resolvedAmiId = amiLookupResource.getAttString('AmiId');

    // EC2 Instance
    this.instance = new aws_ec2.Instance(this, 'Instance', {
      instanceName: `${props.stackPrefix}-isaacsim`,
      instanceType: new aws_ec2.InstanceType(props.instanceType),
      machineImage: {
        getImage: () => ({
          imageId: resolvedAmiId,
          osType: aws_ec2.OperatingSystemType.LINUX,
          userData: aws_ec2.UserData.forLinux(),
        }),
      },
      vpc: props.vpc,
      vpcSubnets: { subnetType: aws_ec2.SubnetType.PUBLIC },
      role: this.instanceRole,
      securityGroup: this.securityGroup,
      userData: userData,
      blockDevices: [
        {
          deviceName: '/dev/sda1',
          volume: aws_ec2.BlockDeviceVolume.ebs(512, { deleteOnTermination: true }),
        },
      ],
    });

    // Ensure mount targets are ready before the instance boots
    mountTargets.forEach((mt) => this.instance.node.addDependency(mt));

    // Elastic IP
    const eip = new aws_ec2.CfnEIP(this, 'EIP', { domain: 'vpc' });
    new aws_ec2.CfnEIPAssociation(this, 'EIPAssociation', {
      allocationId: eip.attrAllocationId,
      instanceId: this.instance.instanceId,
    });
    this.elasticIp = eip.ref;

    // CloudFormation Outputs
    new CfnOutput(this, 'InstancePublicIP', {
      value: eip.ref,
      description: 'Public IP address of the DCV instance',
    });
    new CfnOutput(this, 'DCVWebURL', {
      value: `https://${eip.ref}:8443`,
      description: 'DCV web client URL',
    });
    new CfnOutput(this, 'DCVApp', {
      value: `dcv://${eip.ref}:8443`,
      description: 'DCV native app URL',
    });
    new CfnOutput(this, 'WaitForInstanceCommand', {
      value: `aws ec2 wait instance-status-ok --instance-ids ${this.instance.instanceId} --region ${region}`,
      description: 'Run this command to wait for instance status checks to pass before connecting',
    });
    new CfnOutput(this, 'SSMSessionCommand', {
      value: `aws ssm start-session --target ${this.instance.instanceId} --region ${region}`,
      description: 'Run this command to connect via Session Manager',
    });
    new CfnOutput(this, 'SetPasswordCommand', {
      value: `aws ssm send-command --instance-ids ${this.instance.instanceId} --document-name "AWS-RunShellScript" --parameters "commands=[\\"HASHED=\\$(openssl passwd -6 '\${UBUNTU_PW}') && sudo usermod --password \\\\\\"\\\$HASHED\\\\\\" ubuntu\\"]" --region ${region} --output text --query "Command.CommandId"`,
      description:
        'Run this command to set the ubuntu user password for DCV login (set UBUNTU_PW env var first)',
    });
    new CfnOutput(this, 'S3FilesBucketName', {
      value: s3FilesBucket.bucketName,
      description: 'S3 bucket backing the S3 Files file system',
    });
    new CfnOutput(this, 'S3FilesFileSystemId', {
      value: s3FilesFs.attrFileSystemId,
      description: 'S3 Files file system ID (mounted at /mnt/s3files)',
    });
  }
}
