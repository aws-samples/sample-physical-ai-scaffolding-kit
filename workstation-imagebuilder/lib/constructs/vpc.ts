import * as cdk from "aws-cdk-lib";
import { aws_ec2 } from "aws-cdk-lib";
import { Construct } from "constructs";

export interface VpcProps {
  vpcId?: string;
}

export class Vpc extends Construct {
  public readonly vpc: aws_ec2.IVpc;

  constructor(scope: Construct, id: string, props: VpcProps) {
    super(scope, id);

    if (props.vpcId) {
      this.vpc = aws_ec2.Vpc.fromLookup(this, "ExistingVpc", {
        vpcId: props.vpcId,
      });

      new cdk.CfnOutput(this, "UsingProvidedVpc", {
        value: props.vpcId,
        description: "Using provided VPC ID",
      });
    } else {
      this.vpc = new aws_ec2.Vpc(this, "VPC", {
        ipAddresses: aws_ec2.IpAddresses.cidr("10.0.0.0/16"),
        natGateways: 0,
        maxAzs: 99,
        subnetConfiguration: [
          {
            name: "Public",
            subnetType: aws_ec2.SubnetType.PUBLIC,
            cidrMask: 24,
          },
        ],
        enableDnsHostnames: true,
        enableDnsSupport: true,
      });

      this.vpc.addGatewayEndpoint("S3Endpoint", {
        service: aws_ec2.GatewayVpcEndpointAwsService.S3,
      });
    }
  }
}
