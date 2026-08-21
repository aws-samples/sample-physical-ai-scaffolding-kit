import boto3

imagebuilder = boto3.client("imagebuilder")


def on_event(event, context):
    request_type = event["RequestType"]
    pipeline_arn = event["ResourceProperties"]["PipelineArn"]
    components_hash = event["ResourceProperties"].get("ComponentsHash", "")

    if request_type == "Create":
        return start_pipeline(pipeline_arn, components_hash)

    if request_type == "Update":
        old_hash = event.get("OldResourceProperties", {}).get("ComponentsHash", "")
        if components_hash != old_hash:
            return start_pipeline(pipeline_arn, components_hash)
        return {
            "PhysicalResourceId": event.get("PhysicalResourceId", "none"),
        }

    return {"PhysicalResourceId": event.get("PhysicalResourceId", "none")}


def start_pipeline(pipeline_arn, components_hash):
    response = imagebuilder.start_image_pipeline_execution(
        imagePipelineArn=pipeline_arn
    )
    image_build_arn = response["imageBuildVersionArn"]
    return {
        "PhysicalResourceId": f"{image_build_arn}#{components_hash}",
        "Data": {"ImageBuildArn": image_build_arn},
    }
