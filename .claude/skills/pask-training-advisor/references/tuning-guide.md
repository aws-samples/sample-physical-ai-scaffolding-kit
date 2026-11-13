# 診断のラベルと 12 項目の調整の対応

`diagnosis.json` の診断のラベルから、次の学習で 12 項目のどれをどう動かすかを決めるためのガイド。
ここに書く規則は推奨である。Claude Code は規則に従う場合も従わない場合も、判断の理由を `changes[].reason` に、根拠（ラベル名と `diagnosis.json` の特徴量の `キー=値`）を `changes[].evidence` に書く。

値の範囲、変える項目の数の上限（`maxChangedKeys`）、1 項目の変化の倍率の上限（`maxChangeFactor`）、前の run と同じに固定する項目（`fixedKeys`）は `scripts/config/proposal_constraints.json` が決める。
このガイドの規則よりも、制約の検証（`validate-proposal`）の結果が優先する。

## 対象の 12 項目

| 項目 | 型 | 調整の手段としての意味 |
|---|---|---|
| `POLICY_TYPE` | enum（`pi05` / `pi0`） | `proposal_constraints.json` の `fixedKeys` で固定 |
| `PRETRAINED_PATH` | string | `fixedKeys` で固定。`pi05` には `lerobot/pi05_base`、`pi0` には `lerobot/pi0_base` |
| `FINETUNE_MODE` | enum（`lora` / `expert_only` / `full`） | `expert_only` と `full` は 24 GB の GPU に収まらない可能性が高い（`validate-proposal` が警告を出す） |
| `LORA_R`、`LORA_ALPHA` | integer | `FINETUNE_MODE` が `lora` のときだけ使われる。表現力の調整 |
| `STEPS` | integer | 学習の長さ。学習率のスケジュールにも影響する（下の節） |
| `BATCH_SIZE` | integer | 勾配のノイズと GPU メモリの使用量 |
| `SAVE_FREQ` | integer | チェックポイントの間隔。学習の結果には影響しない |
| `LOG_FREQ` | integer | MLflow の metrics の記録間隔。診断の点数に影響する |
| `NUM_WORKERS` | integer | データローダのプロセス数。上限は `slurm_train.sh` の `--cpus-per-task`（8） |
| `COMPILE_MODEL` | boolean | `torch.compile` の有無。速度とメモリの使用量 |
| `IMAGE_TAG` | string | `fixedKeys` で固定 |

## ラベルごとの対応

複数のラベルが付いた場合は、上の行ほど優先する。
`insufficient_data` や `failed_or_killed` があるときは、曲線の形のラベル（`converging` など）で判断しない。

| ラベル | 推奨する調整 | 判断の注意 |
|---|---|---|
| `insufficient_data` | 変更を最小にする（変えないことも選べる）。`LOG_FREQ` が `STEPS` に対して大きく記録点数が少ない場合は、`LOG_FREQ` を下げることを検討する | 判定に足りる点が無い。原因（記録の間隔、途中終了、記録の失敗）の確認を `risks` に書くことを優先する。`num_points`、`last_step` を根拠にする。`STEPS=20` のような動作確認用の短い run では、`maxChangeFactor`（4 倍）の範囲で `STEPS` を延ばし `LOG_FREQ` を下げても、本番の長さには届かない。本番の学習はテンプレートの既定値から始めることを検討するよう `risks` に書く |
| `failed_or_killed` | 変更を最小にする。`gpu_memory_tight` も付いていれば `BATCH_SIZE` を減らす | run の status か `completion_ratio` が理由。メモリ不足、ノードの障害、手動の中断などの原因の確認を `risks` に書くことを優先する。途中までの曲線で `STEPS` を決めない |
| `diverging` | `BATCH_SIZE` を増やす | 本来は学習率を下げる症状だが、学習率は許可リスト外。`parameters` には入れず、`risks` とレポートの「許可リスト外の所見」に「学習率（preset の `optimizer_lr`）を下げることを検討」と書く。`nonfinite_loss`、`nonfinite_grad_norm`、`tail_slope_per_1k` を根拠にする |
| `unstable` | `BATCH_SIZE` を増やす | 勾配のノイズを減らす。GPU メモリの余地（`gpu_memory_headroom`、`gpu_memory_percent_max`）を確かめる。`loss_spikes`、`grad_norm_spikes`、`tail_cv` を根拠にする |
| `overfit_risk` | `STEPS` を減らす | `eval/*` が無い run では `epochs` による代理の判定である。直接は判定していないことをレポートに書く。`epochs` を根拠にする |
| `converging` | `STEPS` を増やす | 下の「学習率のスケジュールと STEPS」を必ず確かめる。`tail_slope_per_1k`、`tail_improvement`、`lr_progress` を根拠にする |
| `plateau` | `lora` なら `LORA_R` を上げ、`LORA_ALPHA` を合わせて上げる | `lr_progress` が小さい（学習率が減衰しきっている）なら、`STEPS` を増やしても効果は小さいと指摘する。`STEPS` を増やす代わりに表現力（`LORA_R`）を上げる。`FINETUNE_MODE` を `expert_only` / `full` に変える案は GPU メモリの制約があるため、`alternatives` に置く |
| `dataloader_bound` | `NUM_WORKERS` を増やす（上限 8） | 既に 8 なら増やせない。`COMPILE_MODEL` を有効にしても読み込みの律速は解消しない。`dataloading_ratio`、`samples_per_s_median`、`gpu_utilization_median` を根拠にする |
| `gpu_memory_headroom` | `BATCH_SIZE` を増やせる | 単独では変更の理由にしない。`unstable`、`diverging` など `BATCH_SIZE` を増やす理由と組み合わせて使う。`gpu_memory_percent_max`、`gpu_mem_gb_max` を根拠にする |
| `gpu_memory_tight` | `BATCH_SIZE` を減らす。または `COMPILE_MODEL` を有効にする | `BATCH_SIZE` を増やす提案と同時に出さない。`gpu_memory_percent_max` を根拠にする |

### BATCH_SIZE と STEPS を一緒に考える

`BATCH_SIZE` を変えると、同じ `STEPS` で処理するサンプル数（`final_samples`）とデータの周回数（`epochs`）が変わる。
`BATCH_SIZE` を増やすとき、処理するサンプル数を保ちたい場合は `STEPS` を減らすことを検討する。
ただし `maxChangedKeys` と `maxChangeFactor` の範囲に収める。

`estimated_frames`（データセットのフレーム数の推定）と `estimated_episodes` は、`STEPS × BATCH_SIZE` がデータセットを何周するかの見積もりに使える。
レポートに書く数値は `diagnosis.json` の値だけにし、見積もりの計算結果を数値として書かない。

## 項目間の関係

`validate-proposal` が検証する。提案を書く段階で守る。

1. `SAVE_FREQ <= STEPS`。`STEPS` を減らしたら `SAVE_FREQ` も確かめる。
2. `LOG_FREQ <= SAVE_FREQ`。
3. `POLICY_TYPE` と `PRETRAINED_PATH` の組み合わせ（`pi05` と `lerobot/pi05_base`、`pi0` と `lerobot/pi0_base`）。
4. `NUM_WORKERS` は 1 以上 8 以下。
5. `LORA_R` は `proposal_constraints.json` の `choices` のどれか。`LORA_ALPHA` は範囲の中。

## 学習率のスケジュールと STEPS（LeRobot 0.6.1）

学習率は `slurm_train.sh` から渡しておらず、LeRobot の policy の preset が使われる（`TrainPipelineConfig.use_policy_training_preset` の既定値が `True`）。
次の内容は、LeRobot 0.6.1 の `src/lerobot/policies/pi05/configuration_pi05.py`、`src/lerobot/policies/pi0/configuration_pi0.py`、`src/lerobot/optim/schedulers.py`、`src/lerobot/optim/factory.py`、`src/lerobot/configs/train.py` を読んで確かめた。

### preset の値

pi0 と pi0.5 で同じ値である。

| 設定（policy の config） | 値 | 使われ方 |
|---|---|---|
| `optimizer_lr` | `2.5e-5` | AdamW の学習率。スケジュールの最大値（`peak_lr`） |
| `optimizer_betas` | `(0.9, 0.95)` | AdamW |
| `optimizer_eps` | `1e-8` | AdamW |
| `optimizer_weight_decay` | `0.01` | AdamW |
| `optimizer_grad_clip_norm` | `1.0` | 勾配のクリップ |
| `scheduler_warmup_steps` | `1000` | warmup のステップ数 |
| `scheduler_decay_steps` | `30000` | cosine 減衰のステップ数 |
| `scheduler_decay_lr` | `2.5e-6` | 減衰したあとの学習率（`decay_lr`） |

`get_optimizer_preset` は `AdamWConfig`、`get_scheduler_preset` は `CosineDecayWithWarmupSchedulerConfig`（type `cosine_decay_with_warmup`）を返す。

### STEPS との関係

スケジューラは `cfg.scheduler.build(optimizer, cfg.steps)` で作られ、`STEPS` によって次のように変わる。

| `STEPS` | warmup のステップ数 | 減衰のステップ数 | 学習率の推移 |
|---|---|---|---|
| `STEPS < 30000` | `int(1000 × STEPS / 30000)` に縮む | `STEPS` に縮む | 学習の最後に `decay_lr` に達する。スケジュール全体が `STEPS` に合わせて伸び縮みする |
| `STEPS >= 30000` | `1000`（固定） | `30000`（固定） | 30000 ステップで `decay_lr` に達し、それ以降は `decay_lr` のまま一定 |

判断への影響:

1. `STEPS` が 30000 未満の run で `STEPS` を増やすと、warmup と減衰が引き延ばされ、同じステップでの学習率が高くなる。曲線の形が前の run と変わるので、同じステップの損失を単純に比べられない。
2. `STEPS` が 30000 以上の run では、30000 ステップより後は学習率が最小値（`2.5e-6`）のまま学習する。`plateau` の run で `STEPS` を 30000 より大きくしても、効果は小さい可能性が高い。
3. `diagnosis.json` の `lr_progress`（最終の `train/lr` / 最大の `train/lr`）が `decay_lr / optimizer_lr`（preset では 0.1）に近ければ、減衰しきっている。値の比較は `snapshot.json` の `lrScheduleParams` と `diagnosis.json` の値で行い、レポートには JSON の値をそのまま書く。
4. 学習率、warmup、減衰のステップ数は許可リスト外である。変えるべきと判断したら、`parameters` に入れず、`risks` と「許可リスト外の所見」に書く。

### 実際の run の値で判断する

上の preset は LeRobot 0.6.1 の既定値である。実際の run の値は `snapshot.json` の `target.lrScheduleParams`（params の `optimizer.*`、`scheduler.*`、`policy.optimizer_*`、`policy.scheduler_*`）で確かめ、preset と違えばそちらで判断する。
`lrScheduleParams` が空の場合は、preset の値を前提にしたことをレポートに書く。

### params に記録される学習率の値

pi0.5 の LoRA の run では、学習率に関する値は次のように記録される。

1. params に記録されるキーは `optimizer.type`、`optimizer.lr`、`optimizer.betas`、`optimizer.eps`、`optimizer.weight_decay`、`optimizer.grad_clip_norm`、`scheduler.type`、`scheduler.num_warmup_steps`、`scheduler.num_decay_steps`、`scheduler.peak_lr`、`scheduler.decay_lr`、`policy.optimizer_lr`、`policy.optimizer_betas`、`policy.optimizer_eps`、`policy.optimizer_weight_decay`、`policy.optimizer_grad_clip_norm`、`policy.scheduler_warmup_steps`、`policy.scheduler_decay_steps`、`policy.scheduler_decay_lr` である。
2. `scheduler.num_warmup_steps` と `scheduler.num_decay_steps` は、`STEPS` によらず config の値（`1000` と `30000`）のまま記録される。`STEPS < 30000` で縮んだ実際の warmup と減衰のステップ数は params に現れないので、上の表と `lr.png` で判断する。
3. `FINETUNE_MODE` が `lora` の run の `train/lr` の最大値は preset の `optimizer_lr`（`2.5e-5`）にほぼ等しい。LoRA でも preset の学習率が使われる。

### 再開した run の学習率

再開した run（`RESUME=true`）では、preset を適用し直さず（`train.py` の `use_policy_training_preset and not self.resume`）、スケジューラの状態をチェックポイントから読む（`load_training_state`）。再開時に `STEPS` を変えた run では、warmup と減衰のステップ数を `train/lr` の曲線（`lr.png`）で確かめる。
