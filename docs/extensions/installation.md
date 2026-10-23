# 導入と共通設定

[共通運用](README.md)

このページはLinux上の単一DockerデーモンにCVATと独自拡張を導入する管理者向けです。既存環境の変更は[更新手順](update.md)から開始してください。

## ホストと保存先を準備する

Python 3.10以上、Git、Docker Engine、Docker Compose 2.24.4以上と、完全なCVATチェックアウトを用意します。Composeの`!override`を使うため、旧形式のdocker-composeは使用しません。
SAM2、SAM3.1、UltraSAMにはNVIDIA GPU、対応ドライバー、NVIDIA Container Toolkit、Nuclio dashboardと同じ版のnuctlが必要です。版は`components/serverless/docker-compose.serverless.yml`と照合します。モデル用のPythonとCUDA依存関係をCVAT本体へインストールしません。

検証環境と本番環境は別ホストまたは別Dockerデーモンに分けます。標準Composeには固定のコンテナ名と公開ポートがあるため、プロジェクト名だけの変更では分離できません。復元先のメール、Webhook、共有フォルダー、外部DBも本番から隔離します。

モデル管理の既定保存先はチェックアウト内のcvat-model-registry/です。初期化がこのディレクトリの全内容を除外する.gitignoreを作り、Dockerのビルド対象からも除外します。既に追跡されたデータは除外規則だけでは保護できないため、その状態で初期化を進めないでください。
別の保存先を使う場合はMR_HOMEへ絶対パスを指定します。チェックアウトを移す場合も、既存の保存先を引き継ぐかMR_HOMEを固定します。バックアップと秘密情報の複製はリポジトリ外に保管します。

## 共通設定を作成する

以下はチェックアウトのルートで実行します。

```bash
./components/extensions/cvatctl init
chmod 600 .env
```

initは既存の設定ファイルを上書きしません。既存環境では[設定例](../../components/extensions/config.example.env)から必要な項目だけを統合してください。既存のComposeプロジェクト名、ネットワーク、ボリューム名、秘密情報を維持します。

```dotenv
CVAT_HOST=cvat.example.internal
COMPOSE_PROJECT_NAME=cvat
CVAT_NETWORK_NAME=cvat_cvat
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry
MR_HOME=
MR_PUBLIC_URL=https://cvat.example.internal/model-registry/
MR_CVAT_URL=http://cvat_server:8080
CVAT_MODEL_REGISTRY_URL=
CVAT_EXTRA_COMPOSE_FILES=
```

この例のURLとホスト名は実際の環境へ置き換えます。SAM3.1は必要な場合にsam31を追加し、[モデルの選択と配備](model-selection.md)の重みとRedis設定を行います。

| 設定 | 内容 |
|---|---|
| CVAT_HOST | 公開ホスト名です。スキームとポートは含めません。 |
| COMPOSE_PROJECT_NAME | 既存のComposeプロジェクト名を維持します。 |
| CVAT_NETWORK_NAME | CVATと関数が使用するネットワークです。 |
| CVAT_EXTENSIONS | 有効にする機能名をカンマで区切ります。 |
| CVAT_EXTRA_COMPOSE_FILES | HTTPS、共有フォルダー、外部DBなどの追加Composeをセミコロンで区切ります。 |
| SAM2_REDIS_VOLUME、SAM31_REDIS_VOLUME | 各モデル専用のRedisボリュームです。既存状態を引き継ぐ場合は実名を指定します。 |
| SAM2_REDIS_PASSWORD、SAM31_REDIS_PASSWORD | initが準備する各Redisと追跡関数の秘密情報です。共用しません。 |
| NUCLIO_NAMESPACE | CVATと関数で一致させる名前空間です。 |
| NUCLIO_DASHBOARD_PORT | ループバックへ公開する管理用ポートです。 |

.envには展開済みのKEY=VALUEを書き、空白を含む値は引用符で囲みます。export、コマンド置換、複数行の値、変数展開を使用しません。設定ファイルはシェルとして実行されず、保存した設定は親シェルの環境変数より優先されます。
設定を別の場所に保管する場合は、全コマンドで同じ--env-fileを指定します。

```bash
./components/extensions/cvatctl --env-file /srv/cvat-config/cvat.env config
```

追加Composeは記載した順に読み込みます。docker-compose.override.ymlの暗黙の読み込みに依存しません。機能用Composeと管理対象のUIプラグインは、CVAT_EXTENSIONSの選択に応じて自動設定されます。
UltraSAMは標準のAI Toolsを使うため専用UIは不要で、UltraSAMだけの構成では追跡用Redisを起動しません。
選択実行、フレーム移動用フィルター、条件付きエクスポートも同じ設定で選びます。拡張名の一覧と設定変更は[拡張機能の管理](management.md)を参照してください。
タスク共通の患者情報や撮影場所などを登録して参照する場合は、`task_context` を追加します。既存のサーバーとDBを使用するため、追加サービスとGPUは不要です。操作とAPIは[タスクの背景情報](task-context.md)を参照してください。

## モデル管理の接続を設定する

同じDockerホストではCVAT_MODEL_REGISTRY_URLを空欄にし、MR_HOMEとMR_PUBLIC_URLを共通の.envに設定します。Traefikが/model-registry/を管理サーバーへ転送し、管理サーバーと中継にホストの公開ポートを追加しません。
MR_PUBLIC_URLはCVAT画面とスキーム、ホスト、ポートを揃えます。HTTPSの指定だけでは証明書やTraefikの入口は作成されません。
別ホストの接続、認証、証明書、既存保存先の移行は[モデル管理の配備](../model-registry/deployment.md)と[保守](../model-registry/operations.md)を参照してください。

## ビルドして起動する

ソースを確認してコミットしてからビルドします。イメージのタグはソースから決まるため、ビルド後のコミットを同一の構成として扱いません。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

buildはCVATのサーバーとUI、有効な中継、モデル管理サーバー、ONNXワーカーを対象にします。SAM系の関数は必要に応じてupでビルドして配備するため、buildだけでは関数の準備は完了しません。
初回ビルドの外部接続と重みの条件は[モデルの選択](model-selection.md)と[UltraSAMの運用](../ultrasam/operations.md)を確認してください。
checkの成功は、実GPU推論や画面操作の成功を保証しません。[更新手順](update.md)の受入確認と各機能の試験を行ってから公開します。

## 停止、再開、状態記録

新規操作を止め、登録と推論の完了を確認してからdownを実行します。同じホストの管理サーバーとその推論ワーカー、Nuclio関数も停止し、保存データは保持します。別の推論ホストは別途停止します。

```bash
./components/extensions/cvatctl down
./components/extensions/cvatctl up --no-build
```

再開には停止前と同じ設定と既存イメージを使います。通常の停止にボリューム削除やデータベースの巻き戻しは含めません。

cvatctlの状態記録は作業ツリー外の~/.local/state/cvat-extensions/配下に保存し、configが具体的な場所を表示します。--state-dirやXDG_STATE_HOMEを使う場合は、停止と再開でも同じ場所を使用します。
状態記録には秘密情報が含まれます。.env、MR_HOME/config/、MR_HOME/data/、証明書と同様に保護してください。別Dockerデーモンへの復元では、元の稼働記録をそのまま操作に使わず、復元先の設定と関数を確認して新しい記録を作ります。
