# physai-regression

Physical AI パイプラインプラットフォームの回帰テストです。各チェックは
ライブクラスターに対して実行される pytest 関数で、プラットフォームの機構
（ライフサイクルスクリプト、Slurm 機能、DCV 状態、CLI サーフェス）が
壊れていないことを検証します。

このスイートは [uv](https://docs.astral.sh/uv/) の仮想プロジェクトです。
`physai.ssh.Session` と `physai` コンソールスクリプトを、隣接する `cli/`
パッケージから editable なパス依存として取り込むため、`uv run` は常に
（マシン上の別の editable インストールではなく）この作業ツリーの CLI を
実行します。まず環境を用意してください:

```bash
cd physai/regression
uv sync
```

## ライブクラスターに対してチェックを実行する

チェックはクラスターのログインノードに SSH で接続し、GPU パーティション
全体にまたがる Slurm アロケーションを投入するため、一時的にキューを
専有します。クラスターを他の人と共有している場合は、実行前に調整して
ください。

ランナーはライフサイクルモードを位置引数として取ります。
`python -m physai_regression --help` でモード一覧を表示できます。モードの
後ろに置いた引数は pytest へそのまま渡されます。

### モード

- **`fresh`** — `cdk destroy PhysaiClusterStack` → `cdk deploy` →
  チェック実行 → `cdk destroy`（成功時のみ。失敗時はデバッグのため残す）。
  冪等。実行時間は約 25 分。真の初回起動デプロイでのみ顕在化する回帰を
  捕捉するのに使います。
- **`upgrade-existing`** — `cdk deploy PhysaiClusterStack` +
  `infra/scripts/run-lifecycle.sh --all` → チェック実行。ユーザー管理の
  クラスターに対して、文書化された「実行中クラスターへのライフサイクル
  スクリプト変更の適用」アップグレードフローを再現します。健全な
  クラスターに対して約 13 分（スクリプトが未変更ならアップグレードは
  ほぼ no-op）。クラスターは起動したまま残ります。チェック失敗時は
  アップグレード後の状態のまま残り、自動ロールバックはありません。
- **`upgrade-from-ref`** — 完全に使い捨てのエンドツーエンド
  アップグレードテスト: `git worktree add` で `<ref>` をチェックアウト →
  worktree から `npm ci` + `cdk deploy` → 現在の HEAD から `cdk deploy` +
  `run-lifecycle.sh --all`（アップグレード）→ チェック実行 →
  `cdk destroy` + worktree クリーンアップ。実行時間は約 35 分。失敗時は
  クラスターと worktree がディスク上に残され、検査できるよう worktree
  のパスが stderr に出力されます。

### カバレッジレイヤー

チェックはマーカーで選択される 2 つのレイヤーに分かれています:

- **レイヤー 1 — プラットフォーム**（`@pytest.mark.platform`; デフォルト）。
  `regression/fixtures/fake-project/` 配下の小さな fake-project フィクスチャ
  に対して実行します。ライフサイクルスクリプト、Slurm 機能、DCV 状態、
  CLI 配線、ビルドシステム、エンドツーエンドのパイプライン連鎖を
  1 チェックあたり数秒で検証します。クラスター起動後の総実行時間は約 8 分。
- **レイヤー 2 — ビルトインサンプル**（`@pytest.mark.builtin_example`;
  `--builtin-examples` でオプトイン）。`examples/` 配下の実際の同梱
  サンプルを、小さいながらも本物の生フィクスチャに対して `--max-steps 100`
  で学習を打ち切りつつ実行します。サンプルのコンテナは毎回必ず
  リビルドします — 古い `.sqsh` が残っていると、このレイヤーが捕捉
  すべき回帰をまさに覆い隠してしまうためです。同梱パイプラインが
  convert・train・eval をエンドツーエンドで COMPLETED まで到達できることを
  検証します。現在の対象は `so101-gr00t/` の LiftCube サンプルで、
  GR00T の **両バリアント**（N1.5 と N1.6）をパラメータ化して実行します。
  パラメータ ID はバージョン文字列なので、`-k n1.5` / `-k n1.6` で
  片方だけを実行できます。実行時間は 1 バリアントあたり約 100 分、
  AWS 費用は約 $2（コンテナのリビルドと打ち切り学習の実行が大半を
  占めます）。共有コンテナ `so101-converter` と `leisaac-runtime` は
  一度だけビルドされるため、両方合わせて約 150〜180 分を見込んでください。
  `--raw-source <URI>` が必須です（下記参照）。

### `--raw-source <URI>`

レイヤー 2 は生データフィクスチャを必要とします。必須の `--raw-source`
フラグで指定してください（`--builtin-examples` と併用する場合のみ有効。
それ以外では拒否されます）。フィクスチャは実行ごとに新しく取得し直され、
チェックが成功すると削除されます（失敗時は検査のためそのまま残します）。

3 つの URI スキーム:

- **`file:///abs/path`** — スイートを実行するマシン上のローカル
  ディレクトリ。その中身がクラスターへアップロードされます。
  ```bash
  --raw-source file:///Users/me/datasets/liftcube/
  ```
- **`s3://bucket[/prefix/]`** — S3 プレフィックス。`--profile` /
  `--region` の認証情報で読み取れる必要があります。
  ```bash
  --raw-source s3://my-bucket/datasets/liftcube/
  ```
- **`hf://owner/repo[@revision]`** — 公開 HuggingFace データセット。
  任意で `@revision` を固定できます。
  ```bash
  --raw-source hf://organization/liftcube-demos
  --raw-source hf://organization/liftcube-demos@v1.2
  ```

`--builtin-examples` はモードに直交します — 3 つのどのモードとも併用でき、
そのモードが実行するレイヤー 1 の上にレイヤー 2 を追加します。

### 例

```bash
cd physai/regression

# まっさらなデプロイ + チェック + 撤去
python -m physai_regression fresh \
  --profile <aws-profile> --region <aws-region>

# 現在の HEAD のライフサイクルを実行中クラスターに適用してからチェックスイートを実行
python -m physai_regression upgrade-existing \
  --profile <aws-profile> --region <aws-region>

# 1 つのチェックだけ:
python -m physai_regression upgrade-existing -k dcvagent \
  --profile <aws-profile> --region <aws-region>

# あるいは明示的なクラスターに対して（CFN ルックアップをスキップ）:
python -m physai_regression upgrade-existing \
  --cluster physai-cluster-abc12345 \
  --profile <aws-profile> --region <aws-region>

# 使い捨てアップグレードテスト: ベースライン ref からまっさらにデプロイし、HEAD へアップグレードして撤去
python -m physai_regression upgrade-from-ref --from-ref v0.2.0 \
  --profile <aws-profile> --region <aws-region>

# 任意のモードにレイヤー 2 の同梱サンプルチェックを追加。両 GR00T バリアント
# （N1.5 + N1.6）を実行: 1 バリアントあたり約 100 分・約 $2、両方で約 150〜180 分。
# --raw-source は --builtin-examples と併用する場合は必須。URI スキームは上記セクションを参照。
python -m physai_regression upgrade-existing --builtin-examples \
  --raw-source file:///path/to/local/raw/ \
  --profile <aws-profile> --region <aws-region>

# レイヤー 2 を片方のバリアントだけ実行（-k はパラメータ ID にマッチ）:
python -m physai_regression upgrade-existing --builtin-examples -k n1.6 \
  --raw-source file:///path/to/local/raw/ \
  --profile <aws-profile> --region <aws-region>
```

ランナーは `PhysaiClusterStack` の CloudFormation 出力（または `--cluster`）
からクラスターを解決し、プライベートな一時設定ファイルを使って SSH で
接続します — あなたの `~/.ssh/config` は **変更されず**、ランナーが
作成したものは何も残りません。

## ユニットテストを実行する（AWS 不要）

`regression/tests/` 配下のユニットテストは、回帰テストコード自体の配線を
モックで検証します — クラスターとは通信しません:

```bash
cd physai/regression
uv run pytest tests/
```
