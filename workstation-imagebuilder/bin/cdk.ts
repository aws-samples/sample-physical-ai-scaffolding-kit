#!/usr/bin/env node
import * as cdk from "aws-cdk-lib/core";
import { Configuration } from "../lib/types/configurations";
import { WorkstationStack } from "../lib/workstation-stack";
import { ImageBuilderStack } from "../lib/imagebuilder/imagebuilder-stack";

const app = new cdk.App();
const baseConfig = app.node.tryGetContext("config") as Configuration;

/**
 * Read the allowed CIDR ranges. A value given with `-c AllowedIpv4Cidrs=...` is a
 * comma-separated string, while the value in cdk.json is an array.
 */
function parseCidrList(value: string | string[]): string[] {
  const cidrs = Array.isArray(value) ? value : value.split(",");
  const trimmed = cidrs.map((cidr) => cidr.trim()).filter((cidr) => cidr !== "");
  if (trimmed.length === 0) {
    throw new Error("AllowedIpv4Cidrs must contain at least one CIDR range");
  }
  return trimmed;
}

const config: Configuration = {
  VpcId: app.node.tryGetContext("VpcId") ?? baseConfig.VpcId,
  DefaultInstanceType:
    app.node.tryGetContext("DefaultInstanceType") ??
    baseConfig.DefaultInstanceType,
  ImageNames: app.node.tryGetContext("ImageNames") ?? baseConfig.ImageNames,
  AllowedIpv4Cidrs: parseCidrList(
    app.node.tryGetContext("AllowedIpv4Cidrs") ?? baseConfig.AllowedIpv4Cidrs,
  ),
};

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

const amiParameterPrefix = "/workstation-imagebuilder/ami-id/";

new ImageBuilderStack(app, "ImageBuilder", {
  imageNames: config.ImageNames,
  amiParameterPrefix,
  env,
});

new WorkstationStack(app, "WorkstationEnv", {
  config,
  amiParameterPrefix,
  env,
});
