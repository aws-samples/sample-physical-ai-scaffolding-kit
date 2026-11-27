# VAMS から実行した run の診断と提案の出力サンプル

このディレクトリは、Claude Code の project skill `pask-training-advisor` を、VAMS から実行した学習の run に使ったときの出力の例です。
MLflow と VAMS の両方に接続できる状態（`detect` の `mode` が `full`）で作ったので、レポートに学習に使ったデータセットの節があります。
仕組みと使い方は [MLflow の学習結果からの次の学習パラメータの提案](/samples/lerobot-openpi/docs/ja/training_advisor.md) にあります。

run は、1 エピソードの LeRobot のデータセットで pi0.5 を LoRA で 30000 ステップ学習したものです。
診断のラベルは `converging`、`overfit_risk`、`gpu_memory_headroom` で、入力のデータセットが `few_episodes` の除外の候補になっています。
提案は `STEPS` を 30000 から 20000 に減らすもので、パラメータより先に入力のエピソードを増やすことを勧めています。

run ID、VAMS の実行 ID、データベース ID、アセットの名前、日時は置き換えてあります。
`snapshot.json` の metrics の履歴は 1 系列 20 点に間引いてあります。
`diagnosis.json` の特徴量と図は、間引く前の履歴から作ったものです。
`datasets.json` のメタデータは、`analyze` が使うキーだけを残してあります。

## ファイル

| ファイル | 作るもの | 内容 |
|---|---|---|
| [report.md](report.md) | Claude Code | レポート。run の要約、学習曲線の図、診断の結果、次のパラメータの提案、比較表、学習に使ったデータセット、実行する場合の手順 |
| [proposal.json](proposal.json) | Claude Code | 次のパラメータの提案。変える項目、理由、根拠、リスク、代わりの案 |
| [vams_parameters.json](vams_parameters.json) | `validate-proposal` | 検証を通った提案。VamsCLI の `--pipeline-parameters` にそのまま渡せる形で、12 項目すべてを含みます |
| [diagnosis.json](diagnosis.json) | `analyze` | 学習曲線の特徴量と診断のラベル、データセットの診断と除外の候補（`datasets`） |
| [datasets.json](datasets.json) | `fetch-vams` | run が使った VAMS のアセットのメタデータと、同じアセットを使った run の status |
| [snapshot.json](snapshot.json) | `fetch` | MLflow から取得した params、tags、metrics の履歴 |
| `*.png` | `plot` | 学習曲線の図。[loss.png](loss.png)、[lr.png](lr.png)、[grad_norm.png](grad_norm.png)、[throughput.png](throughput.png)、[gpu_mem.png](gpu_mem.png)。比較対象の run が無いため、比較の図はありません |
