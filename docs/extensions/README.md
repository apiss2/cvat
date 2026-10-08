# CVAT拡張機能の運用

このリポジトリは、チームで利用するCVATの拡張機能を機能別に管理します。
`CVAT_EXTENSIONS`で選んだ機能をCVATと同じComposeプロジェクトへ組み込み、`cvatctl`でビルド、起動、停止をまとめて行います。

| 機能名 | 機能 | 説明書 |
|---|---|---|
| `itgformat` | ITGformatを含むバックエンド構成 | [ITGformat](../itgformat/README.md) |
| `sam2` | SAM2による画像分割と動画のポリゴン追跡 | [SAM2](../sam2/README.md) |
| `ultrasam` | UltraSAMによる正負のポイントを使った超音波画像の分割 | [UltraSAM](../ultrasam/README.md) |
| `model_registry` | ONNXモデルの登録、更新、一覧表示とCVATからの実行 | [モデル管理](../model-registry/operations.md) |

`itgformat`の指定はバックエンドの起動構成を選びます。
形式の登録はソースのimportで行うため、指定から外した場合にも同じソースからビルドしたサーバーではITGformatを利用できます。
機能を追加した場合は、共通の更新、バックアップ、受入確認にもその機能を加えます。

導入済み環境の更新は[更新手順](update.md)、保存と復旧は[バックアップと復元](backup-restore.md)に従います。

## 対象構成

Linux上の単一DockerデーモンでDocker Composeを使用し、CVAT本体と有効な拡張機能の実行確認を通過したコミットを本番用の`release`へ反映します。
本番には検証済みの`release`を配置します。

ホストにはPython 3.10以上、Git、Docker Engine、Docker Compose 2.24.4以上を用意します。
`!override`を含むComposeを使用するため、旧形式の`docker-compose`は使用しません。
SAM2またはUltraSAMを有効にする場合は、Nuclio dashboardと同じ版の`nuctl`、NVIDIA GPU、対応ドライバーとNVIDIA Container Toolkitが必要です。
Nuclioの版は対象コミットの`components/serverless/docker-compose.serverless.yml`と照合します。
各関数は独立した推論環境を使うため、SAM2とUltraSAMのPythonやPyTorchをCVAT本体へインストールしません。

検証環境と本番環境は別サーバー、または別Dockerデーモンに分けます。
CVAT標準Composeには固定のコンテナ名と公開ポートがあるため、同じDockerデーモンでプロジェクト名だけを変更しても同時に稼働できません。
別環境へ復元するときは、メール、Webhook、共有フォルダーへの書き込み先も隔離します。

## コードと実行データの配置

ビルドには完全なCVATチェックアウトを使用します。
各機能のコードと標準の保存先は次のとおりです。

```text
components/extensions/                     共通設定とcvatctl
components/itgformat/                      ITGformatのCompose
components/sam2/                           SAM2のCompose
components/ultrasam/                       UltraSAMのNuclio管理用Compose
components/model_registry/                 管理サーバー、中継、推論ワーカー
cvat/apps/dataset_manager/formats/itgformat/ ITGformatの実装
cvat-ui/plugins/                           機能別のUIプラグイン
serverless/pytorch/facebookresearch/sam2/   SAM2のNuclio関数
serverless/pytorch/camma/ultrasam/           UltraSAMのNuclio関数
.env                                      環境設定
cvat-model-registry/                       モデル管理の設定と保存データ
```

モデル管理は、既定ではリポジトリ直下の`cvat-model-registry/`を使用します。
初期化時に、このフォルダーへ`*`と改行だけを記載した`.gitignore`を作成します。
これにより`.gitignore`自身を含む保存先の全内容がGitの対象外になります。
リポジトリの`.dockerignore`でもこのフォルダーを除外し、モデルや秘密情報をDockerのビルド対象へ送りません。
`.env`もGitとDockerの対象外であることを確認します。

保存先を変える場合は`MR_HOME`へ絶対パスを指定します。
既存のリポジトリ外の保存先も継続して使用できます。
チェックアウトを別の場所へ移す場合は、モデル保存先を引き継ぐか、`MR_HOME`を既存の絶対パスへ固定してから起動します。
バックアップ、リリース用イメージ、作業記録はリポジトリ外に保存します。

## CVAT側の初回設定

以下のコマンドは、リポジトリのルートで実行します。
既存環境では、使用中のComposeプロジェクト名、マウント、設定を事前に記録してください。

```bash
./components/extensions/cvatctl init
chmod 600 .env
```

`init`は既存の設定ファイルを上書きしません。
既存の`.env`がある場合は`components/extensions/config.example.env`の必要な項目を統合します。
以後の`build`または`up`が、未作成のモデル保存先とサービス間トークンを準備します。
既にある保存データとトークンは維持します。

全機能を使う場合の設定例は次のとおりです。
公開URLは実際にブラウザーで開くCVATのスキーム、ホスト名、ポートへ合わせます。
追加Composeが不要な場合は`CVAT_EXTRA_COMPOSE_FILES`を空欄にします。

```dotenv
CVAT_HOST=cvat.example.internal
COMPOSE_PROJECT_NAME=cvat
CVAT_NETWORK_NAME=cvat_cvat
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry
MR_HOME=
MR_PUBLIC_URL=https://cvat.example.internal/model-registry/
MR_CVAT_URL=http://cvat_server:8080
CVAT_MODEL_REGISTRY_URL=
CVAT_EXTRA_COMPOSE_FILES=/srv/cvat-config/site.compose.yml
```

設定ファイルには展開済みの`KEY=VALUE`を記載し、空白を含む値は引用符で囲みます。
シェルの`export`、コマンド置換、複数行の値、別の変数を参照する式は使用しません。
設定ファイルはシェルとして実行されません。
既存の設定をリポジトリ外に置く環境では、全コマンドへ同じ`--env-file /絶対パス/cvat.env`を付けます。

| 設定 | 設定する内容 |
|---|---|
| `CVAT_HOST` | ブラウザーからアクセスするホスト名です。スキームとポートは含めません。 |
| `COMPOSE_PROJECT_NAME` | 既存のComposeプロジェクト名です。チェックアウトのフォルダー名が変わっても維持します。 |
| `CVAT_NETWORK_NAME` | 既存CVATと関数が使用するネットワーク名です。 |
| `CVAT_EXTENSIONS` | 有効にする機能名をカンマで区切ります。 |
| `CVAT_EXTRA_COMPOSE_FILES` | HTTPS、共有フォルダー、外部DBなど運用固有のComposeです。複数はセミコロンで区切ります。 |
| `SAM2_REDIS_VOLUME` | SAM2専用Redisのボリューム名です。既存の追跡状態を引き継ぐ場合は実名を指定します。 |
| `SAM2_REDIS_PASSWORD` | `init`が生成するRedisと追跡関数の共通秘密情報です。 |
| `ULTRASAM_GPU_DEVICE` | UltraSAMの推論に使うGPUの数値IDまたはGPU UUIDです。既定値は`0`です。 |
| `NUCLIO_NAMESPACE` | CVATとNuclio関数の名前空間です。既存関数と一致させます。 |
| `NUCLIO_DASHBOARD_PORT` | 管理用ポートです。ホストのループバックへ公開します。 |

追加Composeは明示した順番で読み込みます。
`docker-compose.override.yml`の暗黙の読み込みに依存せず、使用中のHTTPSや共有フォルダー設定を`CVAT_EXTRA_COMPOSE_FILES`へ記載します。
機能用ComposeとUIプラグインは自動で選択されます。
UltraSAMは標準のAI Toolsへ関数を登録するため、専用のUIプラグインを使用しません。
UltraSAMだけを有効にする場合は、SAM2の追跡用Redisを起動しません。

## ONNXモデル管理の初回設定

同じDockerホストでは、`model_registry`を指定すると管理サーバー、中継、画面の拡張をCVATとまとめて管理します。
`CVAT_MODEL_REGISTRY_URL`は空欄にし、`MR_HOME`と`MR_PUBLIC_URL`をCVATと同じ`.env`へ設定します。
管理サーバーと中継にはホストの公開ポートを設けず、既存のTraefikから`/model-registry/`を転送します。
HTTPSの終端は既存のCVAT公開設定に合わせます。

管理画面はCVATのログイン状態を利用し、CVATのリンクから直接モデル一覧を開きます。
セッションCookieは標準のパス`/`を使用します。
認証情報の再入力や個人用トークンの発行は必要ありません。
セッションの期限切れとCVATへの接続障害は区別して表示します。

別の推論ホストを使用する場合だけ、`CVAT_MODEL_REGISTRY_URL`へ接続先を明示し、推論ホストの`registryctl.py`を使用します。
その場合の接続、サービス間トークン、証明書の配置と、独立した構成から同じComposeへ移す手順は[モデル管理の運用](../model-registry/operations.md)に記載しています。

## ビルドと起動

変更を確認してコミットした後、初回起動前に選択した機能のイメージをビルドします。
イメージのタグはソースから決まるため、コミットの確定後にビルドします。
`build`はCVATのサーバーとUI、有効な中継、モデル管理サーバー、ONNX推論ワーカーを対象にします。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

SAM2またはUltraSAMを有効にした初回の`up`は、対応する関数イメージをビルドしてデプロイします。
UltraSAMの初回ビルドでは公式ソース、依存パッケージとモデル重みを取得するため、[UltraSAMの導入手順](../ultrasam/README.md#導入と起動)も確認します。
本番では利用者の接続を外側のプロキシなどで止め、[更新手順](update.md)に従って検証済みのイメージを使用します。
`up`の完了後はログを確認し、[受入確認](update.md#devで受入確認を行う)を終えてから公開します。

## 停止と再開

保守停止では新規操作を止め、CVATの処理とモデルの登録処理が終了してから次を実行します。

```bash
./components/extensions/cvatctl down
```

同じホストのモデル管理サーバーと、その管理インスタンスの推論ワーカーも停止対象です。
保存データは保持され、次の`up`で再利用します。
Nuclioはdashboardに加えて対象の関数も停止します。
別の推論ホストを明示した構成では、そのホストの停止操作も行います。

日常の停止に、ボリューム削除やデータベースの巻き戻しは含めません。
再開は停止前と同じ設定で`cvatctl up --no-build`を実行し、状態と必要な機能を確認します。

## 状態記録と秘密情報

`cvatctl`は解決済みComposeと停止対象の情報を作業ツリーの外へ保存します。
既定の場所は`~/.local/state/cvat-extensions/`配下で、`config`が具体的な保存先を表示します。
`--state-dir`や`XDG_STATE_HOME`を使う場合は、停止と起動を含む全操作で保存先を維持します。

状態記録には秘密情報が含まれるため、設定ファイルと同じ権限で保管します。
バックアップには`.env`、`MR_HOME/config/`、`MR_HOME/data/`、必要な証明書を含めます。
別のDockerデーモンへ復元した場合は元の稼働記録をそのまま操作に使わず、復元先の設定と関数を確認して新しい状態記録を作成します。
