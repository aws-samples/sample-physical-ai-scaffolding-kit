import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";
import * as cdk from "aws-cdk-lib";
import {
  CfnOutput,
  aws_iam,
  aws_lambda,
  aws_logs,
  aws_s3_assets,
  aws_ssm,
  custom_resources,
} from "aws-cdk-lib";
import {
  AmazonManagedWorkflow,
  Component,
  ComponentData,
  ImagePipeline,
  ImageRecipe,
  InfrastructureConfiguration,
  DistributionConfiguration,
  BaseImage,
  Platform,
  ComponentParameterValue,
} from "@aws-cdk/aws-imagebuilder-alpha";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { ImageConfig } from "../types/configurations";

export interface ImagePipelineConstructProps {
  readonly imageName: string;
  readonly imageConfig: ImageConfig;
  readonly amiParameterPrefix: string;
}

export class ImagePipelineConstruct extends Construct {
  public readonly amiParameterName: string;

  constructor(
    scope: Construct,
    id: string,
    props: ImagePipelineConstructProps,
  ) {
    super(scope, id);

    const { imageName, imageConfig, amiParameterPrefix } = props;
    const componentDir = path.join(__dirname, imageName);
    const sharedDir = path.join(__dirname, "shared");
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    const componentFiles = fs
      .readdirSync(componentDir)
      .filter((f) => f.endsWith(".yml"))
      .sort();

    const fileAssets = new Map<string, aws_s3_assets.Asset>();
    for (const file of componentFiles) {
      const dirName = file.replace(".yml", "");
      const localDirPath = path.join(componentDir, dirName);
      const sharedDirPath = path.join(sharedDir, dirName);
      const dirPath = fs.existsSync(localDirPath)
        ? localDirPath
        : sharedDirPath;
      if (fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory()) {
        const asset = new aws_s3_assets.Asset(this, `Asset-${dirName}`, {
          path: dirPath,
        });
        fileAssets.set(file, asset);
      }
    }

    const components = componentFiles.map((file) => {
      const constructId = file.replace(/^\d+-/, "").replace(".yml", "");
      const component = new Component(this, `Component-${constructId}`, {
        platform: Platform.LINUX,
        data: ComponentData.fromInline(
          fs.readFileSync(path.join(componentDir, file), "utf-8"),
        ),
      });

      const params = imageConfig.parameters?.[file];
      const parameters: Record<string, ComponentParameterValue> = {};

      if (params) {
        for (const [key, value] of Object.entries(params)) {
          parameters[key] = ComponentParameterValue.fromString(value);
        }
      }

      const asset = fileAssets.get(file);
      if (asset) {
        parameters["FilesS3Uri"] = ComponentParameterValue.fromString(
          asset.s3ObjectUrl,
        );
      }

      if (Object.keys(parameters).length > 0) {
        return { component, parameters };
      }

      return { component };
    });

    const recipe = new ImageRecipe(this, "ImageRecipe", {
      baseImage: BaseImage.fromString(
        `arn:aws:imagebuilder:${region}:aws:image/${imageConfig.baseImage}/x.x.x`,
      ),
      components,
      blockDevices: [
        {
          deviceName: "/dev/sda1",
          volume: ec2.BlockDeviceVolume.ebs(imageConfig.diskSizeGb, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            deleteOnTermination: true,
          }),
        },
      ],
    });

    const infraConfig = new InfrastructureConfiguration(this, "InfraConfig", {
      instanceTypes: [new ec2.InstanceType(imageConfig.buildInstanceType)],
      terminateInstanceOnFailure: true,
    });

    this.amiParameterName = `${amiParameterPrefix}${imageName}`;

    const amiParameter = new aws_ssm.StringParameter(this, "AmiParameter", {
      parameterName: this.amiParameterName,
      stringValue: "NOT_BUILT_YET",
      description: `Latest AMI ID for ${imageName} workstation`,
    });

    const distConfig = new DistributionConfiguration(this, "DistConfig", {
      amiDistributions: [
        {
          region,
          amiName: `${imageName}-{{imagebuilder:buildDate}}`,
          amiDescription: `${imageName} workstation AMI`,
          amiTags: {
            Name: `${imageName}-{{imagebuilder:buildDate}}`,
          },
          ssmParameters: [
            {
              parameter: amiParameter,
              dataType: aws_ssm.ParameterDataType.TEXT,
            },
          ],
        },
      ],
    });

    const buildWorkflow = AmazonManagedWorkflow.buildImage(
      this,
      "BuildWorkflow",
    );
    const distributeWorkflow = AmazonManagedWorkflow.distributeImage(
      this,
      "DistributeWorkflow",
    );

    const pipeline = new ImagePipeline(this, "ImagePipeline", {
      recipe,
      infrastructureConfiguration: infraConfig,
      distributionConfiguration: distConfig,
      workflows: [
        { workflow: buildWorkflow },
        { workflow: distributeWorkflow },
      ],
    });

    if (pipeline.executionRole) {
      amiParameter.grantWrite(pipeline.executionRole);
    }

    if (infraConfig.role) {
      for (const asset of fileAssets.values()) {
        asset.grantRead(infraConfig.role);
      }
    }

    const triggerLogGroup = new aws_logs.LogGroup(this, "TriggerLogGroup", {
      retention: aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const triggerRole = new aws_iam.Role(this, "TriggerRole", {
      assumedBy: new aws_iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    triggerLogGroup.grantWrite(triggerRole);
    pipeline.grantStartExecution(triggerRole);

    const triggerFunction = new aws_lambda.Function(this, "TriggerFunction", {
      runtime: aws_lambda.Runtime.PYTHON_3_14,
      handler: "index.on_event",
      role: triggerRole,
      code: aws_lambda.Code.fromAsset(
        path.join(__dirname, "../lambda/trigger-pipeline"),
      ),
      timeout: cdk.Duration.minutes(1),
      logGroup: triggerLogGroup,
    });

    const triggerProvider = new custom_resources.Provider(
      this,
      "TriggerProvider",
      {
        onEventHandler: triggerFunction,
      },
    );

    new cdk.CustomResource(this, "PipelineTrigger", {
      serviceToken: triggerProvider.serviceToken,
      resourceType: "Custom::PipelineTrigger",
      properties: {
        PipelineArn: pipeline.imagePipelineArn,
        ComponentsHash: this.computeTriggerHash(componentDir, imageConfig),
      },
    });

    new CfnOutput(cdk.Stack.of(this), `${imageName}-PipelineArn`, {
      value: pipeline.imagePipelineArn,
      description: `Image Builder pipeline ARN for ${imageName}`,
    });
    new CfnOutput(cdk.Stack.of(this), `${imageName}-AmiParameterName`, {
      value: this.amiParameterName,
      description: `SSM Parameter name for ${imageName} AMI ID`,
    });
  }

  private computeTriggerHash(
    componentDir: string,
    imageConfig: ImageConfig,
  ): string {
    const hash = crypto.createHash("sha256");
    const sharedDir = path.join(__dirname, "shared");
    const entries = fs.readdirSync(componentDir, { withFileTypes: true });
    const files = entries
      .filter((e) => e.isFile() && e.name.endsWith(".yml"))
      .map((e) => e.name)
      .sort();
    for (const file of files) {
      hash.update(fs.readFileSync(path.join(componentDir, file)));
    }
    const dirs = entries
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .sort();
    for (const dir of dirs) {
      const dirPath = path.join(componentDir, dir);
      const dirFiles = fs.readdirSync(dirPath).sort();
      for (const f of dirFiles) {
        hash.update(fs.readFileSync(path.join(dirPath, f)));
      }
    }
    for (const file of files) {
      const dirName = file.replace(".yml", "");
      if (dirs.includes(dirName)) continue;
      const sharedDirPath = path.join(sharedDir, dirName);
      if (
        fs.existsSync(sharedDirPath) &&
        fs.statSync(sharedDirPath).isDirectory()
      ) {
        const dirFiles = fs.readdirSync(sharedDirPath).sort();
        for (const f of dirFiles) {
          hash.update(fs.readFileSync(path.join(sharedDirPath, f)));
        }
      }
    }
    hash.update(JSON.stringify(imageConfig));
    return hash.digest("hex").slice(0, 16);
  }
}
