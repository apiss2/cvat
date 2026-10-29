# 検証から本番更新までの手順

[共通運用](README.md) / [バックアップと復元](backup-restore.md)

このページでは、変更を検証し、確認したコードとコンテナイメージを本番へ反映する手順を説明します。このフォークでは、開発用ブランチ `dev` で変更を統合し、検証済みの内容を本番用ブランチ `release` に反映する運用を想定しています。ブランチのマージだけでは、稼働中のCVATは更新されません。

作業は、更新対象の決定、検証環境での確認、実イメージの保存、本番のバックアップ、コードと設定の切り替え、再起動の順に行います。コマンド内のコミット、タグ、保存先は、採用した実際の値へ置き換えてください。

## 更新対象のコミットを決める

最初に、取り込む公式CVATの版と独自変更を決めます。以下では公式リポジトリの接続先を `upstream`、このフォークの接続先を `origin` とします。取得した先端をそのまま本番へ反映せず、検証するコミットを特定します。

```bash
git remote -v
git fetch upstream master develop
git fetch origin dev
git status --short
```

[公式の更新ガイド](https://docs.cvat.ai/docs/administration/community/advanced/upgrade_guide/)と対象版のCHANGELOGを読み、形式の入出力、画面、推論API、バックグラウンド処理、モデルを動かすNuclio、認証と保存先の変更を確認します。Gitの競合がなくても実行時の互換性が保たれるとは限りません。

## 上流との互換性を確認する

比較元は現在使用しているコミット、比較先は取り込む公式リリースのコミットとして特定します。リリースノートだけで判断せず、使用する独自機能が呼び出しているコードと設定を、比較先の実装と照合します。本体内部の関数やクラスを再利用している箇所は、非推奨の表示がなくても呼び出し方や処理順が変わる場合があります。

| 確認する接続点 | 照合する内容 |
| --- | --- |
| 画面から使うAPIと型 | [cvat-core](../../cvat-core/src/)と[画面プラグイン](../../cvat-ui/plugins/)の間で、モデル情報、アノテーション操作、推論の引数・応答・型が一致するか確認します。一覧取得のページ送り、ジョブの種類、権限や認証に関する変更も対象です。 |
| 形式の登録とデータ変換 | [形式の登録](../../cvat/apps/dataset_manager/formats/registry.py)、[データ変換](../../cvat/apps/dataset_manager/bindings.py)、プロジェクトへの取り込みが使う関数の引数と実行順を確認します。Datumaroなどの依存関係が変わる場合は、実ファイルの入出力も確認します。 |
| バックグラウンド処理 | [要求情報](../../cvat/apps/engine/rq.py)と[取り込み・出力の処理](../../cvat/apps/engine/request_handlers.py)について、利用者情報、完了時の通知、入力ファイルの削除、自動再試行を確認します。タスクごとに結果を確定する処理では、再試行による追記の重複や部分保存への影響を確認します。 |
| 起動と保存先 | [標準Compose](../../docker-compose.yml)と追加する構成を比較し、サーバー・ワーカーの起動条件、イメージ、外部パッケージ、保存領域を確認します。推論関数は各機能のビルド設定と[共通の配備処理](../../components/extensions/functions.py)も確認します。 |
| 保存済みデータの移行 | 対象アプリのDB移行と[Redisの移行](../../cvat/apps/engine/redis_migrations/)を確認します。コードの競合解消と既存データの移行は別に検証し、復旧には[保存したコード・イメージ・設定・データ](backup-restore.md#更新に失敗した場合の復旧)を一組で使用します。 |

試験用の型定義や代替実装も更新先の契約に合わせます。UI全体の型検査と、使用する画面プラグインを含む本番用ビルドを実行し、実際の本体との接続を確認してください。実行方法と文書の更新方針は[開発と保守のルール](../development.md)を参照してください。

比較したコミット、確認対象、実行した検証、環境、結果と未実施項目は変更のレビュー記録に残します。今後も必要な仕様や確認手順は既存の機能文書へ反映し、版ごとの調査結果や試験件数を恒久文書として追加しません。

## devで受入確認を行う

検証環境を旧コードと旧設定で停止し、未コミット変更を整理してからレビュー済みの変更を統合します。既に統合した変更を重複して取り込みません。導入済みサービスの移行は[モデル管理の移行手順](../model-registry/operations.md#保存先と起動管理の移行)を先に確認します。

```bash
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
git switch dev
git merge --ff-only origin/dev
# 実際にレビュー済みのコミットを指定します。
git merge --no-ff REVIEWED_FEATURE_COMMIT
```

ソースをコミットした後に、下表の試験と後述のビルドコマンドを検証環境で実行します。このフォークではGitHub Actionsを無効にしているため、コミットやpushでは試験とビルドは実行されません。既存のデータ、秘密情報、ネットワークを維持し、検証環境から本番の書き込み可能な保存先へ接続しません。初回設定は[導入手順](installation.md)を使用します。

| 対象 | 確認する内容と手順 |
|---|---|
| 共通管理 | [管理機能の回帰テスト](management.md#回帰テスト)で構成の合成、機能選択、起動、停止と関数の配備を確認します。 |
| データの取り込み | [DICOM取り込み](../annotation/dicom-import.md)、[アノテーション取り込み](project-annotation-import.md)、[タスク説明の一括登録](task-guide-import.md)で、画像照合、既存データの保持、部分失敗と権限を確認します。 |
| 専用形式の入出力 | [ITGformatの案内](../itgformat/README.md)に従い、全バックエンドの登録と実ファイルの往復を確認します。 |
| SAM2 | [設計と検証](../sam2/design.md)に従い、画像、マスク入力、両方向の追跡、Redis、Undoと保存を確認します。 |
| SAM3.1 | [設計と検証](../sam31/design.md)に従い、互換重み、画像と追跡、実GPUと実Redisを確認します。 |
| UltraSAM | [導入と検証](../ultrasam/operations.md)に従い、正負点、原寸座標、確定と保存を確認します。 |
| モデル登録 | [検証手順](../model-registry/testing.md)で3種類のモデル、部分更新、権限、中継と保存を確認します。 |
| 画像の選択と出力 | [アノテーション操作](../annotation/README.md)から、自動アノテーション、条件での画像移動、条件付きエクスポートを確認します。選択条件、既存アノテーションの保持、形式変換、権限とプレビュー後の変更も対象です。 |
| 物理長表示 | [物理長の表示](../annotation/physical-measurements.md)に従い、画像ごとのDicomTag、Tagと折れ線の編集、ITGformatの間隔、有効化と無効化を確認します。 |
| タスクの説明 | [ガイド表示](task-context.md)で、タスクとプロジェクトの説明の参照、権限、無効化後のデータ保持を確認します。 |
| 統合 | 本番と同じHTTPS、認証、共有ストレージで標準機能と有効な拡張を確認します。 |

```bash
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" doctor
./components/extensions/cvatctl --env-file "$CVAT_ENV" pull
./components/extensions/cvatctl --env-file "$CVAT_ENV" build
./components/extensions/cvatctl --env-file "$CVAT_ENV" up
./components/extensions/cvatctl --env-file "$CVAT_ENV" check
```

SAM2、SAM3.1、UltraSAMの関数イメージは通常のupで準備します。buildだけでは関数の準備は完了しません。cvatctl checkの成功だけで実画像とブラウザーの確認を省略しません。

DB移行を伴う場合は旧本番のバックアップから復元した隔離環境で更新を試験します。空のDBでの起動確認だけでは既存データの移行を確認できません。合格したコミット、設定、結果と所要時間を作業ツリー外へ記録します。

## 検証したイメージを保存する

同じソースとタグでも、基盤イメージや外部パッケージの変化で実イメージは変わります。検証ホストと本番のCPUアーキテクチャとGPU条件を合わせ、確認済みの実イメージを移送します。

cvatctl configからCVATサーバー、全ワーカー、UI、中継、モデル管理、ONNXワーカー、SAM2、SAM3.1、UltraSAMを記録します。DB、Redis、Kvrocks、ClickHouse、Nuclioと既存の標準関数も実際の使用構成に含めます。

```bash
CVAT_RELEASE_DIR=/secure/cvat-releases/APPROVED_RELEASE
umask 077
mkdir -p "$CVAT_RELEASE_DIR"
git rev-parse HEAD > "$CVAT_RELEASE_DIR/commit.txt"
./components/extensions/cvatctl --env-file "$CVAT_ENV" config > "$CVAT_RELEASE_DIR/extensions.json"
```

確認したイメージ参照をimages.txtへ1行1個で記載し、空行やコメントを入れません。

```bash
mapfile -t CVAT_RELEASE_IMAGES < "$CVAT_RELEASE_DIR/images.txt"
test "${#CVAT_RELEASE_IMAGES[@]}" -gt 0
docker image inspect "${CVAT_RELEASE_IMAGES[@]}" > "$CVAT_RELEASE_DIR/images.json"
docker image save --output "$CVAT_RELEASE_DIR/images.tar" "${CVAT_RELEASE_IMAGES[@]}"
(cd "$CVAT_RELEASE_DIR" && sha256sum images.tar > images.tar.sha256)
```

本番へ移送してチェックサムを検証し、読み込み後のIDを保存元と照合します。秘密ファイルとモデルの保存領域はこのアーカイブに混ぜません。重みを同梱する関数イメージ自体も、その利用条件に従って保管します。

```bash
(cd "$CVAT_RELEASE_DIR" && sha256sum -c images.tar.sha256)
docker image load --input "$CVAT_RELEASE_DIR/images.tar"
mapfile -t CVAT_RELEASE_IMAGES < "$CVAT_RELEASE_DIR/images.txt"
docker image inspect "${CVAT_RELEASE_IMAGES[@]}" > "$CVAT_RELEASE_DIR/images-loaded.json"
python3 - "$CVAT_RELEASE_DIR" <<'PYTHON'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
references = (root / "images.txt").read_text().splitlines()
expected = json.loads((root / "images.json").read_text())
loaded = json.loads((root / "images-loaded.json").read_text())
assert references and len(references) == len(expected) == len(loaded), "イメージ一覧が一致しません"
wrong = [ref for ref, before, after in zip(references, expected, loaded) if before["Id"] != after["Id"]]
assert not wrong, f"イメージIDが一致しません: {wrong}"
print("保存したイメージIDとの一致を確認しました")
PYTHON
```

社内レジストリのダイジェストを使う場合も実イメージIDとの対応を確認します。検証に使用していないイメージを一覧へ足して合格にしません。SAMの重みの取得元と内容識別、再ビルドに必要なローカル重みも別に記録します。

## releaseへ検証済みコミットを反映する

```bash
git fetch origin release
git switch release
git merge --ff-only origin/release
git merge --ff-only TESTED_COMMIT
git tag -a APPROVED_RELEASE_TAG TESTED_COMMIT -m '検証済みのCVATリリース'
git push origin release refs/tags/APPROVED_RELEASE_TAG
```

ff-onlyが失敗した場合は履歴が分岐しています。統合後の結果を再検証し、本番で強制リセットやforce pushを行いません。初回のreleaseも検証済みコミットを基準に作成します。

## 本番を停止してコードと設定を切り替える

利用者の書き込みと外部処理を止め、処理を完了させ、旧releaseのコードと設定で停止します。[バックアップ手順](backup-restore.md)に従い、論理バックアップと停止後の同時点の永続データ、旧設定と状態記録を保存します。保存が完了するまでコードを切り替えません。

```bash
CVAT_ENV="$(pwd)/.env"
git status --short
git rev-parse HEAD
./components/extensions/cvatctl --env-file "$CVAT_ENV" status
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
# バックアップが完了した後に実行します。
RELEASE_TAG=APPROVED_RELEASE_TAG
git fetch origin release "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
git switch release
git merge --ff-only "$RELEASE_TAG^{commit}"
git rev-parse HEAD
git rev-parse "$RELEASE_TAG^{commit}"
git status --short
```

コミットIDが承認値と一致し、コードに未コミット変更がないことを確認します。既存設定に必要な項目だけを統合し、保存先、ネットワーク、秘密情報を初期化し直しません。チェックアウトを移す場合はMR_HOMEを維持するか、停止中に保存データを移します。

## 保存したイメージで起動する

イメージの照合後、検証時と同じ設定で起動します。

```bash
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" doctor
./components/extensions/cvatctl --env-file "$CVAT_ENV" up --no-build
docker logs --follow cvat_server
```

不足イメージや設定不一致で失敗した場合は原因を修正します。本番で通常のupへ切り替えて未検証イメージを作成しません。DB移行後に自動確認と全有効機能の受入確認を実施します。

```bash
docker exec cvat_server python manage.py migrate --check
./components/extensions/cvatctl --env-file "$CVAT_ENV" check
```

確認完了後だけアクセス制限を解除し、コミット、実イメージ、設定、結果、バックアップと再開時刻を記録します。モデルと状態形式を変更した場合は、保存済みアノテーションから新しい追跡を開始します。

## 更新に失敗した場合

準備段階の失敗なら旧本番を変更しません。起動後の失敗は、使用中の設定を維持して停止し、ログと失敗状態を保存します。DB移行後は旧イメージだけに戻さず、[復旧手順](backup-restore.md#更新に失敗した場合の復旧)で旧コード、旧イメージ、旧設定、対応データを一組で復元します。更新後に追加されたデータがある場合は回収方針を決めてから復旧します。
