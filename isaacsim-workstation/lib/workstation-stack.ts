import * as cdk from 'aws-cdk-lib/core';
import { Construct } from 'constructs';
import { Configuration } from './types/configurations';
import { Vpc } from './constructs/vpc';
import { IsaacSimWorkstation } from './constructs/isaacsim';

export interface WorkstationStackProps extends cdk.StackProps {
  config: Configuration;
}

export class WorkstationStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: WorkstationStackProps) {
    super(scope, id, props);

    const simulatorVpc = new Vpc(this, 'Simulator', {
      vpcId: props.config.VpcId,
      subnetAZ: props.config.SubnetAZ,
      stackPrefix: props.config.StackPrefix,
    });
    const workstation = new IsaacSimWorkstation(this, 'Workstation', {
      stackPrefix: props.config.StackPrefix,
      vpc: simulatorVpc.vpc,
      instanceType: props.config.InstanceType,
    });
  }
}
