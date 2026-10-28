#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { ClusterStack, GpuWorkerConfig, LOGIN_GROUP_NAME } from '../lib/cluster-stack';
import { InfraStack } from '../lib/infra-stack';
import {
  JOB_INTAKE_STACK_NAME,
  JobIntakeStack,
  parseJobIntakeCallers,
} from '../lib/job-intake-stack';

const app = new cdk.App();

const clusterName = app.node.tryGetContext('clusterName') ?? 'physai-cluster';
const fsxCapacityGiB = app.node.tryGetContext('fsxCapacityGiB') ?? 1200;
const gpuWorkers: GpuWorkerConfig[] = app.node.tryGetContext('gpuWorkers') ?? [
  { name: 'gpu-workers', instanceType: 'ml.g6e.2xlarge', count: 1 },
];
const cpuWorkerType = app.node.tryGetContext('cpuWorkerType') ?? 'ml.m5.2xlarge';
const cpuWorkerCount = app.node.tryGetContext('cpuWorkerCount') ?? 1;

// Job intake is opt-in. See docs/en/JOB_INTAKE.md.
const jobIntakeCallers = parseJobIntakeCallers(app.node.tryGetContext('jobIntakeCallers'));

const infra = new InfraStack(app, 'PhysaiInfraStack', {
  clusterName,
  fsxCapacityGiB,
  terminationProtection: true,
});

const cluster = new ClusterStack(app, 'PhysaiClusterStack', {
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
  jobIntakeEnabled: jobIntakeCallers.length > 0,
});

if (cluster.jobIntakeKey) {
  new JobIntakeStack(app, JOB_INTAKE_STACK_NAME, {
    unsuffixedClusterName: clusterName,
    clusterName: cluster.clusterName,
    loginGroupName: LOGIN_GROUP_NAME,
    sshKey: cluster.jobIntakeKey,
    vpc: infra.vpc,
    clusterSg: infra.clusterSg,
    callers: jobIntakeCallers,
  });
}
