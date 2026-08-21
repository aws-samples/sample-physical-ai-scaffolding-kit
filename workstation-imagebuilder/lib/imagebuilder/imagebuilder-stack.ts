import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { ImageConfig } from "../types/configurations";
import { ImagePipelineConstruct } from "./image-pipeline-construct";

export interface ImageBuilderStackProps extends cdk.StackProps {
  readonly imageNames: string[];
  readonly amiParameterPrefix: string;
}

export class ImageBuilderStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ImageBuilderStackProps) {
    super(scope, id, props);

    const { imageNames, amiParameterPrefix } = props;

    for (const imageName of imageNames) {
      const configPath = path.join(__dirname, imageName, "config.json");
      if (!fs.existsSync(configPath)) {
        throw new Error(
          `Image directory "${imageName}" not found or missing config.json at ${configPath}`,
        );
      }

      const imageConfig: ImageConfig = JSON.parse(
        fs.readFileSync(configPath, "utf-8"),
      );

      new ImagePipelineConstruct(this, imageName, {
        imageName,
        imageConfig,
        amiParameterPrefix,
      });
    }
  }
}
