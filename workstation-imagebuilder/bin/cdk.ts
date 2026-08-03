#!/usr/bin/env node
import * as cdk from "aws-cdk-lib/core";
import { Configuration } from "../lib/types/configurations";
import { WorkstationStack } from "../lib/workstation-stack";
import { ImageBuilderStack } from "../lib/imagebuilder/imagebuilder-stack";

const app = new cdk.App();
const baseConfig = app.node.tryGetContext("config") as Configuration;

const config: Configuration = {
  VpcId: app.node.tryGetContext("VpcId") ?? baseConfig.VpcId,
  DefaultInstanceType:
    app.node.tryGetContext("DefaultInstanceType") ??
    baseConfig.DefaultInstanceType,
  ImageNames: app.node.tryGetContext("ImageNames") ?? baseConfig.ImageNames,
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
