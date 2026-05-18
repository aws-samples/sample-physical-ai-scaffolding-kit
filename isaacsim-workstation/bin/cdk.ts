#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Configuration } from '../lib/types/configurations';
import { WorkstationStack } from '../lib/workstation-stack';

const app = new cdk.App();
const baseConfig = app.node.tryGetContext('config') as Configuration;

const config: Configuration = {
  StackPrefix: app.node.tryGetContext('StackPrefix') ?? baseConfig.StackPrefix,
  VpcId: app.node.tryGetContext('VpcId') ?? baseConfig.VpcId,
  SubnetAZ: app.node.tryGetContext('SubnetAZ') ?? baseConfig.SubnetAZ,
  InstanceType: app.node.tryGetContext('InstanceType') ?? baseConfig.InstanceType,
};

new WorkstationStack(app, `${config.StackPrefix}Workstation`, {
  config,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
