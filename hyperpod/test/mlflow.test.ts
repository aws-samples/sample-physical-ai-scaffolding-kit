import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PASKStack } from '../lib/pask-stack';
import { Configuration } from '../lib/types/configurations';

const HYPERPOD_DIR = path.resolve(__dirname, '..');
const SAMPLE_CONFIG = JSON.parse(fs.readFileSync(path.join(HYPERPOD_DIR, 'cdk.json'), 'utf-8')).context
  .config as Configuration;

const REGION = 'us-west-2';
const MLFLOW_OUTPUT_PREFIXES = ['MlflowAppArn', 'MlflowArtifactBucketName'];

/**
 * Synthesize the PASK stack with the MLflow App enabled or disabled.
 *
 * @param enabled config.Mlflow.Enabled.
 * @returns Stack template.
 */
function synth(enabled: boolean): Template {
  const config = structuredClone(SAMPLE_CONFIG);
  config.Mlflow = { Enabled: enabled };
  // Same as cdk.json: server access logs are delivered through a bucket policy.
  const app = new cdk.App({ context: { '@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy': true } });
  const stack = new PASKStack(app, config.StackName, {
    config,
    env: { account: '123456789012', region: REGION },
  });
  return Template.fromStack(stack);
}

/**
 * Return the output keys of a template that start with one of the given prefixes.
 *
 * @param template Stack template.
 * @param prefixes Output key prefixes.
 * @returns Matching output keys.
 */
function outputKeysWithPrefix(template: Template, prefixes: string[]): string[] {
  return Object.keys(template.findOutputs('*')).filter((key) => prefixes.some((prefix) => key.startsWith(prefix)));
}

process.env.CDK_DEFAULT_REGION = REGION;

describe('MLflow App disabled (sample configuration)', () => {
  const template = synth(false);

  test('the sample configuration keeps the MLflow App disabled', () => {
    expect(SAMPLE_CONFIG.Mlflow?.Enabled).toBe(false);
  });

  test('creates no MLflow App and no MLflow outputs', () => {
    template.resourceCountIs('AWS::SageMaker::MlflowApp', 0);
    expect(outputKeysWithPrefix(template, MLFLOW_OUTPUT_PREFIXES)).toEqual([]);
  });

  test('still outputs the S3 link paths', () => {
    expect(Object.keys(template.findOutputs('*'))).toEqual(expect.arrayContaining(['S3LinkUri', 'S3LinkMountPath']));
  });
});

describe('MLflow App enabled', () => {
  const template = synth(true);

  test('creates the MLflow App', () => {
    template.resourceCountIs('AWS::SageMaker::MlflowApp', 1);
  });

  test('outputs the app ARN and the artifact bucket with generated logical ID suffixes', () => {
    const keys = outputKeysWithPrefix(template, MLFLOW_OUTPUT_PREFIXES);
    expect(keys).toHaveLength(2);
    for (const prefix of MLFLOW_OUTPUT_PREFIXES) {
      expect(keys.some((key) => key.startsWith(prefix) && key !== prefix)).toBe(true);
    }
  });
});
