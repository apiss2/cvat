# モデル管理サービスの保守

[モデル管理の案内](README.md) / [初回配備](deployment.md) / [検証](testing.md)

このページでは、稼働中のモデル管理サービスの更新、資源の調整、障害調査、データの保存と復旧を説明します。モデルの登録や部分更新など、利用者の操作は[利用手順](usage.md)を参照してください。

以下のコマンド例は、モデル管理とCVATを同じDockerホストで管理する構成です。共通管理コマンド `cvatctl` による停止はCVAT全体とモデルの実行ワーカーも止めるため、保守時間に行います。別の推論ホストでは、そのホストの `registry.env` と `registryctl.py` を使用します。

## 停止とイメージの更新

停止する場合は、新規操作を止め、登録、更新、推論が完了してから現在のコードと設定で `cvatctl down` を実行します。モデルデータは保持されます。同じ構成を再開する場合は `up --no-build` で既存のイメージを使用します。

```bash
./components/extensions/cvatctl down
./components/extensions/cvatctl up --no-build
```

コードを更新する場合は、[共通の更新手順](../extensions/update.md)に従ってコミットを確定してからビルドします。管理画面のHTML、CSS、JavaScriptも管理サービスのイメージに含まれるため、ファイルの配置だけでは画面へ反映されません。モデルの入力や出力の仕様を変える場合は、対応するONNXワーカーも更新します。

更新後も古い画面が残る場合は、実行中のイメージ、プロキシの転送先、ブラウザーのキャッシュを確認します。`/model-registry/static/app.js` や `style.css` にCVAT本体のHTMLが返る場合は転送先が誤っています。画面の更新にモデル保存先の再初期化は不要です。

## 実行資源と同時実行

資源の割り当ては、停止してから同じ `.env` の任意設定を変更し、再開して反映します。設定しなければ次の既定値を使用します。ここでのワーカーは、モデルの特定の版を読み込んで推論するコンテナです。

| 設定 | 既定値 | 内容と範囲 |
| --- | --- | --- |
| `MR_MAX_LOADED` | `2` | 同時に読み込むモデルの版数です。1から32まで指定できます。 |
| `MR_WORKER_MEMORY_MIB` | `4096` | ワーカー1個のホストメモリー上限です。単位はMiB、最小値は256です。GPUメモリーの上限ではありません。 |
| `MR_WORKER_CPUS` | `2` | ワーカー1個のCPU割当数です。1から128までの整数を指定できます。 |
| `MR_STORAGE_GIB` | `50` | モデル保存容量の上限です。単位はGiB、最小値は4です。登録時の空き容量条件は後述します。 |
| `MR_GPU_DEVICE` | 空欄 | 空欄ならCPUです。GPUを使う場合は番号またはUUIDを1個指定します。 |

推論は同じ版につき1回ずつ実行します。使用中の版への追加要求や全ワーカーが使用中の場合は、待ち行列に保持せず503を返すため、処理完了後に再試行します。モデルの読み込み待機と推論通信のタイムアウトはそれぞれ90秒です。複数GPUへの自動分散は行いません。

GPUを使う場合は、対応ドライバーとNVIDIA Container Toolkit、およびGPU用ワーカーイメージを用意します。GPUの指定に `all` は使用できません。GPUの割り当ては管理者設定であり、登録モデルから変更できません。

## ログとエラーを確認する

処理の失敗を調べる場合は、所有者または管理者としてモデルの詳細画面を開き、要求ID、版、処理段階、例外と標準出力を確認します。標準のログに画像本体は保存しませんが、モデルコードが内容を出力すると記録されます。

保存するイベントは全モデルを通して直近1万件です。モデル別のログAPIは、1回の応答で最大200件を返します。標準出力の取得範囲が重なると、隣接する要求のログに同じ行が現れる場合があります。

```bash
./components/extensions/cvatctl status
# cvatは実際のComposeプロジェクト名へ置き換えます。
docker ps --filter label=com.docker.compose.project=cvat \
  --filter label=com.docker.compose.service=model-registry
# 一覧で確認したコンテナ名またはIDを指定します。
docker logs --tail 200 ACTUAL_REGISTRY_CONTAINER
```

CVATからの推論要求を転送する中継のログは、サービス名を `model-gateway` に変えて同様に確認します。

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

CVATへの認証確認で失敗する場合は、接続先の `MR_CVAT_URL` と、公開ホスト名とスキームを決める `MR_PUBLIC_URL` を確認します。CVATが400を返す場合は公開ホストの許可設定、406を返す場合は次の `Accept` ヘッダーが稼働コードとプロキシで保持されていることを確認します。

```http
Accept: application/vnd.cvat+json
```

モデルの公開、更新、削除、公開版の切り替えではCVATの再起動は不要です。受理済みの登録は画面を閉じても進みます。利用者を無効にしても公開モデルは残るため、利用を止める場合は管理者がモデルを削除します。

## 保存容量と物理削除

削除していないモデルは公開した全版と試験画像を保持します。既定の上限は50 GiBで、登録前に設定上限まで3 GiB以上、ファイルシステムには4 GiB以上の空きを要求します。これは並列書き込みの厳密な容量保証ではないため、必要なら専用ボリュームやホスト側の容量制限も設定します。

モデルを削除すると、全ファイル、版と処理の詳細記録を消去します。削除と清掃のログ、および所有者や削除状態などの最小限の記録だけを残します。削除済みモデルは管理者と所有者の一覧にも表示しません。受け付け済みの推論、更新のコピーと検証、実行待ちの登録の終了後にワーカーを停止して清掃するため、削除の応答まで時間がかかる場合があります。

削除要求に対する503は、モデルの利用を止めた後のファイル清掃の失敗を示します。ログを確認して障害を解消し、同じモデルの削除を再試行してください。管理サービスも起動時に清掃を再試行するため、過去に削除済みとして残っていたファイルもこのとき消去します。清掃に追加の機能選択や設定は不要です。

清掃の失敗が続く場合は、管理サービスとワーカーを停止した状態で `purge-deleted` を実行します。このコマンドは削除済みモデルのファイルを清掃する復旧用の操作です。別ホストの `registryctl.py purge-deleted` も、管理コンテナと同じインスタンスの推論ワーカーが停止していることを確認します。どちらかが稼働中、またはDockerから稼働状態を取得できない場合は清掃を開始しません。

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

同じホストのCVAT全体を保存する場合は、[増分バックアップ](../extensions/incremental-backup.md)を使用できます。以下は、モデル管理のデータを個別に保存する手順です。

保存する場合は新規操作を止めて処理完了後に停止し、同じ時点の `.env`、`MR_HOME/config/`、永続データの `data/`、ソースのコミット、使用イメージとID、`cvatctl` の状態記録を揃えます。`config/` にはサービス間トークンと証明書、`data/` にはSQLiteの関連ファイル、モデルのコード、重み、試験画像、公開版とログを含めます。`data/run/` のソケットと `data/uploads/` の一時ファイルは復元対象から外します。

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

移行後の認証にはCVATのログイン状態を使用します。`MR_AUTH_MODE` を明示する場合の値は `cvat` です。旧設定の `MR_USERS_FILE` と `MR_SESSION_SECONDS` は使用しません。既存モデルの所有者名とCVATのユーザー名を照合してください。名前が異なる場合に所有権を自動移行する処理はありませんが、管理者は操作できます。

コードを更新してコミット後に[ビルドと起動](deployment.md#ビルドして起動する)を行います。新しい管理サーバーだけを起動し、一覧、公開版、権限と推論を確認してください。以後、同じホストでregistryctl.py upを追加実行せず、コピーした旧registry.envも稼働設定に使用しません。

## 既存モデルの実装を確認する

入力と出力の仕様を変更する更新では、[モデルの実装仕様](model-development.md)と登録済みモデルを照合します。既存の `Mask` 出力と `mask` ラベルには互換処理がありますが、`PredictParams` に `threshold` はありません。`params.threshold` を参照するモデルは、必要な判定を `model.py` 内へ移し、試験した版を登録してください。
