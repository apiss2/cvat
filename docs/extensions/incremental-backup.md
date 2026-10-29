# 更新前の増分バックアップ

[共通運用](README.md) / [手動のバックアップと復元](backup-restore.md)

このページでは、CVATを更新する前にデータを保存し、必要な世代を取り出す手順を説明します。**増分バックアップ** は、初回に全内容を保存し、次回から変更のないファイルの再読み込みと同一内容の再保存を省く方式です。

共通管理コマンドの `cvatctl backup` は、停止したCVATのデータを暗号化して保存します。保存処理にはバックアップ用ソフトウェアresticを使用します。「増分バックアップ」の各世代は独立した復元点として選べるため、差分ファイルを順番に適用する必要はありません。

この機能は、データベースを停止し、そのファイルを保存する物理バックアップです。PostgreSQLのメジャー版を変える場合は、[別途の移行手順](backup-restore.md#postgresqlのメジャー版が異なる場合)を使用します。元の版へ復旧できるよう、[検証した実イメージの保管](update.md#検証したイメージを保存する)も継続してください。

## 初回の設定

LinuxのローカルDocker環境で実行します。既存のcvatctlと同じPython環境、Dockerの実行権限を使用します。resticは固定バージョンの`restic/restic:0.19.1`コンテナで実行するため、ホストへのインストールは不要です。イメージがない場合だけ取得します。

```bash
CVATCTL=./components/extensions/cvatctl
"$CVATCTL" backup init --repository /mnt/backup/cvat
"$CVATCTL" backup plan
```

保存先には、リポジトリとcvatctlの状態ディレクトリの外にある、アクセス権限700の専用ディレクトリを指定します。新規ディレクトリは作成します。既存の非公開設定やバックアップは上書きしません。カンマや改行を含むパスには対応しません。

初期化は暗号化用のパスワードファイルを生成し、その場所を表示します。パスワードファイルは権限600で保存し、バックアップには含めません。このファイルをなくすと復元できません。別の安全な場所にも保存してください。バックアップ用ディスクとパスワードファイルを同時に失わない配置にします。

保存先とパスワードファイルの場所は、既存のcvatctl状態ディレクトリ内の`backup.json`に記録します。例は次のとおりです。通常は手動編集しません。

```json
{
  "version": 1,
  "repository": "/mnt/backup/cvat",
  "password_file": "/home/operator/.local/state/cvat-extensions/ENV_HASH/backup-password"
}
```

バックアップの設定は `.env` や機能選択から独立しています。常駐サービスとGPUは不要です。保存対象は `backup plan`、保存済みデータの整合性は `backup check` で確認します。

## 更新前に取得する

利用者の編集を保存し、import、export、自動アノテーション、追跡、モデル登録が完了してから実行します。共有先に書き込む外部処理も停止します。バックアップはソース、設定、保存先を変更する前に取得してください。

```bash
# 認証と保存対象を確認してから、既存のdown処理で停止し、増分を保存します。
"$CVATCTL" backup create --stop

# バックアップが成功した後に更新します。
git pull --ff-only
"$CVATCTL" up
```

`--stop`を指定すると、成功時も失敗時もCVATは自動再起動しません。更新前の停止状態を維持します。失敗や中断の後は`cvatctl status`で確認し、原因を解消してバックアップを再実行するか、元のコードと設定で`cvatctl up`を実行してください。保存先の認証やマウントの事前確認で失敗した場合は、停止処理へ進みません。

既に`cvatctl down`を実行した場合は、次のコマンドで保存します。稼働中のCVATや、対象領域へ書き込める別コンテナを検出すると中止します。

```bash
"$CVATCTL" backup create
```

既存の`--env-file`と`--state-dir`を使用する環境では、毎回同じ指定を付けます。バックアップと他のcvatctl操作は同じ`operation.lock`で排他制御します。

```bash
"$CVATCTL" --env-file /secure/cvat.env --state-dir /secure/cvat-state backup create --stop
```

## 保存対象と対象外

保存対象は、記録済みの`active.compose.json`にある名前付きボリュームとバインドマウントです。Composeの論理名ではなく、記録されたDocker上の実名を使用し、存在を確認します。既存コンテナについて、サービスごとの保存元とマウント先を照合し、追加、欠落、組み合わせの変更があれば停止処理の前に中止します。設定を編集して保存先を変えた後に、別の空ボリュームをバックアップすることを避けます。

標準構成では、PostgreSQL、CVATの画像と動画、アノテーション、キー、ログ、Redis、Kvrocks、ClickHouse、および有効なSAM系Redisを含みます。Nuclio関数とモデルワーカーの追加マウント、記録済みの重みの保存先、ローカルモデル管理の`MR_HOME`全体も対象にします。ネストしたバインド保存先は重複して取り込みません。

DICOM取り込みで保存した画像と動画、`DicomTag`、タスク用とプロジェクト用の`AnnotationGuide`も、標準のデータ領域とDBに含まれます。これらの機能を設定から無効にしても、記録済みの保存対象は除外しません。

復元用の情報には、解決済みCompose、cvatctlの稼働記録、Composeコンテナの情報、設定ファイル、Gitコミット、変更差分、元の保存先と復元先の対応表を含めます。Gitコミットと設定ファイルはバックアップ実行時のチェックアウトと設定を記録します。先にソースや設定を変更した場合、それだけで旧稼働版を表すとは限らないため、稼働記録のイメージIDと解決済みComposeを照合してください。これらには秘密情報が含まれますが、restic内では暗号化して保存します。

Dockerイメージの実体、未追跡のソースファイル、オブジェクトストレージなどの外部データは自動保存しません。Unixソケットとtmpfsも対象外です。外部DB、別ホストのモデル管理、外部Compose secrets/configs、下記の診断用ログを除く匿名ボリューム、非標準のDockerボリュームドライバーは自動処理せず、中止します。外部表領域などを追加した独自DB構成は、この手順の対象外です。[手動のバックアップと復元](backup-restore.md)に従って別途保存してください。

例外として、ClickHouseのイメージが宣言した診断用ログ `/var/log/clickhouse-server` にDockerが自動作成するボリュームは、宣言と実マウントを照合したうえで対象から外します。データ本体の `/var/lib/clickhouse` は保存対象です。宣言のない追加マウントや保存先の変更は引き続きエラーにします。

Docker以外のホストプロセスや別サーバーによる書き込みまでは検出できません。対象領域はCVAT専用にし、保守中に他の処理から変更しないでください。同じディスク内のバックアップだけでは、そのディスク自体の故障には備えられません。

## 所要時間と世代の扱い

変更のないファイルは、安定した保存パスと前回のメタデータを使って再読み込みを省きます。変更されたファイルは読み込みますが、既に保存済みの内容は重複して保存しません。毎回tarや圧縮済みSQLを作り直す前処理はありません。インデックス用のキャッシュも再利用します。

ただし、全ファイルの確認、DBの変更されたファイルの読み取り、正常な停止にかかる時間は残ります。全データを更新した場合やファイル数が多い場合に、短時間で終わる保証はありません。実行結果にresticの変更件数、追加データ量などを表示します。停止時間はバックアップが終わるまで続きます。

途中失敗した世代には完了タグを付けません。`backup list`と`backup restore`は完了した世代だけを扱い、次回の比較元も完了した世代から選びます。古い世代の自動削除やpruneは行いません。保存容量の管理は別途必要です。

## 一覧と検査

```bash
"$CVATCTL" backup list

# リポジトリの構造と整合性を検査します。
"$CVATCTL" backup check

# 保存された全データも読み取って検証します。時間がかかるため毎回は必須にしません。
"$CVATCTL" backup check --read-data
```

パスワード不一致やresticの異常終了は、成功として扱いません。エラーを示す終了コードを返します。秘密情報が含まれ得る外部コマンドの出力を無条件にログへ転記しません。

## 新しいディレクトリへ復元する

```bash
"$CVATCTL" backup restore latest --target /mnt/restore/cvat-check

# 一覧にある完了世代のIDも指定できます。
"$CVATCTL" backup restore SNAPSHOT_ID --target /mnt/restore/cvat-old-version
```

復元先は存在していない新しいディレクトリに限定します。空であっても既存のディレクトリは拒否します。さらに、選択した世代のマニフェストを先に読み、元のバインド保存先や名前付きボリュームの実体ディレクトリと重なる場所を拒否します。シンボリックリンクも解決して照合し、マニフェストを確認できない場合は復元先を作成しません。

復元時にはresticの`--verify`で内容を検証します。復元用に元のボリュームをマウントする処理や、既存ディレクトリの上書きと削除は行いません。バックアップ後に保存先を移動した場合や別ホストで復旧する場合も、現在使っているデータ領域の外へ展開してください。所有者や権限を維持するため、展開後の確認には管理者権限が必要になる場合があります。

`source/metadata/manifest.json`に、元のボリューム名やバインド先と、`source/volume/...`または`source/bind/...`の対応が記録されます。`source/metadata/active.compose.json`には復元するDBとCVATのイメージ、環境変数、マウント構成があります。

実際のCVATへ戻す際は、[別サーバーで元の版へ復元する手順](backup-restore.md#別サーバーで元の版へ復元する)と同じく、本番とは別のDockerデーモンと空の保存領域を用意します。対応表に従ってデータをコピーし、元と同じCVAT、DBのメジャー版、CPUアーキテクチャ、設定を使用します。DBだけでなく画像、キー、Redis、モデル管理の保存領域も同じ世代で揃えます。別デーモンに古いcvatctl状態や停止済みコンテナIDをそのまま適用せず、Nuclio関数とモデルワーカーは記録した構成で再配備します。

このコマンドが行うのは検証付きの展開までです。本番への切替やDBのメジャー版変更は自動化していません。データ件数、画像と動画、アノテーション、権限、有効な拡張の動作を確認してから切り替えてください。

別ホストで既存のresticリポジトリへ接続する場合は、元のパスワードファイルを指定します。新しいパスワードは生成しません。

```bash
"$CVATCTL" --state-dir /secure/recovery-state backup init \
  --repository /mnt/backup/cvat --password-file /secure/original-backup-password
"$CVATCTL" --state-dir /secure/recovery-state backup restore latest \
  --target /mnt/restore/cvat-recovery
```

## 開発時の検証

Dockerを使用しない単体テストは次のコマンドで実行します。

```bash
python -m unittest discover -s components/extensions/tests -p 'test_backup*.py' -v
```

Dockerを使う試験は明示的に有効化します。ランダムな固有名を持つ試験用コンテナとボリュームだけを作成して削除し、実際のPostgreSQLの停止バックアップ、2回目の追加データ量、古い世代からの復元、格納データの検査を確認します。本番CVATの起動や推論、既存ボリュームの削除は行いません。

```bash
CVAT_BACKUP_DOCKER_TEST=1 python -m unittest discover \
  -s components/extensions/tests -p 'test_backup_docker.py' -v
```

運用環境での受入試験では、拡張を無効にした構成と、実際に使用するSAM系機能およびローカルモデル管理を含む構成の両方で`backup plan`を確認します。Dockerのない開発環境で単体テストが成功したことを、DB復元や実際の全拡張の動作を確認したこととは区別してください。

## 方式の根拠

[PostgreSQLのファイルシステムバックアップ](https://www.postgresql.org/docs/15/backup-file.html)は、サーバーの停止とデータ領域全体の保存を求めています。[resticの変更検出](https://restic.readthedocs.io/en/stable/040_backup.html#file-change-detection)と[復元](https://restic.readthedocs.io/en/stable/050_restore.html)に従い、ファイルの再利用と内容の重複排除を行います。
