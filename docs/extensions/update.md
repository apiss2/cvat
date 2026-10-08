# 検証済みの変更をreleaseへ反映する手順

通常更新では、機能ブランチを`dev`で統合し、検証したコミットと実イメージを`release`へ反映します。
有効にしているすべての拡張機能を確認対象にします。

## 更新対象のコミットを決める

開発担当者は、運用するupstreamのブランチを選びます。
`master`と`develop`を取得しても、その先端を本番へ自動反映しません。

```bash
git remote -v
git fetch upstream master develop
git fetch origin dev feat/modelRegistry
git status --short
```

取り込み時は公式の[更新ガイド](https://docs.cvat.ai/docs/administration/community/advanced/upgrade_guide/)と対象版の`CHANGELOG.md`を読みます。
形式のimporterとexporter、UIプラグイン、lambda API、workerの一覧、Nuclioの版、認証とストレージの変更を確認します。
リポジトリのマージに競合がなくても、実行時のインターフェースが同じとは限りません。

## devで受入確認を行う

開発担当者は、稼働中の検証環境を旧コードと旧設定で停止した後に変更を統合し、コミットしてから自動試験を実行します。
各試験に必要な依存関係をその試験の設定に従って用意します。
試験対象の例は次のとおりです。

| 対象 | 試験の場所と確認する内容 |
|---|---|
| 共通の運用 | `tests/extensions/`でComposeの合成、対象機能、起動と停止、イメージの検査を確認します。 |
| ITGformat | `tests/itgformat/`と完全なCVAT環境で、形式の登録、実ファイルの往復、画素型とラベルの対応を確認します。 |
| SAM2 | `serverless/pytorch/facebookresearch/sam2/tests/`とUIプラグインの試験、実RedisとGPUで、追跡、失敗時の扱いと保存を確認します。 |
| UltraSAM | `serverless/pytorch/camma/ultrasam/tests/`で入力とマスク変換を確認し、実GPUとCVAT画面で正負のポイントによる分割、注釈の保存と再読込を確認します。 |
| ONNXモデル管理 | `components/model_registry/tests/`で認証、権限、登録、更新、ポリゴン変換、推論と中継を確認します。 |
| 統合 | 本番と同じHTTPS、認証方式、共有ストレージで、通常の注釈操作と有効な拡張機能を確認します。 |

検証環境で現在の構成を停止する場合は、コードと設定を更新する前に旧構成の管理コマンドを使います。
同じComposeのモデル管理サーバーと推論ワーカーも、この停止に含まれます。
独立した構成から初めてまとめる場合は、ソースを切り替える前に[保存先と起動管理の移行](../model-registry/operations.md#保存先と起動管理の移行)を実施します。
検証サーバーの設定に本番の書き込み可能なデータ領域を指定しません。

```bash
CVAT_ENV="$(pwd)/.env"
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
```

未コミットの変更がある場合は整理してから統合します。
基準版を更新する場合は、取得した対象リリースのコミットを`dev`へマージします。
機能ブランチはレビュー済みのコミットをマージし、既に統合済みの変更を重複してcherry-pickしません。

```bash
git switch dev
git merge --ff-only origin/dev
# 採用する機能ブランチの例です。
git merge --no-ff origin/feat/modelRegistry
```

検証する機能を`CVAT_EXTENSIONS`へ記載します。
同じホストの管理サーバーとONNX推論ワーカーのイメージタグもソースから自動で決まるため、変更をコミットしてからビルドします。
既存の秘密情報、モデル保存先とCVATネットワークは維持します。
初回構築ではモデル管理の[初回設定](README.md#onnxモデル管理の初回設定)を先に行います。

```bash
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" doctor
./components/extensions/cvatctl --env-file "$CVAT_ENV" pull
./components/extensions/cvatctl --env-file "$CVAT_ENV" build
./components/extensions/cvatctl --env-file "$CVAT_ENV" up
./components/extensions/cvatctl --env-file "$CVAT_ENV" check
```

SAM2またはUltraSAMを初めて構築する場合は、通常の`up`で関数イメージをビルドします。
`build`だけではこれらの関数イメージの準備は完了しません。
新しいソースとNuclioの版で、有効にした画像分割と追跡を検証し、成功した実イメージを保存します。
UltraSAMは[確認方法](../ultrasam/README.md#確認方法)に従い、縦長と横長の画像でクリックと出力位置の一致も確認します。

本番データのDB移行を伴う場合は、旧本番のバックアップから復元した環境で実際の更新を試験します。
空のDBで起動できたことは、既存の利用者、タスク、注釈とキューを移行できたことの確認にはなりません。
合格したコミットID、設定、試験結果と所要時間を作業ツリー外へ記録します。

## 検証したイメージを保存する

ビルドしたソースが同じでも、基盤イメージや外部パッケージの内容が変わると、別の実イメージが作成される場合があります。
本番では検証済みのイメージを移送して使用し、同じタグを別内容で再ビルドしません。
検証ホストと本番ホストのCPUアーキテクチャとGPU実行条件が一致することも確認します。

`cvatctl config`の出力からCVATサーバー、UI、中継、SAM2関数とUltraSAM関数のイメージ参照を記録します。
同じ出力に含まれる管理サーバーとONNX推論ワーカー、および実際に使用するDB、Redis、Kvrocks、ClickHouse、Nuclioなどの基盤イメージも記録します。
この一覧には、本番で使用する既存のNuclio関数も含めます。

```bash
CVAT_RELEASE_DIR=/secure/cvat-releases/team-cvat-2.75.0-r1
umask 077
mkdir -p "$CVAT_RELEASE_DIR"
git rev-parse HEAD > "$CVAT_RELEASE_DIR/commit.txt"
./components/extensions/cvatctl --env-file "$CVAT_ENV" config \
  > "$CVAT_RELEASE_DIR/extensions.json"
```

確認したイメージ参照を1行1個で`images.txt`へ保存します。
空行とコメントは入れません。
`docker image inspect`で存在と実イメージIDを確認してから、一覧の全イメージを保存します。

```bash
mapfile -t CVAT_RELEASE_IMAGES < "$CVAT_RELEASE_DIR/images.txt"
test "${#CVAT_RELEASE_IMAGES[@]}" -gt 0
docker image inspect "${CVAT_RELEASE_IMAGES[@]}" > "$CVAT_RELEASE_DIR/images.json"
docker image save --output "$CVAT_RELEASE_DIR/images.tar" "${CVAT_RELEASE_IMAGES[@]}"
(cd "$CVAT_RELEASE_DIR" && sha256sum images.tar > images.tar.sha256)
```

同じファイルを本番へ移送し、チェックサムを検証してから読み込みます。
イメージを社内レジストリで管理する場合は、保存したダイジェストを取得する同等の手順を使います。
環境固有の秘密ファイルとモデルデータは、イメージのアーカイブへ混ぜません。

```bash
(cd "$CVAT_RELEASE_DIR" && sha256sum -c images.tar.sha256)
docker image load --input "$CVAT_RELEASE_DIR/images.tar"
mapfile -t CVAT_RELEASE_IMAGES < "$CVAT_RELEASE_DIR/images.txt"
docker image inspect "${CVAT_RELEASE_IMAGES[@]}" > "$CVAT_RELEASE_DIR/images-loaded.json"
```

読み込み後は、各参照が指す`Id`を`images.json`と照合します。
次は一覧のタグがすべて同じ実イメージを指すことを確認する例です。
検証に使っていないホストのイメージを一覧へ足して合格させません。

```bash
python3 - "$CVAT_RELEASE_DIR" <<'PYTHON'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
references = (root / "images.txt").read_text().splitlines()
expected = json.loads((root / "images.json").read_text())
loaded = json.loads((root / "images-loaded.json").read_text())
assert references and len(references) == len(expected) == len(loaded), "Image list mismatch"
wrong = [
    reference for reference, before, after in zip(references, expected, loaded)
    if before["Id"] != after["Id"]
]
assert not wrong, f"Image identity mismatch: {wrong}"
print("Saved image identities match")
PYTHON
```

レジストリのダイジェストで管理する場合も、そのダイジェストと実イメージIDの対応を記録して確認します。
SAM2の重みが関数イメージ以外へ保存される構成では、重みも別の成果物としてハッシュとともに保管します。
UltraSAMは重みを関数イメージに含めるため、その実イメージと取得した重みのSHA-256を保存します。

## releaseへ検証済みコミットを反映する

統合担当者は、確認済みのコミットだけを`release`へ反映します。
次の`TESTED_COMMIT`とタグ名を実際の承認値へ置き換えます。

```bash
git fetch origin release
git switch release
git merge --ff-only origin/release
git merge --ff-only TESTED_COMMIT
git tag -a team-cvat-2.75.0-r2 TESTED_COMMIT -m 'CVAT team release'
git push origin release refs/tags/team-cvat-2.75.0-r2
```

`--ff-only`が失敗する場合は、`release`と候補の履歴が分岐しています。
統合担当者が履歴を統合してその結果を再検証します。
本番で強制リセットしたり、`--force`で履歴を書き換えたりしません。
`release`を初めて作成する場合は、検証済みのコミットを基準にします。

## 本番を停止して復元点を保存する

運用担当者は、停止時間、確認に使う試験用タスク、復旧に必要な時間を決めます。
本番でまだ旧`release`のコードと設定を使用しているうちに、書き込みを止めて旧構成の停止とバックアップを行います。
停止対象と整合したバックアップの取り方は[バックアップと復元](backup-restore.md)に従います。

```bash
CVAT_ENV="$(pwd)/.env"
git status --short
git rev-parse HEAD
./components/extensions/cvatctl --env-file "$CVAT_ENV" status
# 利用停止、処理完了、必要な論理バックアップの取得後に実行します。
./components/extensions/cvatctl --env-file "$CVAT_ENV" down
```

全サービスの停止後、CVATとモデル管理の永続データを同じ時点の復元点として保存します。
バックアップが完了するまで次のコードへの切り替えを行いません。
`cvatctl`の状態記録と、旧版が参照していた設定ファイルも保管します。

## 本番のコードと設定を切り替える

本番は、統合担当者が確定した`release`とリリースタグを取得します。
コードを取得する操作とブランチへ変更を作成する操作を分け、本番では検証済みの履歴にのみ進めます。

```bash
RELEASE_TAG=team-cvat-2.75.0-r2
git fetch origin release "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
git switch release
git merge --ff-only "$RELEASE_TAG^{commit}"
git rev-parse HEAD
git rev-parse "$RELEASE_TAG^{commit}"
git status --short
```

2つのコミットIDが予定値と一致し、作業ツリーにコードの変更がないことを確認します。
必要な新設定を既存設定へ統合し、`cvatctl config`が示すモデル管理のイメージ参照を検証時の記録と照合します。
既存のデータディレクトリ、ネットワーク、サービス間の秘密情報を初期化し直しません。
既定のモデル保存先はリポジトリ直下の`cvat-model-registry/`です。
チェックアウトの場所も変える場合は、停止状態で保存データを移すか、`MR_HOME`を既存の絶対パスへ固定します。

## 保存したイメージで起動する

本番ではイメージ読み込み後の照合を済ませてから`--no-build`で起動します。
`cvatctl`はCVAT、設定した拡張機能、同じホストのモデル管理サーバーをまとめて起動します。

```bash
./components/extensions/cvatctl --env-file "$CVAT_ENV" config
./components/extensions/cvatctl --env-file "$CVAT_ENV" doctor
./components/extensions/cvatctl --env-file "$CVAT_ENV" up --no-build
docker logs --follow cvat_server
```

SAM2またはUltraSAMを使用する場合は、検証した関数イメージが本番にも存在することが必要です。
不足するイメージや整合しない設定で停止した場合は、その原因を直してから再試行します。
本番で通常の`up`へ切り替えて未検証の関数イメージを新規ビルドしません。

データ移行が終了したら、自動確認と[受入確認](#devで受入確認を行う)を行います。
画面の確認では、権限の異なる利用者、既存の標準Nuclio関数、ONNXモデル一覧と登録画面も対象にします。

```bash
docker exec cvat_server python manage.py migrate --check
./components/extensions/cvatctl --env-file "$CVAT_ENV" check
```

必要な確認にすべて合格した後、外側のアクセス制限を解除します。
コミット、実イメージID、設定、試験結果、バックアップと再開時刻を作業記録へ残します。
モデルやSAM2の状態形式を更新した場合は、保存済み注釈から新しい追跡を開始します。

## 更新に失敗した場合

ビルドまたはイメージ準備の段階で失敗した場合は、本番の旧構成を変更しません。
起動後に失敗した場合は、起動に使用した設定のまま管理サーバーとCVATを停止し、ログと失敗状態を保存します。
別設定へ切り替えてから停止すると対象を見失うことがあるため、停止が完了するまでは設定と状態記録を維持します。

DB移行後の復旧は、[バックアップと復元](backup-restore.md#更新に失敗した場合の復旧)に従います。
旧コードと旧イメージに対応するデータを一組で復元し、基本機能を確認してから利用を再開します。
本番の更新後に保存されたデータがある場合は、その回収方法を決めてから復元します。
