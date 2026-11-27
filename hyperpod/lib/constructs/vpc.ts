import * as cdk from 'aws-cdk-lib';
import { aws_ec2, aws_logs, aws_iam, aws_lambda, aws_ssm } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Configuration } from '../types/configurations';
import * as path from 'path';

export interface VpcOneAzProps {
  config: Configuration;
}

export class VpcOneAz extends Construct {
  public readonly vpc: aws_ec2.Vpc;
  public readonly publicSubnet: aws_ec2.PublicSubnet;
  public readonly privateSubnet: aws_ec2.PrivateSubnet;
  public readonly securityGroup: aws_ec2.SecurityGroup;
  public readonly lsfSecurityGroup: aws_ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: VpcOneAzProps) {
    super(scope, id);

    let selectedAZ = props.config.ClusterVPC.SubnetAZ;
    const ssmParameterName = `/${props.config.StackName}/${props.config.Cluster.Name}/vpc-az`;

    new cdk.CfnOutput(this, 'AZNameParameterStore', {
      value: ssmParameterName,
      description: 'Parameter store name for selected AZ',
    });

    // Custom resource to select suitable subnet based on instance type availability
    const subnetSelectorLogGroup = new aws_logs.LogGroup(this, 'SubnetSelectorLogGroup', {
      retention: aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const subnetSelectorFunctionRole = new aws_iam.Role(this, 'SubnetSelectorFunctionRole', {
      assumedBy: new aws_iam.ServicePrincipal('lambda.amazonaws.com'),
      inlinePolicies: {
        EC2Describe: new aws_iam.PolicyDocument({
          statements: [
            new aws_iam.PolicyStatement({
              actions: ['ec2:DescribeInstanceTypeOfferings'],
              resources: ['*'],
            }),
          ],
        }),
      },
    });
    subnetSelectorLogGroup.grantWrite(subnetSelectorFunctionRole);

    const subnetSelectorFunction = new aws_lambda.Function(this, 'SubnetSelectorFunction', {
      runtime: aws_lambda.Runtime.PYTHON_3_14,
      handler: 'index.handler',
      role: subnetSelectorFunctionRole,
      code: aws_lambda.Code.fromAsset(
        path.join(__dirname, '../lambda/custom-resources/subnet-selector'),
      ),
      timeout: cdk.Duration.minutes(1),
      logGroup: subnetSelectorLogGroup,
    });

    const subnetSelectorResource = new cdk.CustomResource(this, 'SubnetSelectorResource', {
      serviceToken: subnetSelectorFunction.functionArn,
      resourceType: 'Custom::SubnetSelector',
      properties: {
        selectedAZ: selectedAZ,
      },
    });

    selectedAZ = subnetSelectorResource.getAttString('AvailabilityZone');
    // Save selected AZ to SSM Parameter Store for future use
    new aws_ssm.StringParameter(this, 'VpcAzParameter', {
      parameterName: ssmParameterName,
      stringValue: selectedAZ,
      description: `VPC AZ for HyperPod cluster ${props.config.Cluster.Name}`,
    });

    new cdk.CfnOutput(this, 'ClusterAvailabilityZone', {
      value: selectedAZ,
      description: 'Cluster Availability Zone',
    });

    this.vpc = new aws_ec2.Vpc(this, 'VPC', {
      ipAddresses: aws_ec2.IpAddresses.cidr('10.0.0.0/16'),
      natGateways: 1,
      availabilityZones: [selectedAZ],
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: aws_ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'Private',
          subnetType: aws_ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 17,
        },
      ],
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });
    this.vpc.node.addDependency(subnetSelectorResource);
    this.publicSubnet = this.vpc.publicSubnets[0] as aws_ec2.PublicSubnet;
    this.privateSubnet = this.vpc.privateSubnets[0] as aws_ec2.PrivateSubnet;

    this.lsfSecurityGroup = new aws_ec2.SecurityGroup(this, 'lsfSecurityGroup', {
      vpc: this.vpc,
      description: 'Allow FSX to mount to the head node',
      allowAllOutbound: true,
    });
    this.lsfSecurityGroup.connections.allowFrom(this.lsfSecurityGroup, aws_ec2.Port.allTraffic());

    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: aws_ec2.GatewayVpcEndpointAwsService.S3,
    });

    if (props.config.ClusterVPC.UseFlowLog) {
      const flowLogGroup = new aws_logs.LogGroup(this, 'FlowLogGroup', {
        retention: aws_logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      const flowLogRole = new aws_iam.Role(this, 'FlowLogRole', {
        assumedBy: new aws_iam.ServicePrincipal('vpc-flow-logs.amazonaws.com'),
        inlinePolicies: {
          flowlogs: new aws_iam.PolicyDocument({
            statements: [
              new aws_iam.PolicyStatement({
                actions: [
                  'logs:CreateLogStream',
                  'logs:PutLogEvents',
                  'logs:DescribeLogGroups',
                  'logs:DescribeLogStreams',
                ],
                resources: [flowLogGroup.logGroupArn],
              }),
            ],
          }),
        },
      });

      new aws_ec2.FlowLog(this, 'FlowLog', {
        resourceType: aws_ec2.FlowLogResourceType.fromVpc(this.vpc),
        destination: aws_ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup, flowLogRole),
      });
    }
  }
}
