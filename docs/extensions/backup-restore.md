# CVATと拡張機能のバックアップと復元

[共通運用](README.md) / [更新手順](update.md)

サーバー全体の復元には、データ、キー、設定と実イメージを同じ時点の組として保存します。タスクのexportやバックアップZIPだけでは全利用者、権限、サーバー設定、外部ストレージを復元できません。対象版の[公式バックアップガイド](https://docs.cvat.ai/docs/administration/community/advanced/backup_guide/)も確認してください。

## 保存対象を確定する

稼働コンテナの実マウントから保存先を特定します。Composeの論理名をDocker上の実名と同じと仮定しません。外部DBとバインドマウントも実際の保存先で管理します。

| 対象 | 保存内容 |
|---|---|
| PostgreSQL | 利用者、組織、権限、プロジェクト、タスク、ジョブ、アノテーションと移行履歴。 |
| CVATのデータとキー | /home/django/data、/home/django/keysの実領域と設定で追加した秘密情報。 |
| RedisとKvrocks | キュー、要求状態、永続キャッシュ。再生成を確認せず一律に省略しません。 |
| イベントとログ | ClickHouse、CVATログと運用上必要な外部ログ。 |
| 外部ストレージ | 共有フォルダー、NFS、オブジェクトストレージ、外部DB、証明書と接続設定。 |
| SAM2とSAM3.1 | それぞれ独立したRedis、パスワード、関数定義、重み、実イメージとソース。 |
| UltraSAM | 関数定義、重みを含む実イメージ、重みのSHA-256とソース。専用Redisはありません。 |
| Nuclio | dashboardと全対象関数の定義、実イメージ、ローカル保存領域、ネットワークと再起動設定。 |
| ONNXモデル管理 | .env、MR_HOME/config/と永続data/。SQLiteの関連ファイル、コード、重み、試験画像、作者、公開版とログ。 |
| 実行構成 | Gitコミット、全Composeと設定、イメージID、DockerとGPUの情報、cvatctlの状態記録。 |

SAM31_REDIS_VOLUMEとSAM2_REDIS_VOLUMEは別に保存します。Redisの推論状態は未保存のアノテーションや閉じたブラウザーの再開を保証しません。モデルや状態形式が変わる場合はCVATの保存済みアノテーションから追跡を開始します。再計算できる画像特徴は保存対象に含めません。

## 稼働中の構成を記録する

バックアップはリポジトリとビルド対象の外へ置き、アクセスを制限した新しい復元点を使用します。既存の復元点を上書きしません。コンテナ名やパスは実環境へ置き換えてください。

```bash
CVAT_PROJECT=cvat
CVAT_BACKUP=/secure/backup/cvat-before-update
umask 077
mkdir -p "$CVAT_BACKUP"
git rev-parse HEAD > "$CVAT_BACKUP/git-commit.txt"
git remote -v > "$CVAT_BACKUP/git-remotes.txt"
git status --porcelain > "$CVAT_BACKUP/git-status.txt"
docker version > "$CVAT_BACKUP/docker-version.txt"
docker compose version > "$CVAT_BACKUP/compose-version.txt"
mapfile -t CVAT_CONTAINERS < <(docker ps -aq --filter "label=com.docker.compose.project=$CVAT_PROJECT")
test "${#CVAT_CONTAINERS[@]}" -gt 0
docker inspect "${CVAT_CONTAINERS[@]}" > "$CVAT_BACKUP/containers.json"
```

全Compose、.env、追加秘密ファイルと証明書を保存します。起動元とCompose一覧はコンテナのworking_dirとconfig_filesラベルでも照合します。シェルやsystemdから渡す値も記録し、秘密情報を含む解決済み設定を公開ログへ貼りません。

```bash
# 実際の全-f指定と--env-fileを記載します。
CVAT_COMPOSE=(docker compose --project-directory /srv/cvat \
  --env-file /srv/cvat-config/cvat.env -p "$CVAT_PROJECT" \
  -f /srv/cvat/docker-compose.yml -f /srv/cvat-config/site.compose.yml)
"${CVAT_COMPOSE[@]}" config --format json > "$CVAT_BACKUP/compose.json"
"${CVAT_COMPOSE[@]}" config --services > "$CVAT_BACKUP/services.txt"
"${CVAT_COMPOSE[@]}" config --images > "$CVAT_BACKUP/image-references.txt"
```

動的なONNXワーカーとNuclio関数はComposeの一覧だけで網羅できません。関数のプロジェクト、名前空間とネットワーク、ワーカーの管理インスタンスを照合して追加記録します。別用途の関数を無条件に停止対象にしません。

全対象の実イメージIDを保存し、[イメージの保管と照合](update.md#検証したイメージを保存する)に従います。Nuclioのローカル保存領域、元の再起動設定、モデルの重みも含めます。UltraSAMの記録は[運用手順](../ultrasam/operations.md#ビルドと実行環境の記録)を参照します。

## 書き込みを止めて論理バックアップを取得する

利用者の接続を保守用に制限し、編集を保存して、import、export、自動アノテーション、追跡、モデル登録と更新を完了させます。外部の自動処理、Webhook、共有先へ書く別サーバーも停止します。未完了の処理をそのまま移行成功と扱いません。

PostgreSQLの論理バックアップでは、書き込みを行うCVATサーバーと全ワーカーを停止し、DBだけは稼働させます。コンテナのPOSTGRES_USERがバックアップ権限を持つ場合の例です。

```bash
# DBへ書き込む処理をすべて停止した後に実行します。
docker exec cvat_db sh -c 'exec pg_dumpall -U "$POSTGRES_USER"' > "$CVAT_BACKUP/postgres-all.sql"
test -s "$CVAT_BACKUP/postgres-all.sql"
```

終了コードとファイルを確認します。SQLには認証情報も含まれるため保護します。論理バックアップに加え、同時点の物理バックアップも保持します。外部DBでは対応する認証と保存手順を使用します。

## 全サービスを停止する

ソースと設定を更新する前に、旧構成で停止します。

```bash
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
```

同じComposeのモデル管理と自身のワーカーも対象です。独立した管理サービスと別ホストは、そのホストのregistryctl.py downでも停止します。稼働記録のない既存環境では実際の全Composeを指定して停止します。

旧Nuclioの手動停止では、元の設定を記録して確認済みの関数とdashboardの再起動を一時的に抑止し、全Composeでdown --timeout 120を行います。対象外の停止、ボリューム削除、remove-orphansを行いません。停止済み関数がネットワークに残る場合も、ネットワークを消すために関数やデータを削除しません。

保存の直前に、対象領域を使用する稼働コンテナがないことを確認します。

## 停止した永続データを保存する

名前付きボリューム1個の例です。同じ方法で実際の全対象を保存します。保守前に保存用イメージを取得し、そのIDを記録します。

```bash
CVAT_VOLUME=ACTUAL_VOLUME_NAME
CVAT_ARCHIVE=ACTUAL_VOLUME_NAME.tar
CVAT_BACKUP_IMAGE=ubuntu:24.04
docker image inspect "$CVAT_BACKUP_IMAGE" > "$CVAT_BACKUP/backup-image.json"
docker volume inspect "$CVAT_VOLUME" > "$CVAT_BACKUP/$CVAT_VOLUME.volume.json"
test -z "$(docker ps -q --filter "volume=$CVAT_VOLUME")"
docker run --rm --network none --read-only \
  --mount "type=volume,src=$CVAT_VOLUME,dst=/source,readonly" \
  --mount "type=bind,src=$CVAT_BACKUP,dst=/backup" \
  "$CVAT_BACKUP_IMAGE" tar --numeric-owner --xattrs --acls -cpf "/backup/$CVAT_ARCHIVE" -C /source .
sha256sum "$CVAT_BACKUP/$CVAT_ARCHIVE" > "$CVAT_BACKUP/$CVAT_ARCHIVE.sha256"
```

volume inspectに失敗した場合は進めません。誤った名前でマウントすると空のボリュームを新規作成する場合があるためです。tarの内容一覧、終了コードとチェックサムを確認します。

バインドマウントも所有者、権限、拡張属性、リンクを維持します。標準のモデル管理保存先では、停止後に次を実行します。

```bash
REGISTRY_HOME="$(pwd)/cvat-model-registry"
sudo tar --numeric-owner --xattrs --acls \
  --exclude='data/run' --exclude='data/uploads' \
  -cpf "$CVAT_BACKUP/model-registry.tar" -C "$REGISTRY_HOME" .gitignore config data
sudo chmod 600 "$CVAT_BACKUP/model-registry.tar"
```

稼働中のSQLite単体をコピーせず、関連ファイルと公開版のモデルを一緒に保存します。共通設定、状態記録、別ホストのregistry.envとトークンも保存します。外部DBとストレージは各管理方式で同じ保守時間帯の復元点を確保します。

## 別サーバーで元の版へ復元する

本番とは別のDockerデーモンに、元と同じCVAT版、DBメジャー版、実イメージと設定を用意します。最初から新しいCVATでDB移行を始めません。チェックサムを検証し、停止した空の領域へ復元します。

```bash
CVAT_RESTORE_VOLUME=cvat_restore_db_unique
# このinspectが成功した場合は中止し、別の未使用名を選びます。
docker volume inspect "$CVAT_RESTORE_VOLUME"
# 未使用であることを確認した後に実行します。
docker volume create "$CVAT_RESTORE_VOLUME"
docker run --rm --network none --read-only \
  --mount "type=volume,src=$CVAT_RESTORE_VOLUME,dst=/restore" \
  --mount "type=bind,src=$CVAT_BACKUP,dst=/backup,readonly" \
  "$CVAT_BACKUP_IMAGE" tar --numeric-owner --xattrs --acls -xpf "/backup/$CVAT_ARCHIVE" -C /restore
```

復元先Composeの論理ボリュームを実名へ対応付けます。DBだけでなく画像、キー、Redis、Kvrocks、イベントにも同様に設定します。

```yaml
volumes:
  cvat_db:
    external: true
    name: cvat_restore_db_unique
```

バインド先の絶対パス、関数とワーカーが使用するパスも合わせます。モデル管理のMR_INSTANCEとトークンを保持し、MR_HOME配下のconfig/とdata/を復元します。Gitのチェックアウトだけでモデルデータは復元されません。別Dockerデーモンでは旧cvatctl状態記録を停止操作にそのまま使用しません。

復元後は利用者、権限、データの件数と参照先、画像と動画、アノテーション、外部ストレージを元の記録と照合します。有効な全拡張で実操作と保存を確認し、合格した復元点を採用します。

## PostgreSQLのメジャー版が異なる場合

旧版の物理データを新しいDBイメージへ直接接続しません。旧DBを同じ版で起動して論理バックアップを取得し、新しい空領域のDBへ復元します。初期ロールやDBの重複はダンプに合わせた復元試験で処理します。[公式DB更新手順](https://docs.cvat.ai/docs/administration/community/advanced/upgrade_guide/#how-to-upgrade-postgresql-database-base-image)も確認してください。元ボリュームを先に削除しません。

## 更新に失敗した場合の復旧

データ移行後に新しいデータへ旧イメージだけを接続しません。失敗した構成を停止してログとデータを保持し、旧イメージ、旧設定、同時点の全データを新しい空領域へ復元します。

アクセス制限を維持し、元のComposeと復元先の対応を確認します。Nuclio関数の再起動設定と稼働状態を戻し、全機能を確認してから公開します。復旧対象に含める判断が済んでいない新しいサービスは停止状態を維持します。

更新後に保存したアノテーションは、更新前の復元点へ戻すと失われます。失敗環境を保持し、失う変更と回収方法を決めてから復旧します。DBの逆方向移行や新旧データの自動統合は提供しません。
