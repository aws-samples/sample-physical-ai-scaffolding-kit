# 学習に使ったデータセットの読み方と除外の提案

SKILL.md の Step 4 と Step 7 で、`diagnosis.json` の `datasets` を読むときに使う。
`datasets` は `analyze` が `datasets.json` から作る。値は VAMS のアセットのメタデータをそのまま型変換したもので、ツールは計算し直していない。

## 対象の範囲

対象は、snapshot の run（対象の run と比較対象の run）が使ったアセットだけである。

| `roles` | 意味 |
|---|---|
| `input` | run の入力（タグ `vams.input.asset_id`）。データセットか curation の view |
| `view_source` | view の元のデータセット（タグ `vams.source.asset_id`）。品質とレビューはこちらにある |

データベースのほかのアセットは見ない。学習に使っていないアセットを候補にしない。

## アセットの種類（`kind`）

| `kind` | 判定 | 読むところ |
|---|---|---|
| `dataset` | `lerobot_codebase_version` がある | 規模、品質、レビュー |
| `view` | `lerobot_dataset_kind` が `view` | 規模（残したエピソードの数）だけ。品質とレビューは `view_source` のアセットを読む |
| `non_lerobot` | どちらでもない | LeRobot の学習には使えない |

## データセットの除外の候補（`datasetExclusions`）

`input` のアセットに次の規則を当てる。候補にするのは「次の学習の入力に選ばない」ことで、削除ではない。

| `reasons` | 規則 | レポートに書くこと |
|---|---|---|
| `only_failed_runs` | そのアセットを入力にした run がすべて `FAILED` / `KILLED` | 失敗の原因がデータにあるかは分からない。MLflow の run とポーラーのログで原因を確かめてから外すかを決める |
| `not_lerobot` | `kind` が `non_lerobot` | LeRobot のデータセットではないため、学習の入力にしない |
| `stats_missing` | `lerobot_stats_present` が false | 正規化の統計（`meta/stats.json`）が無い |
| `stats_count_mismatch` | `lerobot_stats_count_matches_frames` が false | 統計のフレーム数が実際のフレーム数と合わない。統計を作り直すまで入力にしない |
| `few_episodes` | `lerobot_total_episodes` が閾値（`thresholds.fewEpisodesMax`）以下 | エピソードが少なく、課題のばらつきを学べない。外すより、エピソードを足したデータセットか view に替えることを提案する |

`only_failed_runs` 以外の規則は、`runsUsingAssets` の status に関係なく当てる。成功した run があっても、`few_episodes` などは次の学習で改善できる点として書く。

## エピソードの除外の候補（`episodeExclusions`）

VAMS のレビュー（`lerobot_review_episodes`）で外す判定のエピソードのうち、その run がまだ学習に使ったものが候補になる。

| `reasons` | 規則 |
|---|---|
| `review_exclude` | レビューの `exclude` が true |
| `review_failure` | レビューの `verdict` が `failure` |

入力が view の場合は、view が残したエピソード（`curation_manifest.json` の `episodes`）にあるものだけを候補にする。view が既に外したエピソードは `alreadyExcludedEpisodes` にあり、候補にしない。
入力がデータセットの全体の場合は、レビューで外す判定のエピソードがすべて候補になる。

## 品質のフラグ

品質のフラグは、エピソードごとの値が VAMS の品質ファイル（`meta/vams_episode_quality.parquet`）にあり、メタデータには件数（`flagCounts`、`features.qualityFlaggedCount`）しか無い。
そのため、フラグだけでエピソードの除外の候補は作らない。`findings` の文を使い、次の順に書く。

1. フラグが立ったエピソードの数とフラグの内訳（`flagCounts`）
2. レビューがまだ無い（`features.reviewedEpisodeCount` が 0）場合は、VAMS の画面でフラグの立ったエピソードをレビューし、外すものに `exclude` を付けること
3. レビューのあと、view を作り直して次の学習の入力にすること

フラグはトリアージのためのもので、成功や失敗を表さない。フラグが立ったことだけを理由に外すと書かない。

## VAMS のキュレーションで除外する手順の書き方

レポートには、人が行う手順として次を書く。この skill は実行しない。

1. エピソードの除外: VAMS の LeRobot Dataset Curation で、元のデータセット（`assetId`）から候補のエピソード（`episodeIndex`）を外した view を作る。既に view を使っている場合は、その view の残すエピソードから候補を外して作り直す。
2. データセットの除外: 次の学習の `--input-file` に、候補のアセットを選ばない。`few_episodes` の場合は、エピソードを足したデータセットか、複数のエピソードを持つ view を選ぶ。
3. 新しい view で学習した run を、この skill でもう一度診断し、損失の変化を比べる。

view の作り方と、view を学習の入力にする方法は `hyperpod/docs/ja/VAMS_INTEGRATION/OVERVIEW.md` にある。

## 書かないこと

1. 「削除する」「archive する」と書かない。除外は次の学習の入力から外すことである。
2. `datasets` に無い数値（エピソードごとのフラグの値、除外後のフレーム数など）を作らない。
3. AWS のアカウント ID を含む値（バケット名、ARN）を書かない。アセットは名前と ID で示す。
