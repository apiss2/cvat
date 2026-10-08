# CVATと拡張機能のバックアップと復元

サーバー全体を復元するには、CVATのデータベース、画像、キー、実行設定と使用したイメージを同じ時点の組として保存します。
タスク単位のexportやバックアップZIPには、全利用者、権限、サーバー設定、外部ストレージが含まれないため、サーバー全体の復元には使用できません。
公式資料も全コンテナの停止後の保存と、保存元と同じCVAT版での復元を案内しています。[公式バックアップガイド](https://docs.cvat.ai/docs/administration/community/advanced/backup_guide/)

## 保存対象を確定する

最初に、実際のコンテナから保存先を特定します。
Composeに記載された論理名とDocker上の実ボリューム名は区別し、名前を既定値から推測しません。
外部DBやバインドマウントを使用している場合は、以下の表を実際の保存先へ置き換えます。

| 保存対象 | 保存する内容と確認先 |
|---|---|
| CVATのPostgreSQL | 利用者、組織、権限、プロジェクト、タスク、ジョブ、注釈、Djangoの移行履歴です。DBコンテナのイメージIDと実マウントを保存します。 |
| CVATのデータ | アップロードした画像や動画、生成したフレーム、タスクのファイルです。`/home/django/data`の実マウントを確認します。 |
| CVATのキー | `/home/django/keys`と設定で上書きした秘密情報です。新しいキーを生成して代用しません。 |
| RedisとKvrocks | 要求の状態、キュー、永続キャッシュです。`cvat_inmem_db`と`cvat_cache_db`に対応する実領域を確認します。 |
| イベントとログ | ClickHouseのデータ、CVATのログ、運用上必要な外部ログです。 |
| 共有データと外部ストレージ | 共有フォルダー、NFS、S3互換ストレージ、別管理のDB、証明書と接続設定です。各保存先のバックアップ方式を確認します。 |
| SAM2 | 専用Redisのデータ、関数の設定、モデルの重み、関数イメージと再デプロイに必要なソースです。 |
| UltraSAM | 関数の設定、重みを含む実関数イメージ、重みのSHA-256と再デプロイに必要なソースです。独立した追跡状態やRedisの保存領域はありません。 |
| Nuclio | dashboardの設定、全関数の定義、関数イメージ、ローカル保存領域、ネットワークと再起動設定です。 |
| ONNXモデル管理 | 共通の`.env`、`MR_HOME/config/`と`MR_HOME/data/`の永続データです。モデル、試験画像、SQLiteと関連ファイル、作者の記録、公開版、ログを含みます。 |
| 実行構成 | Gitコミット、使用した全Composeと設定ファイル、イメージID、Dockerの版、GPUの情報、`cvatctl`の状態記録です。 |

キャッシュを保存しない場合は、対象CVAT版で再生成できることを復元試験で確認します。
キューや処理状態までキャッシュとして一律に省略しません。
SAM2の途中追跡状態を復元しても、モデルや状態形式が変わる場合は保存済みのCVAT注釈から追跡を開始し直します。
UltraSAMで確定して保存した注釈はCVATのデータベースに含まれます。
関数のメモリ上の画像特徴は再計算できるため、復元対象へ含めません。

## 稼働中の構成を記録する

保存先はリポジトリとDockerのビルド対象の外へ置き、更新ごとに新しい名前を使用します。
過去の復元点へ保存物を上書きしません。
次の値を実環境へ置き換え、操作対象のコンテナを確認します。
この例はCVAT用Composeプロジェクトのコンテナを選び、同じ構成のモデル管理サーバーも含みます。
Nuclio関数と動的に作成されたONNX推論ワーカーは追加で記録し、別の推論ホストを使う場合はその構成も保存します。

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

続いて、起動時に使用した全Compose、`.env`、別ファイルの秘密情報と証明書を保存します。
Composeの起動元と追加ファイルは、コンテナの`com.docker.compose.project.working_dir`と`com.docker.compose.project.config_files`ラベルでも確認できます。
シェルやsystemdから渡している設定値も記録します。
解決済みの`docker compose config`には秘密情報が含まれることがあるため、一般公開の作業ログへ貼り付けません。

```bash
# 実際に使用している全-f指定と--env-fileをこの配列へ入れます。
CVAT_COMPOSE=(docker compose --project-directory /srv/cvat \
  --env-file /srv/cvat-config/cvat.env -p "$CVAT_PROJECT" \
  -f /srv/cvat/docker-compose.yml \
  -f /srv/cvat-config/site.compose.yml)
"${CVAT_COMPOSE[@]}" config --format json > "$CVAT_BACKUP/compose.json"
"${CVAT_COMPOSE[@]}" config --services > "$CVAT_BACKUP/services.txt"
"${CVAT_COMPOSE[@]}" config --images > "$CVAT_BACKUP/image-references.txt"
```

イメージはタグだけでなく、稼働中コンテナが使用する実イメージIDを保存します。
タグは別の内容を指すことがあるため、必要なイメージを`docker image save`または社内レジストリで保持します。
旧CVAT、全worker、UI、DB、Redis、Kvrocks、ClickHouse、Nuclioと関数、モデル管理と推論ワーカーを対象にします。

Nuclio関数は`nuclio.io/project-name`と`nuclio.io/namespace`、接続ネットワークを照合して選びます。
関数の`docker inspect`、デプロイ用ソース、使用したモデル、イメージ、稼働状態と再起動設定を保存します。
UltraSAMを使用している場合は`pth-ultrasam-interactor`の関数イメージも含め、[重みと実行環境の記録](../ultrasam/README.md#重みと実行環境の記録)に従って内容を記録します。
ローカル方式のNuclioで使用する`nuclio-local-storage`などの保存領域も実環境から特定します。
同じDockerデーモンの別用途の関数を保存または停止対象へ無条件に含めません。

## 書き込みを止めて論理バックアップを取得する

バックアップ前に利用者の接続を保守用のアクセス制限で止め、編集中の注釈を保存し、import、export、自動注釈、追跡、モデル登録と更新を完了させます。
外部の自動処理、Webhookから起動する処理、共有データへ書き込む別サーバーも止めます。
処理が完了しない場合は中止または継続の方針を決め、処理中の状態を残したまま移行成功として扱いません。

PostgreSQLの論理バックアップを取得するときは、DBを書き換えるCVATのサーバーと全workerを停止し、DBだけは動作させます。
サービスの一覧は保存済みのComposeと照合します。
次はコンテナ内の`POSTGRES_USER`がバックアップ可能な管理用ユーザーである場合の例です。
外部DBを使用している場合は、その環境の認証方式で同等の保存を行います。

```bash
# CVATのサーバー、全worker、外部の書き込み処理を止めた後に実行します。
# cvat_dbという名前を変更している環境では実名を指定します。
docker exec cvat_db sh -c 'exec pg_dumpall -U "$POSTGRES_USER"' \
  > "$CVAT_BACKUP/postgres-all.sql"
test -s "$CVAT_BACKUP/postgres-all.sql"
```

コマンドの終了コードとファイルのサイズを確認します。
SQLダンプには認証情報も含まれるため、バックアップと同じアクセス制御を適用します。
論理バックアップはDBメジャー版の移行や調査に使用し、同時点の物理バックアップも保持します。

## 全サービスを停止する

同じComposeへ拡張を導入した環境では、使用中の設定でCVAT、モデル管理サーバー、その推論ワーカーをまとめて停止します。
管理コマンドの稼働記録がない既存環境では、使用中の全Composeを指定して停止します。

```bash
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
```

モデル管理を独立したComposeで運用している環境では、現在の`registryctl.py down`も実行します。
この停止はソースと設定を更新する前に行います。
別ホストの管理サーバーも、そのホストで停止します。

旧構成にNuclioがある場合はdashboardと確認済みの全関数を先に停止します。
関数の元の再起動設定と稼働状態を記録してから、一時的に再起動を抑止します。
その後、旧構成の全Composeを使って`down --timeout 120`を実行します。
対象外のコンテナ停止、ボリューム削除、`--remove-orphans`は行いません。

ネットワークに停止済み関数が残ると、Composeのネットワーク削除だけが失敗することがあります。
この場合は各対象コンテナの停止を確認し、ネットワークを消す目的で関数やデータを削除しません。
保存作業の直前に、対象領域を使用する稼働中コンテナがないことを確認します。

## 停止した永続データを保存する

次は名前付きボリューム1個の保存例です。
同じ方法で実際の保存対象をすべて保存します。
保存用イメージは保守作業前に取得し、使用したイメージIDを記録します。

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
  "$CVAT_BACKUP_IMAGE" tar --numeric-owner --xattrs --acls \
  -cpf "/backup/$CVAT_ARCHIVE" -C /source .
sha256sum "$CVAT_BACKUP/$CVAT_ARCHIVE" > "$CVAT_BACKUP/$CVAT_ARCHIVE.sha256"
```

`docker volume inspect`が失敗した場合は次へ進みません。
名前を間違えた状態でマウントすると、Dockerが別の空ボリュームを作ることがあるためです。
保存したtarの内容一覧と終了コードを確認し、全対象のチェックサムを保存します。

バインドマウントはホスト側のディレクトリを、所有者、権限、拡張属性とリンクを保持して保存します。
ONNXモデル管理の保存先を標準構成で使用する場合は、管理サーバー停止後に次を実行します。
既定の保存先はリポジトリ直下の`cvat-model-registry/`です。
`MR_HOME`や個々のマウント元を変更した環境では、その実パスを使用します。
`data/run/`の一時ソケットと`data/uploads/`の一時ファイルは保存対象から除きます。

```bash
REGISTRY_HOME="$(pwd)/cvat-model-registry"
sudo tar --numeric-owner --xattrs --acls \
  --exclude='data/run' --exclude='data/uploads' \
  -cpf "$CVAT_BACKUP/model-registry.tar" \
  -C "$REGISTRY_HOME" .gitignore config data
sudo chmod 600 "$CVAT_BACKUP/model-registry.tar"
```

SQLiteのファイルだけを稼働中にコピーしません。
管理サーバー停止後にSQLiteの関連ファイルと公開版が指すモデルファイルをまとめて保存します。
共通の`.env`と`cvatctl`の状態記録は実行構成の保存に含め、別ホストで使用する`registry.env`も保存します。
外部DB、共有ストレージやオブジェクトストレージは、それぞれの管理方式で同じ保守時間帯の復元可能な状態を保存します。

## 別サーバーで元の版へ復元する

復元試験では、本番と別のDockerデーモンを使います。
保存元と同じCVAT版、同じDBメジャー版、保存した実イメージと設定を用意し、新しい空の領域へ復元します。
最初から新しいCVATを起動するとデータ移行が始まるため、バックアップ自体が元の環境を復元できるか確認できません。

次は新しい名前のボリュームへ展開する例です。
既存名だった場合は別名を選び、既存領域へ重ねて展開しません。

```bash
CVAT_RESTORE_VOLUME=cvat_restore_db_20260916
# このinspectが成功する場合は、その名前を使わず別の未使用名を選びます。
docker volume inspect "$CVAT_RESTORE_VOLUME"
# 未使用名であることを確認した後に実行します。
docker volume create "$CVAT_RESTORE_VOLUME"
docker run --rm --network none --read-only \
  --mount "type=volume,src=$CVAT_RESTORE_VOLUME,dst=/restore" \
  --mount "type=bind,src=$CVAT_BACKUP,dst=/backup,readonly" \
  "$CVAT_BACKUP_IMAGE" tar --numeric-owner --xattrs --acls \
  -xpf "/backup/$CVAT_ARCHIVE" -C /restore
```

復元先Composeは各論理ボリュームを復元先の実名へ明示的に対応付けます。
以下はDBだけの例です。
同じ方法で画像、キー、Redis、Kvrocks、イベントなどを対応付け、解決済みComposeを確認してから起動します。

```yaml
volumes:
  cvat_db:
    external: true
    name: cvat_restore_db_20260916
```

別パスへ復元したバインドマウントは、設定中のホスト絶対パスも変更します。
Nuclio関数とモデル管理ワーカーへ渡すパスも確認します。
モデル管理では`MR_INSTANCE`とサービス間トークンを保持し、`MR_HOME`から決まる`config/`と`data/`を復元先へ一致させます。
元のリポジトリ直下を使う場合も、Gitのチェックアウトだけではモデルデータは復元されません。
`cvatctl`の旧状態記録は保存資料として保持し、異なるDockerデーモンでそのまま停止操作に使用しません。

復元後はユーザーと権限、プロジェクト、タスク、ジョブ、代表的な画像と動画、注釈、外部ストレージを確認します。
確認用の少数の画面表示だけでなく、件数とデータの参照先も元の記録と照合します。
UltraSAMを使用している場合は、保存した実イメージで関数を起動し、正負のポイントからの分割と既存注釈の再読込を確認します。
この試験に成功したバックアップを、本番の復元点として採用します。

## PostgreSQLのメジャー版が異なる場合

PostgreSQLのメジャー版が異なる場合は、旧版の物理データを新しいDBイメージへ直接接続しません。
旧DBを同じ版で起動して論理バックアップを取得し、別の空領域の新DBへ復元してからCVATを起動します。
既存のロールと初期DBの重複をどう扱うかは、ダンプ内容に合わせて復元試験で確定します。[公式DB更新手順](https://docs.cvat.ai/docs/administration/community/advanced/upgrade_guide/#how-to-upgrade-postgresql-database-base-image)

独自のDB設定がある場合だけ、その差分に応じたDB移行工程を追加します。
元のボリュームを先に削除する必要はありません。

## 更新に失敗した場合の復旧

データ移行が開始された後は、新しいデータに旧イメージだけを接続しません。
新しいCVAT、モデル管理と関数を停止し、失敗時のログとデータを調査用に保持します。
そのうえで、旧イメージ、旧設定と同じ時点の全データを新しい空の復元先へ戻します。

復旧時は、旧チェックアウトの全Composeに復元先の実ボリューム名とバインド先を設定します。
バックアップ取得時のイメージを起動します。
外側のアクセス制限を維持して基本機能を確認し、記録したNuclio関数の再起動設定と必要な稼働状態を戻してから利用を再開します。
新しく作成したONNXモデル管理サーバーは、復旧対象に含める判断が済むまで停止状態で保持します。

利用再開後に更新後の環境へ注釈を保存した場合は、移行直前の復元点へ戻すとその変更が失われます。
失敗環境を保存し、復元で失う変更の範囲と回収方法を確定してから復旧します。
DBの逆方向移行や、新旧DB間での注釈の自動統合はこの管理構成では実施しません。
