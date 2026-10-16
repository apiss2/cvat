# モデル管理サービスの配備

[モデル管理の案内](README.md) / [共通の導入手順](../extensions/installation.md)

このページは管理者向けの初回配備手順です。同じDockerホストではcvatctlでCVATとまとめて管理します。別の推論ホストを使う場合だけ、そのホストでregistryctl.pyを使用します。既存構成の移行は[保守手順](operations.md#保存先と起動管理の移行)を参照してください。

## 同じDockerホストへ配置する

Linux、Docker Engine、Docker Composeと完全なCVATチェックアウトを用意し、ルートから共通設定を作成します。既存の.envは上書きせず、必要な項目を統合します。

```bash
./components/extensions/cvatctl init
chmod 600 .env
```

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,model_registry
MR_HOME=
MR_INSTANCE=
MR_PUBLIC_URL=https://cvat.example.internal/model-registry/
MR_CVAT_URL=http://cvat_server:8080
CVAT_MODEL_REGISTRY_URL=
CVAT_MODEL_GATEWAY_SECRETS_DIR=
```

機能名とURLは環境に合わせ、既に有効な機能を維持してください。MR_HOMEが空欄ならチェックアウト内のcvat-model-registry/を使用します。初期化は保存先とサービス間トークンを準備し、保存先全体をGitとDockerのビルド対象から除外します。既存のモデル、トークン、インスタンス名を作り直す必要はありません。

| 設定 | 内容 |
|---|---|
| MR_HOME | 保存先です。既存の外部保存先には絶対パスを指定します。 |
| MR_INSTANCE | 空欄ならCOMPOSE_PROJECT_NAMEです。移行時は旧registry.envの値を維持します。 |
| MR_PUBLIC_URL | CVAT画面とスキーム、ホスト名、ポートを一致させ、末尾に/model-registry/を付けます。 |
| MR_CVAT_URL | 管理サーバーからCVATのユーザー情報APIへ接続する基底URLです。 |
| MR_CVAT_SESSION_COOKIE | CVATのCookie名です。既定はsessionidです。 |
| MR_GPU_DEVICE | 空欄はCPUです。GPUでは1個の番号またはUUIDを指定します。 |
| MR_TRAEFIK_ENTRYPOINTS | 既存Traefikの入口です。既定はwebです。 |
| MR_TRAEFIK_TLS | Traefik自身がTLSを終端する場合の指定です。既定はfalseです。 |

MR_CONFIG_DIR、MR_HOST_DATA_DIR、CVAT_MODEL_GATEWAY_SECRETS_DIRは、MR_HOME配下のconfig/、data/、config/secrets/から決まります。明示的な設定を残す場合は、これらと一致する絶対パスを指定します。機能用ComposeとUIプラグインを重複して登録しません。

公開経路には既存のTraefikを使い、管理サーバーの内部ポート8091へ転送します。管理サーバーと中継のホストポートを追加公開しません。MR_PUBLIC_URLをHTTPSにするだけでは証明書や入口は作成されません。CVATの公開構成と合わせ、Cookie、Origin、X-Registry-Request、X-Registry-User-IDを転送します。Cookieのパスは標準の/を使用します。

## ビルドして起動する

ソースを確認してコミットしてから、同じコミットのイメージをビルドします。管理サーバーとONNXワーカーの両方が対象です。Classificationの追加など出力形式が変わる更新は、画面だけを更新しても使用できません。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

cvatctl upは有効なSAM関数の配備も行います。初回の準備と、本番で検証済みイメージを使う更新を区別し、本番反映は[共通の更新手順](../extensions/update.md)に従います。

利用者はCVATへログインし、「モデル管理」から一覧を開きます。管理画面でのパスワード入力や個人用トークンの配布は不要です。ログイン状態の失効と接続障害を区別して確認し、公開前に[受入確認](testing.md)を実施します。

## 別の推論ホストへ配置する

推論ホストにcomponents/model_registry/一式とDocker Engineを用意します。CVAT側ではCVAT_MODEL_REGISTRY_URLを明示すると、同じCompose内の管理サーバーは起動しません。管理画面用プラグインと中継はCVAT側に残ります。

```bash
REGISTRY_HOME=/srv/cvat-model-registry
python3 components/model_registry/registryctl.py init \
  --home "$REGISTRY_HOME" --standalone \
  --public-url https://cvat.example.internal/model-registry/ \
  --cvat-url https://cvat.example.internal
```

推論ホストのregistry.envでMR_MANAGER_IMAGEとMR_WORKER_IMAGEをリリース固有のタグへ設定してからビルドします。

```bash
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target manager
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target worker
python3 components/model_registry/registryctl.py up --home "$REGISTRY_HOME"
```

ブラウザー向けの公開URLはCVATと同じまま維持します。[CVAT側のNginx設定例](../../components/model_registry/deploy/nginx.example.conf)と[推論ホスト側の設定例](../../components/model_registry/deploy/nginx.remote-registry.example.conf)を、既存プロキシに合わせて使用します。例だけではプロキシコンテナと証明書は作成されません。

推論ホストでは管理サーバーと同じDockerネットワークにHTTPSプロキシを接続し、内部のmodel-registry:8091へ転送します。標準ネットワーク名は`mr-<MR_INSTANCE>_default`です。公開ポートと接続元制限はプロキシ側で設定してください。

CVAT側プロキシから接続する設定の例です。接続先、Host、認証局のパスを環境に合わせます。

```nginx
proxy_pass https://registry.example.internal:8443;
proxy_set_header Host registry.example.internal;
proxy_ssl_server_name on;
proxy_ssl_name registry.example.internal;
proxy_ssl_verify on;
proxy_ssl_trusted_certificate /etc/ssl/certs/company-root-ca.pem;
```

CVAT_MODEL_REGISTRY_URLにはこの例ならhttps://registry.example.internal:8443を指定します。Cookie、Originと操作用ヘッダーはブラウザーの値を維持します。両ホストの実際のプロキシでnginx -tを行い、設定を読み直してから接続を確認します。公開ネットワークでセッションCookieを平文送信しません。

## サービス間トークンと証明書を配置する

推論ホストのconfig/secrets/service_tokenと同じ内容を、CVATホストの専用ディレクトリへ安全に配置し、CVAT_MODEL_GATEWAY_SECRETS_DIRで指定します。トークンをGit、モデル、ブラウザーへ渡しません。

管理サーバーからCVATへ接続するTLSの独自認証局は、MR_CONFIG_DIR/ca/cvat-ca.pemへ配置し、コンテナ内のパスを設定します。

```dotenv
MR_CVAT_CA_FILE=/config/ca/cvat-ca.pem
```

中継から推論ホストへ接続する証明書は別の設定です。中継の秘密ディレクトリへ配置し、CVAT_MODEL_REGISTRY_CA_FILEに/run/secrets/registry-ca.pemなどのコンテナ内パスを指定します。こちらの指定は管理サーバーからCVATへの認証照会には適用されません。

証明書未指定時は標準の検証を使用し、指定ファイルが読めない場合は起動を失敗させます。証明書検証を無効化する設定はありません。権限と信頼境界の詳細は[セキュリティ](security.md)を参照してください。
