# Cleanup

When the environment is no longer needed, make sure to delete it to avoid unnecessary costs.

## Deleting All Resources

Run the following command to delete all stacks.

```bash
cdk destroy
```

If your Cloud Shell session has expired, you will need to re-upload the source code, so delete the stack from the CloudFormation console instead.

[https://console.aws.amazon.com/cloudformation/home#/stacks](https://console.aws.amazon.com/cloudformation/home#/stacks)

Select the `PASK` stack and delete it.

The following resources are not automatically deleted and must be removed manually. If you are already using SageMaker, be careful as these resources may be in use.

- Amazon CloudWatch log groups
  - /aws/lambda/PASK-*
  - /aws/sagemaker/Clusters/pask-cluster/*
- S3 buckets
  - pask-*
