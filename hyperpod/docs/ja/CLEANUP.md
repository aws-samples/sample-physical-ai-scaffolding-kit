# 後片付け

環境が不要になった場合、忘れずに削除を行うことで、余計なコストが掛からなくなります。

## リソース全体の削除

次に以下のコマンドを実行し、すべてのスタックを削除します。

```bash
cdk destroy
```

Cloud Shell のセッションが終了していた場合は、再度ソースをアップロードする必要が出てくるため、CloudFormationのコンソールから削除を行います。

[https://console.aws.amazon.com/cloudformation/home#/stacks](https://console.aws.amazon.com/cloudformation/home#/stacks)

`PASK` のスタックを選択し、削除してください。

以下のリソースは、削除されずに残っていますので、手動で削除を行なってください。すでにSageMakerを利用されている場合は、利用している可能性があるため削除は注意してください。

- Amazon CloudWatch のロググループ
  - /aws/lambda/PASK-*
  - /aws/sagemaker/Clusters/pask-cluster/*
- S3バケット
  - pask-*
