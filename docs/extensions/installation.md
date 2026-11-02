# 導入と共通設定

[共通運用](README.md)

このページは、Linuxの1台のDockerホストにCVATと追加機能を新規導入する管理者向けです。設定と起動は、共通管理コマンド **cvatctl** で行います。「cvatctl」は設定ファイル `.env` を読み、選択した機能だけを組み合わせます。既存環境の変更は[更新手順](update.md)から開始してください。

## ホストと保存先を準備する

共通の前提は、Python 3.10以上、Git、Docker Engine、Docker Compose 2.24.4以上、およびこのリポジトリの完全なチェックアウトです。複数のサービスをまとめて起動するDocker Composeには `!override` 構文が必要なため、旧形式の `docker-compose` は使用しません。ここでのPython要件は「cvatctl」の実行用で、別途CVAT SDKやCLIを利用する場合はPython 3.11以上が必要です。

モデルによる画像分割と追跡を使う場合は、追加の準備があります。SAM2、SAM3.1、UltraSAMにはNVIDIA GPU、対応ドライバー、NVIDIA Container Toolkit、および推論サービスNuclioの管理コマンド `nuctl` が必要です。`nuctl` は [NuclioのCompose定義](../../components/serverless/docker-compose.serverless.yml) のdashboardと同じ版を用意します。モデル用のPythonとCUDA依存関係は、CVAT本体とは別のコンテナへ導入します。

検証環境と本番環境は別ホストまたは別Dockerデーモンに分けます。標準構成には固定のコンテナ名と公開ポートがあるため、環境名だけの変更では分離できません。復元先のメール、Webhook、共有フォルダー、外部DBも本番から隔離します。

## 共通設定を作成する

リポジトリのルートで、次のコマンドを実行します。`init` は設定例をコピーし、追跡用の秘密情報を生成します。

```bash
./components/extensions/cvatctl init
chmod 600 .env
```

作成された `.env` はGitへ登録しません。`init` は既存ファイルを上書きしないため、既存環境では[設定例](../../components/extensions/config.example.env)から必要な項目だけを統合します。環境名、ネットワーク、保存ボリューム、秘密情報は元の値を維持してください。

## 最初に確認する設定

初めに、接続先、環境の名前、使う機能を確認します。設定例の `[確認必須]` は利用環境に合うか確認する項目、`[選択時必須]` は該当機能を使う場合に必要な項目、`[自動生成]` は `init` が用意する秘密情報、`[任意]` は通常そのまま使う項目です。

| 設定 | 確認する内容 |
| --- | --- |
| `CVAT_HOST` | ブラウザーから接続するホスト名またはIPです。スキームとポートを含めません。ホスト内だけで使う場合は `localhost` のままにできます。 |
| `COMPOSE_PROJECT_NAME` | Dockerで環境を識別する名前です。新規環境の例は `cvat` です。既存環境では変更しません。 |
| `CVAT_EXTENSIONS` | 使う機能の設定名をカンマで区切ります。初期値にはGPUが必要なSAM2とUltraSAMが含まれるため、必要な機能だけを残します。空欄なら追加機能を選択しません。 |
| `CVAT_NETWORK_NAME` | 任意です。空欄なら `<COMPOSE_PROJECT_NAME>_cvat` になります。既存ネットワークを引き継ぐ場合は実名を指定します。 |
| `CVAT_EXTRA_COMPOSE_FILES` | 任意です。HTTPS、共有フォルダー、外部DBなどの追加設定ファイルをセミコロンで区切ります。 |

機能の名前と内容は[機能の選択と管理](management.md)で確認します。例えば、DICOMを取り込み、画像上の長さとタスクの説明を表示する場合は、生成済み `.env` の該当行を次のように変更します。この例はGPUを必要としません。

```dotenv
CVAT_HOST=cvat.example.internal
COMPOSE_PROJECT_NAME=cvat
CVAT_EXTENSIONS=dicom_import,physical_measurements,task_context
```

## 選んだ機能の設定を確認する

共通設定の次に、選択した機能の節だけを[設定例](../../components/extensions/config.example.env)で確認します。選んでいないモデルの重みや接続先を埋める必要はありません。

| 使う機能 | 必要な確認 | 詳細 |
| --- | --- | --- |
| SAM2 | モデルの大きさと重みを選びます。公式重みを使う場合は `SAM2_CHECKPOINT_HOST` を空欄にします。Redis接続用パスワードは `init` が生成します。 | [モデルの選択と配備](model-selection.md) |
| SAM3.1 | 互換性のある結合済み重みの絶対パスを `SAM31_CHECKPOINT_HOST` に指定します。新規配備時に必要で、自動取得しません。RedisはSAM2と別にします。 | [モデルの選択と配備](model-selection.md) |
| UltraSAM | GPUの番号またはUUIDを確認します。既定はGPU 0です。 | [導入と検証](../ultrasam/operations.md) |
| モデル登録 | 画面のURLとモデル保存先を確認します。同じホストか別ホストかで接続設定が変わります。 | 次の節 |
| DICOM取り込み、アノテーションの一括取り込み、画像の確認機能 | `CVAT_EXTENSIONS` で選びます。機能ごとの追加環境変数は通常不要です。 | [機能の選択と管理](management.md) |
| 画像を選んで実行 | 使用するモデルを別途用意します。登録ONNXモデルなら `model_registry` も選びます。 | [画像を選んで実行](../annotation/selective-ai.md) |

追跡用の保存先を引き継ぐ場合は、`SAM2_REDIS_VOLUME` と `SAM31_REDIS_VOLUME` に元の実名を指定します。空欄なら環境名から自動設定されます。既存のRedis接続用パスワードは保持し、SAM2とSAM3.1で共用しません。

## モデル管理の接続を設定する

ONNXモデル管理を同じDockerホストで使う場合は、`CVAT_MODEL_REGISTRY_URL` を空欄にし、`MR_PUBLIC_URL` を実際のCVAT画面のURLに合わせます。例えば `http://cvat.example.internal:8080` でCVATを開く場合は次のとおりです。

```dotenv
MR_PUBLIC_URL=http://cvat.example.internal:8080/model-registry/
MR_HOME=
CVAT_MODEL_REGISTRY_URL=
```

公開URLは、CVAT画面とスキーム、ホスト、ポートを一致させます。HTTPの経路を振り分ける既存のTraefikが `/model-registry/` を管理サービスへ転送し、専用の公開ポートは追加しません。HTTPSを使う場合は、URLの変更に加えて証明書と接続経路を別途設定します。

保存先は、`MR_HOME` が空欄ならチェックアウト内の `cvat-model-registry/` です。初期化がGitの除外設定を作り、Dockerのビルド対象からも除外します。既にGitへ登録したデータは除外規則だけでは保護できないため、その状態で初期化を進めないでください。保存先を変える場合は絶対パスを指定し、既存データを引き継ぐ場合は元の `MR_HOME` と `MR_INSTANCE` を維持します。

別ホストでモデル管理を動かす場合は、`CVAT_MODEL_REGISTRY_URL` と `CVAT_MODEL_GATEWAY_SECRETS_DIR` の指定が必要です。認証、証明書、既存保存先の移行は[モデル管理の配備](../model-registry/deployment.md)と[保守](../model-registry/operations.md)を参照してください。

## 設定ファイルの書き方

`.env` には展開済みの `KEY=VALUE` を記載し、空白を含む値は引用符で囲みます。`export`、コマンド置換、複数行の値、変数展開は使用できません。ファイルはシェルとして実行されず、記載した設定は親シェルの環境変数より優先されます。

別の場所に設定を保管する場合は、全コマンドで同じ `--env-file` を指定します。バックアップと秘密情報の複製はリポジトリ外へ保管してください。

```bash
./components/extensions/cvatctl --env-file /srv/cvat-config/cvat.env config
```

追加Composeファイルは `CVAT_EXTRA_COMPOSE_FILES` に記載した順に読み込みます。相対パスはリポジトリのルートが基準です。`docker-compose.override.yml` の暗黙の読み込みには依存しません。機能用のComposeと画面プラグインは自動選択されるため、重複指定は不要です。

## ビルドして起動する

ソースを確認してコミットしてからビルドします。イメージのタグはソースから決まるため、ビルド後にコミットしたソースを同一の構成として扱いません。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

`build` はCVATのサーバーと画面、および選択したモデル管理サービスと推論ワーカーなどを作成します。GPUモデルの推論処理は必要に応じて `up` でビルドして配備するため、`build` だけでは準備が完了しません。

このフォークではGitHub Actionsによるイメージのビルドと公開は行いません。公式の `cvat/server` と `cvat/ui` には独自変更が含まれないため、上記の `cvatctl build` で作成します。`pull` は外部の依存イメージを取得する操作です。検証済みイメージの移送と再利用は[更新手順](update.md)を参照してください。

起動後は、[更新手順](update.md)の受入確認と各機能の試験を行ってから公開します。`check` は構成を検査するため、実画像でのGPU推論やブラウザー操作は別途確認します。

## 停止、再開、状態記録

停止する場合は、新規操作を止め、登録と推論の完了を確認してから `down` を実行します。同じホストのモデル管理サービスと推論ワーカー、Nuclioの推論処理も停止し、保存データは保持します。別の推論ホストは別途停止します。

```bash
./components/extensions/cvatctl down
./components/extensions/cvatctl up --no-build
```

再開には停止前と同じ設定と既存イメージを使用します。通常の停止にボリューム削除やデータベースの巻き戻しは含めません。

起動構成の記録は、作業ツリー外の `~/.local/state/cvat-extensions/` 配下へ保存します。`config` が具体的な場所を表示します。`--state-dir` や `XDG_STATE_HOME` を使用する場合は、停止と再開でも同じ場所を指定します。

この記録には秘密情報が含まれます。`.env`、モデル保存先の `config/` と `data/`、証明書と同様に保護してください。別Dockerデーモンへ復元する場合は、復元先の設定と推論処理を確認し、新しい記録を作ります。
