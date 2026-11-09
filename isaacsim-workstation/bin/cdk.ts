#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Configuration } from '../lib/types/configurations';
import { WorkstationStack } from '../lib/workstation-stack';

const app = new cdk.App();
const baseConfig = app.node.tryGetContext('config') as Configuration;

/**
 * Read the allowed CIDR ranges. A value given with `-c AllowedIpv4Cidrs=...` is a
 * comma-separated string, while the value in cdk.json is an array.
 */
function parseCidrList(value: string | string[]): string[] {
  const cidrs = Array.isArray(value) ? value : value.split(',');
  const trimmed = cidrs.map((cidr) => cidr.trim()).filter((cidr) => cidr !== '');
  if (trimmed.length === 0) {
    throw new Error('AllowedIpv4Cidrs must contain at least one CIDR range');
  }
  return trimmed;
}

const config: Configuration = {
  StackPrefix: app.node.tryGetContext('StackPrefix') ?? baseConfig.StackPrefix,
  VpcId: app.node.tryGetContext('VpcId') ?? baseConfig.VpcId,
  SubnetAZ: app.node.tryGetContext('SubnetAZ') ?? baseConfig.SubnetAZ,
  InstanceType: app.node.tryGetContext('InstanceType') ?? baseConfig.InstanceType,
  AllowedIpv4Cidrs: parseCidrList(
    app.node.tryGetContext('AllowedIpv4Cidrs') ?? baseConfig.AllowedIpv4Cidrs,
  ),
};

new WorkstationStack(app, `${config.StackPrefix}Workstation`, {
  config,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
