# 学習の診断と次のパラメータの提案の出力サンプル

このディレクトリは、Claude Code の project skill `pask-training-advisor` の出力の例です。
仕組みと使い方は [MLflow の学習結果からの次の学習パラメータの提案](/samples/lerobot-openpi/docs/ja/training_advisor.md) にあります。

このサンプルの run は VAMS を使わずに手動で実行したものなので、レポートに学習に使ったデータセットの節がありません。
VAMS から実行した run の出力の例は [VAMS から実行した run の出力サンプル](/hyperpod/docs/ja/VAMS_INTEGRATION/sample-report-vams/README.md) にあります。

## ファイル

| ファイル | 作るもの | 内容 |
|---|---|---|
| [report.md](report.md) | Claude Code | レポート。run の要約、学習曲線の図、診断の結果、次のパラメータの提案、比較表、実行する場合の手順 |
| [proposal.json](proposal.json) | Claude Code | 次のパラメータの提案。変える項目、理由、根拠、リスク、代わりの案 |
| [vams_parameters.json](vams_parameters.json) | `validate-proposal` | 検証を通った提案。VamsCLI の `--pipeline-parameters` にそのまま渡せる形で、12 項目すべてを含みます |
| [diagnosis.json](diagnosis.json) | `analyze` | 学習曲線の特徴量と診断のラベル |
| [snapshot.json](snapshot.json) | `fetch` | MLflow から取得した params、tags、metrics の履歴 |
| `*.png` | `plot` | 学習曲線の図。[loss.png](loss.png)、[lr.png](lr.png)、[grad_norm.png](grad_norm.png)、[throughput.png](throughput.png)、[gpu_mem.png](gpu_mem.png)、比較の図 [compare_loss_step.png](compare_loss_step.png) と [compare_loss_samples.png](compare_loss_samples.png) |
