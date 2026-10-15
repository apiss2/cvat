# モデル管理サービスの導入と運用

## 導入する環境

このサービスは、Linux上のDocker Engineでモデルごとの推論コンテナを動かします。
管理者はPython 3とDocker Composeを用意し、CVATリポジトリのルートから以下のコマンドを実行します。
CVAT本体の準備と有効機能の設定は[拡張機能の運用](../extensions/README.md)に従います。

同じDockerホストでは、管理サーバーをCVATと同じComposeプロジェクトに含めます。
設定はCVATと共通の`.env`へ記載し、ビルド、起動、停止は`cvatctl`で行います。
利用者はCVAT上部の「モデル管理」から、同じ公開URLの`/model-registry/`へアクセスします。
別の推論ホストを使用する構成も利用できます。

既存の独立したモデル管理サービスを同じComposeへまとめる場合は、[保存先と起動管理の移行](#保存先と起動管理の移行)を使用します。

## 同じDockerホストでの初期設定

新規環境では`cvatctl init`で`.env`を作成します。
既存の`.env`がある場合は、その値を維持して必要な項目を追加します。
同じキーを重複させず、URLとホスト名を実際の環境へ置き換えます。

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

`MR_HOME`が空欄なら、リポジトリ直下の`cvat-model-registry/`を使用します。
`init`または最初の`build`、`up`が保存先と内部通信専用のサービス間トークンを準備します。
保存先には、次の内容の`.gitignore`を作成します。

```gitignore
*
```

`.gitignore`自身を含め、このフォルダーの内容はGitの追跡対象になりません。
リポジトリの`.dockerignore`にも除外規則があるため、モデルと秘密情報をDockerのビルド対象へ送りません。
既に追跡されたファイルがある場合は除外規則だけでは追跡を解除できないため、初期化はその状態を拒否します。

| 項目 | 設定 |
|---|---|
| `MR_HOME` | 空欄ならリポジトリ直下の`cvat-model-registry/`です。既存のリポジトリ外の保存先を使う場合は絶対パスを指定します。 |
| `MR_INSTANCE` | 空欄なら`COMPOSE_PROJECT_NAME`を使用します。既存データを引き継ぐ場合は、元の`registry.env`の値を指定します。 |
| `MR_PUBLIC_URL` | ブラウザーで開くCVATのURLに`/model-registry/`を付けます。スキーム、ホスト名、ポートをCVAT画面と揃えます。 |
| `MR_CVAT_URL` | 管理サーバーからCVATの`GET /api/users/self`へ接続する基底URLです。同じホストの既定は`http://cvat_server:8080`です。 |
| `MR_CVAT_SESSION_COOKIE` | CVATのセッションCookie名です。既定は`sessionid`で、CVAT側で変更している場合だけ合わせます。 |
| `MR_GPU_DEVICE` | 空欄ならCPUです。GPUを使う場合は1個の番号またはUUIDを指定します。 |
| `MR_TRAEFIK_ENTRYPOINTS` | 既存CVATのTraefikで使用する入口です。既定は`web`です。 |
| `MR_TRAEFIK_TLS` | Traefik自身がTLSを終端する場合だけ、その構成に合わせます。既定は`false`です。 |

同じホストの`MR_CONFIG_DIR`、`MR_HOST_DATA_DIR`、`CVAT_MODEL_GATEWAY_SECRETS_DIR`は、`MR_HOME`配下の`config/`、`data/`、`config/secrets/`から自動で決まります。
設定を残す場合は、これらの保存先と一致する絶対パスである必要があります。
`CVAT_MODEL_REGISTRY_URL`は空欄で同じComposeの管理サーバーへ接続します。
個々のComposeファイルやUIプラグインを追加設定へ重複登録する必要はありません。

HTTPSの終端は既存のCVAT公開設定に合わせます。
`MR_PUBLIC_URL`へHTTPSを指定するだけで、Traefikの証明書やHTTPSの入口が自動作成されるわけではありません。
管理サーバーはホストポートを公開せず、TraefikからDockerネットワーク上の`model-registry:8091`へ接続します。
CVATのセッションCookieは標準のパス`/`を使用し、プロキシは`Cookie`、`Origin`、`X-Registry-Request`、`X-Registry-User-ID`をそのまま転送します。

## ビルドと起動

変更を確認してコミットした後、同じソースから全機能のイメージをビルドします。
同じホストでは、管理サーバーとONNX推論ワーカーも`cvatctl build`の対象です。
イメージのタグはソースから自動で決まり、管理サーバーとワーカーの参照は`cvatctl config`で確認できます。
ビルド後にコミットするとタグも変わるため、コードを確定してからビルドします。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

SAM2の初回構築では、`up`が関数のビルドとデプロイも行います。
検証済みのイメージで起動する本番では、[更新手順](../extensions/update.md)に従って`up --no-build`を使用します。
CVATとモデル管理を別々に起動する必要はありません。

CVATのリンクから管理画面を開くと、そのままモデル一覧を表示します。
セッションが失効している場合は「CVATへ戻る」から同じタブでCVATへ移動します。
CVATへの接続障害や設定不備は接続エラーとして表示し、「再試行」で状態を確認できます。
管理サーバーが再起動しても、CVATのセッションが有効なら認証情報の再入力はありません。

停止前に利用者の新規操作を止め、登録、更新、推論の処理完了を確認します。
`down`は管理サーバーとその推論ワーカーを含む全構成を止め、保存データを保持します。

```bash
./components/extensions/cvatctl down
./components/extensions/cvatctl up --no-build
```

## 別の推論ホストへの配置

別のLinuxホストでは `components/model_registry/` 一式を配置し、そのホストにDocker Engineを用意します。
CVAT側の`.env`には`CVAT_MODEL_REGISTRY_URL`で別ホストのURLを指定し、モデル管理のUIプラグインと中継サービスを配置します。
この接続先を明示した場合だけ、CVAT側に管理サーバーを作成しません。
推論ホストの初期化では`--standalone`を指定し、管理サーバーから到達できるCVATのURLを明示します。
この別ホスト構成では、`registry.env`の`MR_MANAGER_IMAGE`と`MR_WORKER_IMAGE`へリリース固有のタグを設定してからビルドします。

```bash
REGISTRY_HOME=/srv/cvat-model-registry
python3 components/model_registry/registryctl.py init \
  --home "$REGISTRY_HOME" \
  --standalone \
  --public-url https://cvat.example.internal/model-registry/ \
  --cvat-url https://cvat.example.internal
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target manager
python3 components/model_registry/registryctl.py build --home "$REGISTRY_HOME" --target worker
python3 components/model_registry/registryctl.py up --home "$REGISTRY_HOME"
```

CVATを公開しているプロキシに、`/model-registry/` を推論ホストへ転送する設定を追加します。
[CVAT側のNginx設定例](../../components/model_registry/deploy/nginx.example.conf)を既存のCVAT公開構成へ合わせる出発点として使います。
ブラウザーの公開URLはCVATと同じまま維持し、推論ホストのポートを利用者へ案内しません。

推論ホストには、管理サーバーと同じDockerネットワークへ接続したHTTPSプロキシコンテナを用意します。
`--standalone` の標準ネットワーク名は `mr-<MR_INSTANCE>_default` です。
推論ホストの`registry.env`にある`MR_INSTANCE`と実際のDockerネットワークを照合して、プロキシの接続先を指定します。
[推論ホスト側のNginx設定例](../../components/model_registry/deploy/nginx.remote-registry.example.conf)は、このプロキシから `http://model-registry:8091` へ接続する構成です。
既存の管理サーバーへホストポートを追加する必要はありません。
TLS用の公開ポート、証明書、CVATの中継サービスおよび公開プロキシの実際の接続元IPは、推論ホストのプロキシ側に設定します。
Nginxの設定例だけでは、このプロキシコンテナや証明書は作成されません。
CVAT側の転送設定では `proxy_pass` と `Host` を次の値へ変更し、証明書検証の設定を同じ `location` に追加します。

```nginx
proxy_pass https://registry.example.internal:8443;
proxy_set_header Host registry.example.internal;
proxy_ssl_server_name on;
proxy_ssl_name registry.example.internal;
proxy_ssl_verify on;
proxy_ssl_trusted_certificate /etc/ssl/certs/company-root-ca.pem;
```

`Cookie`、`Origin`、`X-Registry-Request` はCVATを開いたブラウザーの値のまま転送します。
CVAT側の `CVAT_MODEL_REGISTRY_URL` には、この例なら `https://registry.example.internal:8443` を指定します。
設定変更後は両ホストでNginxの設定検査を行い、設定を読み直してから公開URLと中継の接続を確認します。
コンテナで運用するNginxでは、対象のプロキシコンテナ内で `nginx -t` と `nginx -s reload` を実行します。

推論ホストとの接続には、接続元をCVATホストへ限定した内部ネットワーク、または証明書を検証するHTTPS接続を使用します。
同じホストの標準構成はCVATのTraefikを使い、別ホストの構成は推論ホスト側のHTTPSプロキシを使います。
どちらの構成も、プロキシから管理サーバーの内部ポート8091へ接続します。

CVAT側の `CVAT_MODEL_REGISTRY_URL` を、実際に中継サービスから到達できる推論ホストのURLへ変更します。
推論ホストの `config/secrets/service_token` と同じ内容をCVATホストの専用ディレクトリへ安全に配置し、そのディレクトリを `CVAT_MODEL_GATEWAY_SECRETS_DIR` へ指定します。
秘密ファイルをGit、モデルパッケージ、ブラウザーへ渡しません。

管理サーバーからCVATのユーザー情報APIへ接続できることも確認します。
CVATのセッションCookieがこの接続を通るため、別ホスト間の平文の公開経路を使用しません。
CVATのHTTPS証明書に独自の認証局を使う場合は、その認証局のPEM証明書をホスト側の `MR_CONFIG_DIR/ca/cvat-ca.pem` に配置します。
標準の保存先なら `/srv/cvat-model-registry/config/ca/cvat-ca.pem` です。
`registry.env` にはコンテナ内のパスを指定し、管理サーバーを再起動します。

```dotenv
MR_CVAT_CA_FILE=/config/ca/cvat-ca.pem
```

未指定の場合は標準の証明書検証を使用し、指定した証明書が読めない場合は起動時にエラーとします。
証明書の検証を無効化する設定はありません。

中継サービス側には `CVAT_MODEL_REGISTRY_CA_FILE` で内部認証局の証明書ファイルを指定できます。
証明書を中継用の秘密ディレクトリへ置き、コンテナ内のパスである `/run/secrets/registry-ca.pem` などを指定します。
この設定は管理サーバーからCVATへの認証接続には適用されません。

## 利用開始前の確認

受入確認には、所有者A、別の一般ユーザーB、管理者を使います。
以下は、設定の読み込みだけでは確認できないブラウザーと実推論の項目です。

| 確認 | 合格条件 |
|---|---|
| 画面への到達 | CVAT上部の「モデル管理」から公開URLを開けます。SSHや個人用トークンの配布を必要としません。 |
| 認証 | CVATへログイン済みなら、モデル管理での再入力なしに一覧が表示されます。有効なセッションがない場合だけ管理APIが401を返し、同じタブでCVATへ戻れます。 |
| セッションの終了 | CVATでログアウトした後やセッションの期限切れ後にモデル管理を操作すると、再びCVATへのログインが必要になります。 |
| 操作要求の検査 | 更新要求の `Origin` が公開URLと異なる場合、または `X-Registry-Request: 1` がない場合は403を返します。 |
| 一覧の共有 | Aが公開したモデルをBの一覧で確認でき、作者名と記入済みの連絡先が表示されます。 |
| 操作権限 | BはAの更新、削除、試験、ログへアクセスできず、Aと管理者は操作できます。 |
| 登録 | detectionとsegmentationの作例が試験を通過して公開されます。 |
| ポリゴン | 代表的な実画像で、小領域の除外と頂点間隔が用途に合うことを確認します。穴を持つ領域の扱いも確認します。 |
| 二値出力 | 自作コードが `(N,H,W)` の0か1を返し、ラベルの配列順と縦横サイズが合っています。 |
| 更新 | 更新成功後に新しい版が表示され、更新失敗時は公開中の版が維持されます。 |
| CVATでの実行 | 単一画像と一括アノテーションで、ラベルを対応付けて矩形またはポリゴンを保存できます。 |
| 既存機能 | 有効にしているITGformatの読み書き、SAM2の対話操作と追跡が継続して動きます。 |
| 一括管理 | `cvatctl build`で必要なイメージが揃い、`down`で管理サーバーとその推論ワーカーが止まります。再起動後もモデルと権限が残り、CVATのセッションで一覧と推論を使用できます。 |
| 保存先 | `cvat-model-registry/`のモデル、秘密情報、`.gitignore`がGitに現れず、Dockerのビルド対象にも含まれません。 |
| 接続障害 | CVATへの接続失敗や設定不備は接続エラーとして表示され、ログインをやり直す画面には切り替わりません。 |

管理APIの実接続確認には、付属の試験スクリプトも使用できます。
専用の試験用CVATアカウントで2個の試験モデルを登録し、推論、更新、旧版の実行、版の切り替え、削除を確認します。
CVATのアノテーションは変更しませんが、削除済みモデルの記録と試験用の保存ファイルは残ります。
このCLI試験はCVATの標準ログインAPIへ直接接続し、取得したCVATのセッションCookieを使用します。
パスワードは実行時に入力し、コマンドライン引数へ書きません。
パスワードやCVATトークンを管理サーバーへ渡さず、取得したCookieをファイルへ保存しません。
終了時はCVATの標準ログアウトAPIを呼び、CLIが取得したセッションを終了します。
このログアウトにより同じアカウントの既存RESTトークンも無効になる場合があるため、日常業務や別の連携処理で使用するアカウントを試験に使わないでください。
ブラウザーのログイン状態を再利用する操作は、上の受入確認で別途確認します。

```bash
python3 components/model_registry/tools/smoke_registry.py \
  --url https://cvat.example.internal/model-registry/ \
  --username ACTUAL_CVAT_TEST_USERNAME --run
```

CVAT側からの設定と関数一覧だけを調べる場合は、[CVAT側の確認スクリプト](../../components/model_registry/tools/verify_in_cvat.py)をCVATサーバーと自動アノテーション用ワーカーのPythonへ渡して実行できます。
SAM2を有効にしている場合は `--require-sam2`、モデルを公開済みの場合は `--require-registry-model` を付けます。
この検査は実際の推論やブラウザーの権限確認を代替しません。

## メンバーが行う操作

モデル作成と出力形式は[モデル登録手順](../../components/model_registry/README.md)を参照します。
登録に失敗した場合は、自分のモデルの「詳細」で処理状態とログを確認します。
更新は「このモデルを更新」から開始し、マニフェスト、コード、全ての重み、試験画像をまとめて送ります。

公開されている過去の版へ戻すときは、詳細画面で版を選び、試験を行って公開先を切り替えます。
モデルの登録、更新、削除、公開版の切り替えではCVATサービスの再起動は必要ありません。
画面を閉じても、管理サーバーが受理した登録処理は継続します。

CVATのアカウントの作成、利用停止、パスワード変更はCVATで行います。
利用者を無効化しても、その人が公開したモデルは他のメンバーのために公開状態を維持します。
モデル自体の利用を止める場合は管理者がモデルを削除します。

## 保存先と起動管理の移行

この節は、管理サーバーを独立したComposeで稼働している環境を、CVATと同じComposeへまとめる手順です。
設定の読み方も変わるため、ソースと設定を更新する前に、現在のコードで管理サーバーとCVATの両方を停止します。
先に利用者の新規操作を止め、登録、更新、推論を完了させます。

```bash
OLD_REGISTRY_HOME=/srv/cvat-model-registry
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
python3 components/model_registry/registryctl.py down --home "$OLD_REGISTRY_HOME"
```

停止後は、[バックアップと復元](../extensions/backup-restore.md)に従って現在の設定とデータを保存します。
旧`registry.env`から、`MR_INSTANCE`、使用イメージ、公開URL、GPU、資源制限、証明書の設定を記録します。
`config/secrets/service_token`は既存の値を引き継ぎます。
同じデータへ接続した新旧の管理サーバーを同時に起動しません。

リポジトリ直下へ移す場合は、未使用の移行先へ保存データをコピーします。
次は、旧保存先に標準の`config/`と`data/`があり、移行先がまだ存在しない場合の例です。
移行先が存在する場合は重ねずに止め、内容と使用状況を確認します。
`data/run/`のソケットと`data/uploads/`の一時アップロードはコピーしません。

```bash
MR_HOME="$(pwd)/cvat-model-registry"
mkdir -m 700 "$MR_HOME"
printf '*\n' > "$MR_HOME/.gitignore"
sudo rsync -a --exclude '/data/run/' --exclude '/data/uploads/' \
  --exclude '/.gitignore' "$OLD_REGISTRY_HOME/" "$MR_HOME/"
```

モデル、SQLiteの関連ファイル、試験画像、サービス間トークンと証明書がコピーされたことを確認します。
旧保存先は削除せず、移行の確認が終わるまで停止状態で保持します。
保存先を移さない場合はコピーを省略し、既存の絶対パスを`MR_HOME`へ設定します。

続いて、コードを更新し、旧`registry.env`の必要な項目を、停止前と同じパスのCVAT設定ファイルへ統合します。
ルートの`.env`を使っていた場合はそのファイルを更新します。
同じホストの構成では、`MR_HOME`と旧`MR_INSTANCE`を設定し、`CVAT_MODEL_REGISTRY_URL`を空欄にします。
移行先がリポジトリ直下なら、`MR_HOME`も空欄で構いません。
`MR_CONFIG_DIR`、`MR_HOST_DATA_DIR`、`CVAT_MODEL_GATEWAY_SECRETS_DIR`は旧パスを削除して自動設定を使用するか、移行先から決まる絶対パスへ一致させます。
同じホストで使用した`MR_CVAT_NETWORK`は、CVAT側の`CVAT_NETWORK_NAME`へ統一します。

次は元のインスタンス名が`team-models`である場合の例です。
この名前は旧設定の実際の値へ置き換えます。
同じ名前を維持することで、保存済みモデルとワーカーの識別を引き継ぎます。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,model_registry
MR_HOME=
MR_INSTANCE=team-models
MR_PUBLIC_URL=https://cvat.example.internal/model-registry/
MR_CVAT_URL=http://cvat_server:8080
CVAT_MODEL_REGISTRY_URL=
CVAT_MODEL_GATEWAY_SECRETS_DIR=
```

`MR_AUTH_MODE=token`、`MR_USERS_FILE`、`MR_SESSION_SECONDS`は使用しません。
個人用トークン方式から移行する場合は、保存済みモデルの所有者名とCVATのユーザー名を照合します。
名前が異なるモデルの所有権は自動で移しませんが、CVATの管理者は引き続き操作できます。

コードの変更を確認してコミットしてから、[ビルドと起動](#ビルドと起動)を実行します。
新しいComposeの管理サーバーだけが動き、モデル一覧、作者、権限、公開版、CVATからの推論が維持されていることを確認します。
以後、同じホストで`registryctl.py up`を追加実行する必要はありません。
コピーされた旧`registry.env`も稼働設定には使用しません。

## イメージと画面の更新

管理画面のHTML、CSS、JavaScriptは管理サーバーのイメージに含まれます。
ソースファイルを配置しただけでは実行中の画面は更新されないため、[更新手順](../extensions/update.md)に従って停止、コードの確定、ビルド、起動を行います。
モデル管理とワーカーの更新も、同じ`cvatctl`の操作に含まれます。

ブラウザーを再読み込みし、開発者ツールのNetworkで`/model-registry/`、`/model-registry/static/style.css`、`/model-registry/static/app.js`の応答を確認します。
CSSとJavaScriptの要求にCVAT本体のHTMLが返る場合は、静的ファイルの経路が管理サーバーへ転送されていません。
古い画面が残る場合は、実行中のイメージ、公開プロキシの接続先、ブラウザーのキャッシュを確認します。
画面の更新に、保存先の再初期化は必要ありません。

保存済みマニフェストの`mask`ラベルは`polygon`として扱い、旧来の`threshold`は受理後に取り除きます。
`Mask`のリスト、または`Box`と`Mask`の混在リストを返す既存コードには互換処理があり、`Mask`に同じポリゴン変換を適用します。
既存コードが`params.threshold`を参照する場合は、その属性がないため実行に失敗します。
必要な判定をコード内へ移し、試験を通したパッケージを新しい版として登録してから使用します。

## 実行資源と同時実行

実行資源はCVATの`.env`へ設定します。
変更前に`cvatctl down`で停止し、設定を変更してから`cvatctl up --no-build`で再開します。
この手順ではCVATを含む構成全体を停止するため、保守時間に実施します。
別の推論ホストでは、そのホストの`registry.env`と`registryctl.py`を使用します。

```dotenv
MR_MAX_LOADED=2
MR_WORKER_MEMORY_MIB=4096
MR_WORKER_CPUS=2
MR_STORAGE_GIB=50
```

設定の意味は順に、読み込み済みの版の数、ワーカー1個のホストRAM、ワーカー1個のCPU数、保存容量です。
ログインセッションの有効期間はCVAT側の設定に従います。
同じ版の推論は1回ずつで、推論待ちの要求を保持するキューや複数GPUへ自動分散する機能はありません。
読み込みと1回の推論の上限はそれぞれ90秒です。

GPUを使用する場合は、`.env`で`MR_GPU_DEVICE=0`などを指定します。
NVIDIAドライバーとNVIDIA Container Toolkitを用意し、GPU用のワーカーイメージをビルドします。
指定できるGPUは1個の番号またはUUIDで、`all` は使いません。
GPUの選択はサービス全体の設定であり、メンバーが登録するモデルから変更できません。

## ログとエラーの確認

管理画面では、所有者と管理者がモデルごとのログを確認できます。
要求ID、版、処理段階、例外、ワーカーの標準出力を使って、失敗した登録や推論を追跡します。
入力画像そのものをプラットフォームのログへ保存しませんが、モデルコードが内容を出力すると記録されます。

イベントログは全体で直近1万件、モデルの表示とダウンロードは直近200件です。
ワーカーの標準出力は直近部分のスナップショットで、隣接する要求では同じ行が重複することがあります。
サービスの起動失敗やネットワーク障害は次のコマンドで確認します。

```bash
./components/extensions/cvatctl status
# cvatは実際のComposeプロジェクト名へ置き換えます。
docker ps --filter label=com.docker.compose.project=cvat \
  --filter label=com.docker.compose.service=model-registry
# 一覧で確認した実際のコンテナ名またはIDを指定します。
docker logs --tail 200 ACTUAL_REGISTRY_CONTAINER
# 中継のログはservice=model-gatewayで同様に確認します。
```

| 状態 | 確認する内容 |
|---|---|
| 401 | CVATのログイン状態、セッション期限、Cookieの名前とパス、利用者の有効状態を確認します。内部APIの場合はサービス間トークンの一致を確認します。 |
| 403 | CVAT側の利用停止やアクセス拒否、モデルの所有者権限、公開URLとブラウザーのOriginの一致、`X-Registry-Request: 1`の転送を確認します。 |
| 409 | 同時更新、登録待ち上限、保存容量、関数ID衝突などのメッセージを確認します。 |
| 410 | 削除されたモデルです。CVATで別のモデルを選びます。 |
| 413 | ZIPまたは画像のサイズと、プロキシ側の受信上限を確認します。 |
| 422 | 重み名、ラベル、出力配列、二値の値、画像サイズ、座標、モデルの引数を確認します。 |
| 429 | CVATのユーザー情報APIの要求が制限されています。CVAT側の制限を確認し、待ってから再試行します。 |
| 502 | CVATとの連携エラーなら、応答本文で公開ホスト設定と応答形式のどちらが原因かを確認します。CVATの400と406の確認事項は表の下に記載します。推論エラーならONNXの読み込み、Python例外、GPU、メモリ不足を確認します。 |
| 503 | CVATのユーザー情報APIへの到達性、DNS、TLS、応答時間と応答内容を確認します。推論中の応答なら同じ版の実行中や全ワーカーの使用中も確認します。 |
| 504 | 中継から推論ホストへの接続と応答時間を確認します。 |

管理サーバーからCVATのユーザー情報APIへ照会するときは、接続先に`MR_CVAT_URL`を使い、HTTPの`Host`と公開スキームは`MR_PUBLIC_URL`から設定します。
CVATが内部サービス名を公開ホストとして拒否しないよう、この2つのURLの役割を分けます。
CVATが400を返した場合は、`MR_PUBLIC_URL`、CVATの許可ホスト、プロキシ設定とCVATのログを確認します。
CVATが406を返した場合は、要求した応答形式が受け付けられていません。
管理サーバーは、CVATの[API契約に沿ったヘッダー](architecture.md#cvatのログイン状態を使う画面と認証)を指定します。

```http
Accept: application/vnd.cvat+json
```

406の場合は、稼働中の管理サーバーにこの指定が含まれること、プロキシがヘッダーを書き換えていないこと、接続先CVATの応答形式を確認します。
管理APIはCVATの400と406を502として返し、応答本文で原因を区別します。

## 保存容量と物理削除

モデル保存先には、公開した全ての版と試験画像を保持します。
既定の上限は50 GiBで、登録前にその上限まで3 GiB分以上の余裕と、実ファイルシステムに4 GiB以上の空きを要求します。
この事前検査は同時書き込みへの厳密な容量制限ではないため、必要な環境では専用ボリュームやホストの容量制限も設定します。

画面上の削除は利用停止で、重みの物理削除は行いません。
物理削除が必要な場合は、対象モデルが削除済みであることを確認し、管理サーバーを停止してから次の操作を行います。
この操作を取り消すにはバックアップが必要です。

```bash
./components/extensions/cvatctl config
# configに表示されたstate_directoryの絶対パスを指定します。
CVAT_STATE_DIR=/actual/state/directory
./components/extensions/cvatctl down
sudo ./components/extensions/cvatctl --env-file "$(pwd)/.env" \
  --state-dir "$CVAT_STATE_DIR" purge-deleted \
  --model-id ACTUAL_20_HEX_MODEL_ID --confirm
./components/extensions/cvatctl up --no-build
```

所有者の記録と削除した記録は残ります。
公開中のモデルから過去の版だけを物理削除する操作は提供しません。
管理コンテナが作るファイルの所有者はrootのため、上の例では物理削除を`sudo`で実行します。
`sudo`を付ける場合も、通常の運用担当者が使う`state_directory`を明示し、同じ停止済み構成を参照します。

## バックアップと復元

バックアップでは、新規操作を止めて登録処理の完了を確認し、`cvatctl down`で管理サーバーと推論ワーカーも停止します。
次の保存先を同じ時点の組として保持します。

| 保存対象 | 内容と用途 |
|---|---|
| CVATの`.env` | 使用イメージ、公開URL、ホストの保存先などの設定 |
| `MR_HOME/config/`全体 | サービス間トークン、追加の信頼証明書など |
| `MR_HOME/data/`の永続データ | SQLiteと関連ファイル、登録コード、重み、試験画像、公開版、イベントログ |
| 使用イメージとリポジトリのコミット | 同じ実装で復元するためのイメージのタグ、ID、保存物とソース |
| `cvatctl`の状態記録 | 起動に使ったComposeと停止対象を確認するための記録 |

`data/run/`のソケットと`data/uploads/`の一時ファイルは復元対象にしません。
別ホストの構成では、推論ホストの`registry.env`と、CVATホストへ複製したサービス間トークンも保存します。
そのホストの管理サーバーは`registryctl.py down`で別途停止します。

以下はリポジトリ直下の標準保存先を使用する場合の例です。
`MR_HOME`や設定ファイルの場所を変更した環境では、実際の絶対パスへ置き換えます。
バックアップ先はリポジトリ外に用意し、別の復元点へ上書きしません。

```bash
REGISTRY_HOME="$(pwd)/cvat-model-registry"
CVAT_ENV="$(pwd)/.env"
REGISTRY_BACKUP=/secure/backup/cvat-model-registry.tar.gz
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
sudo tar --acls --xattrs --numeric-owner \
  --exclude='data/run' --exclude='data/uploads' \
  -czf "$REGISTRY_BACKUP" -C "$REGISTRY_HOME" .gitignore config data
sudo cp -p "$CVAT_ENV" "$REGISTRY_BACKUP.env"
sudo chmod 600 "$REGISTRY_BACKUP" "$REGISTRY_BACKUP.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" up --no-build
```

復元先は停止済みで、復元前の状態を別途退避した場所にします。
元と同じ設定とイメージを用意し、所有者と権限を保って展開します。
保存した`.env`の必要な値を復元し、保存先を変える場合は`MR_HOME`と明示的な絶対パスを復元先へ合わせます。
復元前の本番環境へ誤って接続しないよう、外部DB、共有フォルダー、公開URLも確認します。

```bash
sudo tar --acls --xattrs --numeric-owner -xzf "$REGISTRY_BACKUP" -C "$REGISTRY_HOME"
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" up --no-build
```

復元後はCVATからモデル管理を開き、公開モデルの一覧、作者と権限、代表モデルの試験、CVATからの実行を確認します。
このモデル用アーカイブはCVATのデータベースやSAM2の保存先を含まないため、CVAT全体の復元には[バックアップと復元](../extensions/backup-restore.md)も実施します。

## サービス間トークンの交換

サービス間トークンを交換する場合は、計画停止中に管理サーバーとCVAT側の秘密ファイルを同じ内容へ更新します。
ディレクトリ単位のマウントを維持してファイルを置き換え、起動後に中継と管理サーバーの接続を確認します。
