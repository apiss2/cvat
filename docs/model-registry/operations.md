# モデル管理サービスの保守

[モデル管理の案内](README.md) / [初回配備](deployment.md) / [検証](testing.md)

このページは稼働後の管理者向けです。利用者の登録と部分更新は[利用手順](usage.md)に分けています。以下の停止操作は同じホストのCVAT全体も停止するため、保守時間に実施してください。

## 停止とイメージの更新

新規操作を止め、登録、更新、推論が完了してから現在のコードと設定でcvatctl downを実行します。管理サーバーと自身のワーカーも止まり、モデルデータは保持されます。同じ構成の再開にはup --no-buildを使用します。

```bash
./components/extensions/cvatctl down
./components/extensions/cvatctl up --no-build
```

コード変更では[共通の更新手順](../extensions/update.md)に従い、コミットを確定してからビルドします。HTML、CSS、JavaScriptは管理サーバーのイメージに含まれるため、ファイル配置だけでは画面は更新されません。Classificationなどの出力契約変更ではONNXワーカーも更新します。

古い画面が残る場合は、実行中イメージ、プロキシの転送先とブラウザーのキャッシュを確認します。/model-registry/static/app.jsやstyle.cssにCVAT本体のHTMLが返る場合は転送先が誤っています。画面の更新にモデル保存先の再初期化は不要です。

旧Mask出力や旧maskラベルには互換処理がありますが、PredictParamsにthresholdはありません。params.thresholdを読むモデルは必要な判定をmodel.py内へ移し、試験した新しい版に更新します。

## 実行資源と同時実行

設定変更前に停止し、同じ.envを編集して再開します。別の推論ホストではそのホストのregistry.envとregistryctl.pyを使用します。

```dotenv
MR_MAX_LOADED=2
MR_WORKER_MEMORY_MIB=4096
MR_WORKER_CPUS=2
MR_STORAGE_GIB=50
```

順に保持する版数、ワーカー1個のホストRAM、ワーカー1個のCPU数、保存容量です。同じ版の推論は1回ずつで、待ち行列や複数GPUへの自動分散はありません。読み込みと1回の推論はそれぞれ90秒が上限です。

GPUを使う場合はMR_GPU_DEVICEに1個の番号またはUUIDを設定し、対応ドライバーとNVIDIA Container Toolkit、GPU用ワーカーイメージを準備します。allは使用できません。GPUの選択は管理者設定であり、登録モデルから変更できません。

## ログとエラーを確認する

モデルの詳細画面では、所有者と管理者が処理ログを確認できます。要求ID、版、段階、例外と標準出力を照合します。標準のログに画像本体は保存しませんが、モデルコードが内容を出力すると記録されます。全体のイベントは直近1万件、モデル別の表示と保存は直近200件です。標準出力のスナップショットでは隣接要求に同じ行が現れる場合があります。

```bash
./components/extensions/cvatctl status
# cvatは実際のComposeプロジェクト名へ置き換えます。
docker ps --filter label=com.docker.compose.project=cvat \
  --filter label=com.docker.compose.service=model-registry
# 一覧で確認したコンテナ名またはIDを指定します。
docker logs --tail 200 ACTUAL_REGISTRY_CONTAINER
```

中継のログはservice=model-gatewayで同様に確認します。

| 応答 | 主な確認対象 |
|---|---|
| 401 | CVATのログイン状態、Cookie名とパス、アカウント。内部APIではサービス間トークン。 |
| 403 | 所有者権限、利用停止、Origin、操作用ヘッダー。 |
| 409 | 同時更新、登録数、保存容量、関数IDの衝突。 |
| 410 | モデルの削除状態。別の公開モデルを選びます。 |
| 413 | ファイルサイズと公開プロキシの受信上限。 |
| 422 | ファイル名、ラベル、出力型、二値の値、寸法、座標、引数。 |
| 429 | CVATのユーザー情報APIの要求制限。 |
| 502 | CVATの400または406、あるいはワーカーの読み込み、推論、GPU、メモリ不足。本文で区別します。 |
| 503 | DNS、TLS、接続先、応答時間、使用中の版または全ワーカー。 |
| 504 | 中継から推論ホストへの接続と応答時間。 |

管理サーバーからCVATへの接続先はMR_CVAT_URL、公開HostとスキームはMR_PUBLIC_URLから設定します。CVATの400では公開ホストの許可設定、406では次のAcceptが稼働コードとプロキシで保持されていることを確認します。

```http
Accept: application/vnd.cvat+json
```

モデルの公開、更新、削除、公開版の切り替えではCVATの再起動は不要です。受理済みの登録は画面を閉じても進みます。利用者を無効にしても公開モデルは残るため、利用を止める場合は管理者がモデルを削除します。

## 保存容量と物理削除

公開した全版と試験画像を保持します。既定の上限は50 GiBで、登録前に設定上限まで3 GiB以上、ファイルシステムには4 GiB以上の空きを要求します。これは並列書き込みの厳密な容量保証ではないため、必要なら専用ボリュームやホスト側の容量制限も設定します。

画面の削除は利用停止です。物理削除は削除済みモデルだけを対象にし、復旧に必要なバックアップを先に作成してから実行します。

```bash
./components/extensions/cvatctl config
# configのstate_directoryを指定します。
CVAT_STATE_DIR=/actual/state/directory
./components/extensions/cvatctl down
sudo ./components/extensions/cvatctl --env-file "$(pwd)/.env" \
  --state-dir "$CVAT_STATE_DIR" purge-deleted \
  --model-id ACTUAL_20_HEX_MODEL_ID --confirm
./components/extensions/cvatctl up --no-build
```

所有者と削除の記録は残ります。公開中モデルの旧版だけを物理削除する操作は提供しません。ファイル所有者がrootの場合も、sudoで別の状態記録を参照しないよう、通常運用のstate_directoryを指定します。

## バックアップと復元

新規操作を止めて処理完了後に停止し、同じ時点の.env、MR_HOME/config/、永続data/、ソースのコミット、使用イメージとID、cvatctlの状態記録を保存します。config/にはサービス間トークンと証明書、data/にはSQLiteの関連ファイル、コード、重み、試験画像、公開版とログを含めます。data/run/のソケットとdata/uploads/の一時ファイルは復元対象から外します。

別ホスト構成では、推論ホストのregistry.envとCVAT側に複製したトークンも保存し、推論ホストをregistryctl.py downで停止します。同じデータを新旧の管理サーバーから同時に使用しません。

次は標準保存先の例です。実際の保存先を確認し、リポジトリ外のアクセス制限したバックアップ先を用意します。既存の復元点へ上書きしないでください。

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

復元先は停止し、復元前の状態を別に退避します。元と同じ設定とイメージを準備し、所有者と権限を維持します。保存先変更時はMR_HOMEと明示的なパスを合わせ、本番の外部DB、共有先、公開URLへ誤接続しないことを確認します。

```bash
sudo tar --acls --xattrs --numeric-owner -xzf "$REGISTRY_BACKUP" -C "$REGISTRY_HOME"
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" up --no-build
```

復元後はモデル一覧、作者、権限、代表モデルの試験とCVATからの実行を確認します。このアーカイブにCVATのDBやSAMのRedisは含まれません。全体の復元は[共通のバックアップと復元](../extensions/backup-restore.md)に従います。

サービス間トークンの交換は計画停止中に行い、管理サーバーとCVAT側の秘密ファイルを同じ内容にします。ディレクトリ単位のマウントを維持してファイルを置き換え、再開後に中継を確認します。

## 保存先と起動管理の移行

独立したモデル管理をCVATと同じComposeへまとめる場合は、旧コードと設定のまま両方を停止してバックアップします。旧registry.envのMR_INSTANCE、イメージ、URL、GPU、資源制限、証明書とservice_tokenを記録します。

```bash
OLD_REGISTRY_HOME=/srv/cvat-model-registry
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
python3 components/model_registry/registryctl.py down --home "$OLD_REGISTRY_HOME"
```

保存先も変更する場合は未使用の移行先にコピーします。移行先が既に存在する場合は重ねずに停止して内容を確認します。旧保存先は確認完了まで削除しません。既存の外部保存先を使い続ける場合はコピーせずMR_HOMEへその絶対パスを設定します。

```bash
MR_HOME="$(pwd)/cvat-model-registry"
mkdir -m 700 "$MR_HOME"
printf '*\n' > "$MR_HOME/.gitignore"
sudo rsync -a --exclude '/data/run/' --exclude '/data/uploads/' \
  --exclude '/.gitignore' "$OLD_REGISTRY_HOME/" "$MR_HOME/"
```

同じCVAT設定ファイルへ旧設定を統合し、MR_INSTANCEとトークンは維持、CVAT_MODEL_REGISTRY_URLは空欄にします。MR_CONFIG_DIR、MR_HOST_DATA_DIR、CVAT_MODEL_GATEWAY_SECRETS_DIRは自動設定を使うか移行先に合わせます。旧MR_CVAT_NETWORKはCVAT_NETWORK_NAMEへ統一します。

旧MR_AUTH_MODE、MR_USERS_FILE、MR_SESSION_SECONDSは使用しません。旧モデルの所有者名とCVATのユーザー名を照合します。名前が異なる場合に所有権を自動移行する処理はありませんが、管理者は操作できます。

コードを更新してコミット後に[ビルドと起動](deployment.md#ビルドして起動する)を行います。新しい管理サーバーだけを起動し、一覧、公開版、権限と推論を確認してください。以後、同じホストでregistryctl.py upを追加実行せず、コピーした旧registry.envも稼働設定に使用しません。
