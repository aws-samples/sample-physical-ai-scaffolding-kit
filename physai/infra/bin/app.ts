#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { ClusterStack, GpuWorkerConfig } from "../lib/cluster-stack";
import { InfraStack } from "../lib/infra-stack";

const app = new cdk.App();

const clusterName = app.node.tryGetContext("clusterName") ?? "physai-cluster";
const fsxCapacityGiB = app.node.tryGetContext("fsxCapacityGiB") ?? 1200;
const gpuWorkers: GpuWorkerConfig[] = app.node.tryGetContext("gpuWorkers") ?? [
  { name: "gpu-workers", instanceType: "ml.g6e.2xlarge", count: 1 },
];
const cpuWorkerType = app.node.tryGetContext("cpuWorkerType") ?? "ml.m5.2xlarge";
const cpuWorkerCount = app.node.tryGetContext("cpuWorkerCount") ?? 1;

const infra = new InfraStack(app, "PhysaiInfraStack", {
  clusterName,
  fsxCapacityGiB,
  terminationProtection: true,
});

new ClusterStack(app, "PhysaiClusterStack", {
  clusterName,
  vpc: infra.vpc,
  privateSubnet: infra.privateSubnet,
  clusterSg: infra.clusterSg,
  dataBucket: infra.dataBucket,
  fsxFileSystem: infra.fsxFileSystem,
  fsxDnsName: infra.fsxDnsName,
  fsxMountName: infra.fsxMountName,
  dbEndpoint: infra.dbEndpoint,
  dbSecret: infra.dbSecret,
  gpuWorkers,
  cpuWorkerType,
  cpuWorkerCount,
});
