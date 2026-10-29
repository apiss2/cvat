# モデル管理サービスの配備

[モデル管理の案内](README.md) / [共通の導入手順](../extensions/installation.md)

このページは、ONNXモデルを登録してCVATから使える環境を初めて用意する管理者向けです。この機能は、モデルと画面を管理するサービス、モデルを実行するワーカー、CVATからの推論要求を転送する中継で構成されます。

最初に、モデルをCVATと同じホストで動かすか、別の推論ホストで動かすかを選びます。既存環境の保存先や管理方法を変える場合は、[移行手順](operations.md#保存先と起動管理の移行)を使用してください。

| 配置 | 設定と管理方法 |
| --- | --- |
| CVATと同じDockerホスト | 共通管理コマンド `cvatctl` と共通の `.env` を使用します。通常はこちらの構成です。 |
| 別の推論ホスト | CVAT側は `cvatctl`、推論ホスト側は専用コマンド `registryctl.py` と `registry.env` を使用します。両ホストの接続と認証の設定が必要です。 |

## 同じDockerホストへ配置する

新規環境では、[共通の導入手順](../extensions/installation.md)でLinux、Python、Dockerとリポジトリを準備し、ルートで設定を作成します。既存の `.env` がある場合は、この初期化を繰り返さず必要な項目だけを統合してください。

```bash
./components/extensions/cvatctl init
chmod 600 .env
```

機能の選択では `model_registry` を指定します。以下はモデル管理だけを選ぶ新規環境の例です。既存環境では、引き続き使う他の機能名を残して追記します。

```dotenv
CVAT_EXTENSIONS=model_registry
MR_PUBLIC_URL=http://localhost:8080/model-registry/
MR_HOME=
MR_INSTANCE=
MR_CVAT_URL=http://cvat_server:8080
CVAT_MODEL_REGISTRY_URL=
CVAT_MODEL_GATEWAY_SECRETS_DIR=
```

公開URLは、実際にブラウザーで開くCVATのURLに合わせます。以下の「必須」は機能の利用に必要な項目、「任意」は通常そのまま使える項目です。既存環境の保存先、識別名、認証情報は維持してください。

### 公開URLと保存先

| 設定 | 区分 | 内容 |
| --- | --- | --- |
| `MR_PUBLIC_URL` | 必須 | CVAT画面とスキーム、ホスト、ポートを一致させ、末尾に `/model-registry/` を付けます。 |
| `MR_HOME` | 任意 | モデルと設定の保存先です。空欄ならチェックアウト内の `cvat-model-registry/` です。別の保存先には絶対パスを指定します。 |
| `MR_INSTANCE` | 任意 | 管理サービスの識別名です。空欄なら `COMPOSE_PROJECT_NAME` を使用します。移行時は元の識別名を維持します。 |
| `MR_CVAT_URL` | 任意 | 管理サービスからログイン状態を確認するためのCVAT接続先です。既定は `http://cvat_server:8080` です。 |
| `MR_CVAT_SESSION_COOKIE` | 任意 | CVATのログイン状態を示すCookie名です。既定は `sessionid` です。 |

初期化は保存先とサービス間の認証情報を準備します。既定の保存先はGitとDockerのビルド対象から除外されますが、既にGitへ登録したデータは除外規則だけでは保護できません。既存のモデルと認証情報を作り直さず、[共通の導入手順](../extensions/installation.md#モデル管理の接続を設定する)に従って保存先を引き継ぎます。

同じホストで使用する `MR_CONFIG_DIR`、`MR_HOST_DATA_DIR`、`CVAT_MODEL_GATEWAY_SECRETS_DIR` は、`MR_HOME` 配下の `config/`、`data/`、`config/secrets/` から自動設定されます。通常は個別指定しません。既存の明示設定を残す場合は、自動設定と一致する絶対パスを使用してください。機能用のComposeファイルと画面プラグインも自動選択されます。

### GPUと接続経路

| 設定 | 区分 | 内容 |
| --- | --- | --- |
| `MR_GPU_DEVICE` | 任意 | 空欄ならCPUです。GPUを使用する場合は番号またはUUIDを1個指定します。 |
| `MR_TRAEFIK_ENTRYPOINTS` | 必要時のみ | CVATの接続経路を管理するTraefikの入口名です。既定は `web` です。 |
| `MR_TRAEFIK_TLS` | 必要時のみ | Traefik自身がHTTPS接続を受ける場合の指定です。既定は `false` です。 |

公開経路には既存のTraefikを使い、モデル管理サービスの内部ポート8091へ転送します。管理サービスと中継のホストポートは追加公開しません。`MR_PUBLIC_URL` をHTTPSに変更する場合は、証明書と接続経路も別途用意します。

既存のプロキシを介する場合は、CVATの構成と合わせて `Cookie`、`Origin`、`X-Registry-Request`、`X-Registry-User-ID` を転送します。Cookieのパスは標準の `/` を使用します。推論のメモリーや保存容量などは、[実行資源の設定](operations.md#実行資源と同時実行)を参照してください。

## ビルドして起動する

ソースを確認してコミットしてから、同じコミットのイメージをビルドします。モデル管理サービスとONNXワーカーの両方が対象です。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

起動時には、他に選択した機能も配備します。例えばSAMを選択している場合は、その推論処理も `cvatctl up` が準備します。本番環境の更新では、[共通の更新手順](../extensions/update.md)に従って検証済みのイメージを使用します。

起動後はCVATへログインし、「モデル管理」から一覧を開きます。CVATのログイン状態を使用するため、モデル管理用のパスワード入力や個人用トークンの配布は不要です。公開前に[受入確認](testing.md)を実施し、ログイン状態の失効と接続障害を区別して確認してください。

## 別の推論ホストへ配置する

別ホスト構成では、モデルと推論処理を推論ホストに置きます。CVAT側には画面への入口と要求を転送する中継を残します。推論ホストにもPython、Docker Engine、Docker Composeと `components/model_registry/` 一式を用意してください。

最初に、推論ホストで保存先と設定を作成します。公開URLは利用者が開くCVATのURL、CVATへの接続先は推論ホストから到達できるURLです。

```bash
REGISTRY_HOME=/srv/cvat-model-registry
python3 components/model_registry/registryctl.py init \
  --home "$REGISTRY_HOME" --standalone \
  --public-url https://cvat.example.internal/model-registry/ \
  --cvat-url https://cvat.example.internal
```

次に、作成された `registry.env` の `MR_MANAGER_IMAGE` と `MR_WORKER_IMAGE` を、配備する版を識別できるタグへ設定してからビルドします。`MR_INSTANCE` は初期化時の `--instance` で選べ、未指定時は `team-models` です。同じホストの構成で使う空欄時の規則とは異なります。

```bash
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target manager
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target worker
python3 components/model_registry/registryctl.py up --home "$REGISTRY_HOME"
```

CVAT側の `.env` には、推論ホストの接続先と認証情報の保存先を指定します。別ホストのURLを設定すると、同じCompose内のモデル管理サービスは起動しません。

| CVAT側の設定 | 区分 | 内容 |
| --- | --- | --- |
| `CVAT_MODEL_REGISTRY_URL` | 別ホスト時必須 | CVATの中継から接続する推論ホストのURLです。例は `https://registry.example.internal:8443` です。 |
| `CVAT_MODEL_GATEWAY_SECRETS_DIR` | 別ホスト時必須 | 推論ホストと共有する認証情報を置く、CVATホスト上のディレクトリです。次の節で配置します。 |
| `CVAT_MODEL_REGISTRY_CA_FILE` | 必要時のみ | 独自の認証局を使う場合の、コンテナ内の証明書パスです。 |

ブラウザー向けの公開URLは、推論ホストのURLに変更せず、CVATと同じまま維持します。[CVAT側のNginx設定例](../../components/model_registry/deploy/nginx.example.conf)と[推論ホスト側の設定例](../../components/model_registry/deploy/nginx.remote-registry.example.conf)を既存プロキシへ組み込み、証明書とプロキシのコンテナを別途用意します。

推論ホストでは、モデル管理サービスと同じDockerネットワークにHTTPSプロキシを接続し、内部の `model-registry:8091` へ転送します。標準のネットワーク名は `mr-<MR_INSTANCE>_default` です。公開ポートと接続元制限はプロキシ側で設定し、モデル管理サービス自体のポートは公開しません。

CVAT側プロキシからの接続例は次のとおりです。接続先、ホスト名、認証局のパスは実際の環境へ置き換えます。

```nginx
proxy_pass https://registry.example.internal:8443;
proxy_set_header Host registry.example.internal;
proxy_ssl_server_name on;
proxy_ssl_name registry.example.internal;
proxy_ssl_verify on;
proxy_ssl_trusted_certificate /etc/ssl/certs/company-root-ca.pem;
```

接続確認では、両ホストの実際のプロキシで `nginx -t` を実行し、設定を読み直します。`Cookie`、`Origin` と操作用ヘッダーはブラウザーの値を維持してください。ログイン情報を転送するため、公開ネットワークではHTTPSを使用します。

## サービス間トークンと証明書を配置する

別ホスト構成の認証には、利用者のパスワードとは別に、サービス間だけで共有する **サービス間トークン** を使用します。推論ホストの `config/secrets/service_token` と同じ内容をCVATホストの専用ディレクトリへ配置し、`CVAT_MODEL_GATEWAY_SECRETS_DIR` で指定します。「サービス間トークン」はGit、登録モデル、ブラウザーへ渡しません。

独自の認証局を使う場合は、接続の向きごとに証明書を設定します。標準の認証局で検証できる場合、追加の証明書指定は不要です。

| 接続 | 証明書のホスト上の配置 | 設定するコンテナ内パス |
| --- | --- | --- |
| モデル管理サービスからCVAT | 推論ホストの `MR_CONFIG_DIR/ca/cvat-ca.pem` | `MR_CVAT_CA_FILE=/config/ca/cvat-ca.pem` |
| CVATの中継から推論ホスト | CVATホストの `CVAT_MODEL_GATEWAY_SECRETS_DIR/registry-ca.pem` | `CVAT_MODEL_REGISTRY_CA_FILE=/run/secrets/registry-ca.pem` |

証明書の指定は、それぞれの接続にだけ適用されます。指定ファイルが読めない場合は起動に失敗し、証明書検証を無効化する設定はありません。権限の詳細は[セキュリティ](security.md)を参照してください。
