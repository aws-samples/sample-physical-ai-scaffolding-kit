import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';

export interface JobIntakeCallerConfig {
  accountId: string;
  roleNamePattern?: string;
}

export type JobIntakeCaller = Required<JobIntakeCallerConfig>;

export const JOB_INTAKE_STACK_NAME = 'PhysaiJobIntakeStack';

export interface JobIntakeStackProps extends cdk.StackProps {
  unsuffixedClusterName: string;
  clusterName: string;
  loginGroupName: string;
  sshKey: ec2.KeyPair;
  vpc: ec2.IVpc;
  clusterSg: ec2.ISecurityGroup;
  callers: JobIntakeCaller[];
}

const DEFAULT_CALLER_ROLE_PATTERN = '*-pask-integration';
const SUBMIT_TIMEOUT = cdk.Duration.minutes(5);
const SUBMIT_MEMORY_MB = 512;
const LAMBDA_DIR = path.join(__dirname, '..', 'lambda');
const BUILD_SCRIPT = path.join(LAMBDA_DIR, 'build.py');
const SUBMIT_SOURCE_DIR = path.join(LAMBDA_DIR, 'submit');
const SUBMIT_BUNDLE = path.join(LAMBDA_DIR, 'build', 'submit.zip');
const LAMBDA_ARCHITECTURE = lambda.Architecture.ARM_64;
const LAMBDA_RUNTIME = lambda.Runtime.PYTHON_3_12;
const SSH_PORT = 22;

/**
 * Bundle one Lambda on the host. See docs/en/JOB_INTAKE.md.
 */
function bundle(sourceDir: string, output: string): lambda.Code {
  return lambda.Code.fromCustomCommand(output, [
    'python3',
    BUILD_SCRIPT,
    '--source',
    sourceDir,
    '--output',
    output,
    '--architecture',
    LAMBDA_ARCHITECTURE.name,
    '--python-version',
    LAMBDA_RUNTIME.name.replace('python', ''),
  ]);
}

export function parseJobIntakeCallers(raw: unknown): JobIntakeCaller[] {
  if (raw == null) return [];
  if (!Array.isArray(raw)) {
    throw new Error(
      `jobIntakeCallers must be a JSON array (got ${typeof raw}), so it has to be set in ` +
        'cdk.json: the CDK CLI passes every -c value through as a string without parsing it. ' +
        'See docs/en/JOB_INTAKE.md.',
    );
  }
  return raw.map((entry: unknown, i) => {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new Error(`jobIntakeCallers[${i}] must be an object with an accountId.`);
    }
    const { accountId, roleNamePattern = DEFAULT_CALLER_ROLE_PATTERN } =
      entry as JobIntakeCallerConfig;
    return { accountId, roleNamePattern };
  });
}

function assertValidProps(props: JobIntakeStackProps): void {
  const { callers } = props;

  if (callers.length === 0) {
    throw new Error(
      'jobIntakeCallers is empty, so there is nothing for this stack to accept jobs from. ' +
        'See docs/en/JOB_INTAKE.md.',
    );
  }

  const seen = new Set<string>();
  for (const { accountId, roleNamePattern } of callers) {
    if (!/^\d{12}$/.test(accountId)) {
      throw new Error(`jobIntakeCallers: '${accountId}' is not a 12-digit AWS account ID.`);
    }
    if (seen.has(accountId)) {
      throw new Error(`jobIntakeCallers: account ${accountId} is listed twice.`);
    }
    seen.add(accountId);
    // A pattern of only wildcards would let every role in the account enqueue.
    if (
      typeof roleNamePattern !== 'string' ||
      !/^[\w+=,.@*?-]+$/.test(roleNamePattern) ||
      /^[*?]+$/.test(roleNamePattern)
    ) {
      throw new Error(
        `jobIntakeCallers: account ${accountId} has roleNamePattern ` +
          `${JSON.stringify(roleNamePattern)}, which must be a role name that may contain ` +
          'wildcards but is not only wildcards.',
      );
    }
  }
}

export class JobIntakeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: JobIntakeStackProps) {
    super(scope, id, props);
    assertValidProps(props);

    const sshKey = props.sshKey;
    const submitFunction = this.createSubmitFunction(props, sshKey);

    this.createOutputs(submitFunction, sshKey);
  }

  private createSubmitSecurityGroup(props: JobIntakeStackProps): ec2.SecurityGroup {
    const securityGroup = new ec2.SecurityGroup(this, 'SubmitSg', {
      vpc: props.vpc,
      securityGroupName: `${props.unsuffixedClusterName}-job-intake-submit`,
      description: 'Job-intake submit Lambda',
    });

    // Declared as a standalone resource rather than via clusterSg.addIngressRule,
    // which would place the rule in PhysaiInfraStack — where it would reference
    // this stack's group id and make the two stacks cyclic.
    new ec2.CfnSecurityGroupIngress(this, 'LoginNodeSshIngress', {
      groupId: props.clusterSg.securityGroupId,
      sourceSecurityGroupId: securityGroup.securityGroupId,
      ipProtocol: 'tcp',
      fromPort: SSH_PORT,
      toPort: SSH_PORT,
      description: 'Job-intake submit Lambda to login node sshd',
    });

    return securityGroup;
  }

  private createSubmitFunction(props: JobIntakeStackProps, sshKey: ec2.KeyPair): lambda.Function {
    const submitFunction = new lambda.Function(this, 'SubmitFunction', {
      functionName: `${props.unsuffixedClusterName}-job-intake-submit`,
      runtime: LAMBDA_RUNTIME,
      handler: 'handler.lambda_handler',
      code: bundle(SUBMIT_SOURCE_DIR, SUBMIT_BUNDLE),
      architecture: LAMBDA_ARCHITECTURE,
      timeout: SUBMIT_TIMEOUT,
      memorySize: SUBMIT_MEMORY_MB,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [this.createSubmitSecurityGroup(props)],
      environment: {
        CLUSTER_NAME: props.clusterName,
        LOGIN_GROUP_NAME: props.loginGroupName,
        SSH_KEY_PARAMETER_NAME: sshKey.privateKey.parameterName,
      },
    });

    sshKey.privateKey.grantRead(submitFunction);

    // grantRead covers the SSM actions but not decryption: it only grants
    // kms:Decrypt when the parameter carries an explicit encryptionKey, and
    // KeyPair.privateKey builds its reference without one. The key is the
    // AWS-managed aws/ssm key, whose ARN is not knowable at synth, so this is
    // scoped by the ViaService condition rather than by resource.
    submitFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'DecryptSshKeyParameter',
        actions: ['kms:Decrypt'],
        resources: ['*'],
        conditions: { StringEquals: { 'kms:ViaService': `ssm.${this.region}.amazonaws.com` } },
      }),
    );

    // The login node is replaced by HyperPod on failure, taking its address
    // with it, so the address is resolved per invocation rather than configured.
    submitFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ResolveLoginNode',
        actions: ['sagemaker:ListClusterNodes', 'sagemaker:DescribeClusterNode'],
        resources: [
          cdk.Arn.format({ service: 'sagemaker', resource: 'cluster', resourceName: '*' }, this),
        ],
      }),
    );

    return submitFunction;
  }

  private createOutputs(submitFunction: lambda.IFunction, sshKey: ec2.KeyPair): void {
    new cdk.CfnOutput(this, 'SubmitFunctionName', {
      value: submitFunction.functionName,
      description: 'Probe the transport with {"probeOnly": true}',
    });

    new cdk.CfnOutput(this, 'SshKeyName', {
      value: sshKey.keyPairName,
      description: 'EC2 key pair, owned by PhysaiClusterStack, the submit Lambda logs in with',
    });

  }
}
