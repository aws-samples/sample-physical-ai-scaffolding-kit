import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as fsx from "aws-cdk-lib/aws-fsx";
import * as rds from "aws-cdk-lib/aws-rds";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface InfraStackProps extends cdk.StackProps {
  clusterName: string;
  fsxCapacityGiB: number;
}

export class InfraStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly privateSubnet: ec2.ISubnet;
  public readonly clusterSg: ec2.SecurityGroup;
  public readonly dataBucket: s3.Bucket;
  public readonly fsxFileSystem: fsx.CfnFileSystem;
  public readonly fsxDnsName: string;
  public readonly fsxMountName: string;
  public readonly dbEndpoint: string;
  public readonly dbSecret: secretsmanager.ISecret;

  constructor(scope: Construct, id: string, props: InfraStackProps) {
    super(scope, id, props);

    const { clusterName, fsxCapacityGiB } = props;
    const account = cdk.Stack.of(this).account;

    // ── VPC ──

    this.vpc = new ec2.Vpc(this, "Vpc", {
      vpcName: `${clusterName}-vpc`,
      ipAddresses: ec2.IpAddresses.cidr("10.0.0.0/16"),
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: "Public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: "Private", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    this.privateSubnet = this.vpc.privateSubnets[0];

    this.clusterSg = new ec2.SecurityGroup(this, "ClusterSg", {
      vpc: this.vpc,
      securityGroupName: `${clusterName}-sg`,
      description: "PhysAI cluster + FSx",
    });
    // Self-referencing ingress as a standalone resource so we can depend on
    // it explicitly from FSx. Without this dependency, CloudFormation may
    // create the FSx file system in parallel with the ingress rule — FSx's
    // port-988 validation runs before the rule is in place and fails with
    // "InvalidNetworkSettings".
    const clusterSgIngress = new ec2.CfnSecurityGroupIngress(this, "ClusterSgSelfIngress", {
      groupId: this.clusterSg.securityGroupId,
      sourceSecurityGroupId: this.clusterSg.securityGroupId,
      ipProtocol: "-1",
      description: "Self-referencing (all traffic within SG)",
    });

    // ── S3 Data Bucket ──

    this.dataBucket = new s3.Bucket(this, "DataBucket", {
      bucketName: `${clusterName}-data-${account}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // ── FSx for Lustre ──

    this.fsxFileSystem = new fsx.CfnFileSystem(this, "FsxLustre", {
      fileSystemType: "LUSTRE",
      storageCapacity: fsxCapacityGiB,
      storageType: "SSD",
      subnetIds: [this.privateSubnet.subnetId],
      securityGroupIds: [this.clusterSg.securityGroupId],
      lustreConfiguration: {
        deploymentType: "PERSISTENT_2",
        perUnitStorageThroughput: 125,
      },
      tags: [{ key: "Name", value: `${clusterName}-fsx` }],
    });
    this.fsxFileSystem.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    this.fsxFileSystem.addDependency(clusterSgIngress);

    this.fsxDnsName = `${this.fsxFileSystem.ref}.fsx.${this.region}.amazonaws.com`;
    this.fsxMountName = this.fsxFileSystem.getAtt("LustreMountName").toString();

    // DRA: auto-import from S3 raw/
    new fsx.CfnDataRepositoryAssociation(this, "FsxDra", {
      fileSystemId: this.fsxFileSystem.ref,
      fileSystemPath: "/raw",
      dataRepositoryPath: `s3://${this.dataBucket.bucketName}/raw`,
      s3: {
        autoImportPolicy: { events: ["NEW", "CHANGED", "DELETED"] },
      },
    });

    // ── RDS for Slurm Accounting ──

    const dbSg = new ec2.SecurityGroup(this, "DbSg", {
      vpc: this.vpc,
      description: "Slurm accounting DB",
    });
    dbSg.addIngressRule(this.clusterSg, ec2.Port.tcp(3306), "From HyperPod cluster");

    this.dbSecret = new secretsmanager.Secret(this, "DbSecret", {
      secretName: `${clusterName}/slurm-db`,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: "slurm" }),
        generateStringKey: "password",
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    const dbInstance = new rds.DatabaseInstance(this, "SlurmDb", {
      engine: rds.DatabaseInstanceEngine.mariaDb({
        version: rds.MariaDbEngineVersion.VER_10_11,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.SMALL),
      vpc: this.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [dbSg],
      databaseName: "slurm_acct_db",
      credentials: rds.Credentials.fromSecret(this.dbSecret),
      multiAz: false,
      allocatedStorage: 20,
      storageType: rds.StorageType.GP3,
      publiclyAccessible: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.dbEndpoint = dbInstance.dbInstanceEndpointAddress;

    new cdk.CfnOutput(this, "DataBucketName", {
      value: this.dataBucket.bucketName,
      description: "S3 bucket for raw data (upload to s3://<bucket>/raw/ to auto-import to /fsx/raw/)",
      exportName: `${this.stackName}-DataBucketName`,
    });
  }
}
