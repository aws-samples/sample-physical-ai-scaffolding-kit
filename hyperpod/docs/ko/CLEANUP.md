# Cleanup

Environment가 더 이상 필요하지 않으면 불필요한 비용을 피하기 위해 반드시 삭제하세요.

## 모든 Resource 삭제

다음 command로 모든 stack을 삭제합니다.

```bash
cdk destroy
```

Cloud Shell session이 만료되었다면 source code를 다시 upload해야 하므로, 대신 CloudFormation console에서 stack을 삭제하세요.

[https://console.aws.amazon.com/cloudformation/home#/stacks](https://console.aws.amazon.com/cloudformation/home#/stacks)

`PASK` stack을 선택하고 삭제합니다.

다음 resource는 자동으로 삭제되지 않으므로 수동으로 삭제해야 합니다. 이미 SageMaker를 사용 중이라면 해당 resource가 사용 중일 수 있으니 주의하세요.

- Amazon CloudWatch log groups
  - /aws/lambda/PASK-*
  - /aws/sagemaker/Clusters/pask-cluster/*
- S3 buckets
  - pask-*
