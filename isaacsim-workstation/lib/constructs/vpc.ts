import * as cdk from 'aws-cdk-lib';
import { aws_ec2, aws_logs, aws_iam, aws_lambda, aws_ssm } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Configuration } from '../types/configurations';
import * as path from 'path';

export interface VpcProps {
  stackPrefix: string;
  vpcId?: string;
  subnetAZ: string;
}

export class Vpc extends Construct {
  public readonly vpc: aws_ec2.IVpc;
  public readonly publicSubnet: aws_ec2.PublicSubnet;
  public readonly privateSubnet: aws_ec2.PrivateSubnet;
  public readonly securityGroup: aws_ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: VpcProps) {
    super(scope, id);

    let selectedAZ = props.subnetAZ;
    const ssmParameterName = `/${props.stackPrefix}/Simulator/vpc-az`;

    new cdk.CfnOutput(this, 'AZNameParameterStore', {
      value: ssmParameterName,
      description: 'Parameter store name for selected AZ',
    });

    if (props.vpcId) {
      const existingVpc = aws_ec2.Vpc.fromLookup(this, 'ExistingVpc', {
        vpcId: props.vpcId,
      });
      this.vpc = existingVpc;

      // If VPC is provided, use its first public subnet's AZ
      selectedAZ = this.vpc.publicSubnets[0].availabilityZone;
      new cdk.CfnOutput(this, 'UsingProvidedVpc', {
        value: props.vpcId,
        description: 'Using provided VPC ID',
      });
    } else {
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
                actions: ['ec2:DescribeSubnets', 'ec2:DescribeInstanceTypeOfferings'],
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
        description: `VPC AZ for Simulation environoment ${props.stackPrefix}`,
      });

      new cdk.CfnOutput(this, 'ClusterAvailabilityZone', {
        value: selectedAZ,
        description: 'Cluster Availability Zone',
      });

      this.vpc = new aws_ec2.Vpc(this, 'VPC', {
        ipAddresses: aws_ec2.IpAddresses.cidr('10.0.0.0/16'),
        natGateways: 0,
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

      this.vpc.addGatewayEndpoint('S3Endpoint', {
        service: aws_ec2.GatewayVpcEndpointAwsService.S3,
      });
    }
  }
}
