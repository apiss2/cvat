# 文書とテストの保守ルール

[追加機能の一覧](README.md) / [開発方針](../AGENTS.md)

このページは、このフォークのコード、文書、テストを編集する人向けです。配置は機能の担当範囲と実行環境から決め、文書には現在も使える説明を残します。機能を追加する場合の設定と起動への組み込みは、[機能の管理手順](extensions/management.md#機能を追加するときの手順)を参照してください。

## 文書に残す情報を決める

編集する情報は、次の基準で置き場所を選びます。新しい情報が既存の目的に属する場合は、その文書を更新します。同じ仕様や手順は一つの文書にまとめて管理し、このページではその文書を「正本」と呼びます。

| 情報 | 置き場所 | 理由 |
| --- | --- | --- |
| 現行の操作、入力形式、設定、制限 | `docs/` の該当する機能または共通運用の文書 | 利用者が現在の動作を一か所で確認できるようにします。 |
| 繰り返し使う更新手順、試験方法、設計理由 | 既存の更新、検証、設計の節 | 次の変更でも再利用でき、過去の作業を読まずに判断できます。 |
| 実装ディレクトリの責務と案内 | そのディレクトリの `README.md` | コードから操作説明と試験の場所へ移動できるようにします。詳細な仕様や手順は正本へリンクします。 |
| 調査日、対象コミット、合格件数、所要時間、実行ログ、その作業で未実施だった確認 | PRやコミットの説明、作業報告。ログのファイルはGit管理外の作業領域 | 過去の実施結果は次の版の動作を保証しないため、現行の手順と分けて参照します。 |

版別の互換性報告や作業日誌を `docs/` に新設しません。更新で判明した問題はコードと回帰テストに反映し、再利用できる確認観点を[更新手順](extensions/update.md)へ統合します。既存の上流 `CHANGELOG.md` は上流のリリース履歴として扱います。

現行の入力形式の版、依存するモデルの固定コミット、保存済みデータの互換条件は仕様なので残します。バックアップのコミット、設定、イメージを保存する手順も復旧に必要です。日付や版を含むという理由だけで、これらを作業ログとして削除しません。

## 文書の入口と名前を揃える

機能の目的を説明してから、画面名、設定名、内部の名称を示します。機能一覧の表示名と分類は[追加機能の一覧](README.md)に揃え、設定キーやGPU、追加サービスの要否は[機能の管理一覧](extensions/management.md)を参照します。長い機能名へ個々の変更点を付け足しません。

新しい独立した利用目的がある場合だけ文書を増やします。既存文書を分割する場合は、元の入口と関連リンクを同じ変更で更新し、同じ手順を両方へ残しません。利用者の操作、管理者の導入と運用、開発者の実装と試験は、節または文書を分けて案内します。

独自機能と運用を説明する文書の本文と見出しは日本語で記述します。画面上の名称、コマンド、API、設定キーと標準の技術用語は原文を使います。PR番号や「今回の変更」を知らなくても読めるよう、現在の機能と動作を主語にします。上流由来の文書と第三者のライセンス、著作権表示は、この説明上の規則に合わせて書き換えません。

## テストを機能ごとに配置する

独自テストの配置は、対象の機能を変更するときに一緒に確認できる場所へ揃えます。実行コードの場所だけを理由に、テストの置き場所を増やしません。

| 試験の担当範囲 | 配置 | 配置の根拠 |
| --- | --- | --- |
| 独自のPython処理、形式変換、推論、運用コマンド | `components/<機能>/tests/` | `components/` は独自機能の管理単位です。本体内の形式処理や `serverless/` の推論も、管理する機能と同じ場所で試験を探せます。 |
| CVAT画面プラグイン | `cvat-ui/plugins/<機能>/tests/` | 画面プラグインは独立した入口とビルド単位を持ちます。画面の変更と試験を同じ場所で管理します。 |
| モデル管理サービス独自の画面 | `components/model_registry/tests/` | 対象は同じ機能の `static/` にある画面で、CVAT画面プラグインとは別の実装です。JavaScriptでも所有する機能を基準にします。 |
| 本体の共有環境を使うHTTPなどの結合試験 | 本体の既存の試験群 | 本体の認証、DB、サービス用の共通準備を再利用する試験は、それらと同じ実行環境で確認します。 |

形式変換の実装は `cvat/apps/dataset_manager/`、SAM2とUltraSAMの推論実装は `serverless/` にありますが、独自のPythonテストはそれぞれ `components/itgformat/tests/`、`components/filtered_export/tests/`、`components/task_guide/tests/`、`components/sam2/tests/`、`components/ultrasam/tests/` で管理します。配備時の読み込み経路を変える必要がないため、テストを揃える目的で実行コードを移動しません。

本体の試験群に置く根拠は、[共通の試験準備](../tests/python/conftest.py)、そこから読み込む[サービスの準備処理](../tests/python/shared/fixtures/init.py)と[実行設定](../tests/python/pytest.ini)を使うことです。例えば [Job APIの結合試験](../tests/python/rest_api/test_jobs.py)に追加する応答項目の検証は、その試験群に残します。外部サービスを必要としない独自の単体試験は、機能側に配置します。本体の試験群自体を、このフォークの配置に合わせて移動しません。

## 試験用データと依存関係をまとめる

試験専用の入力ファイルは、各機能の `tests/fixtures/` に置きます。例えば、タスク説明の入力例は `components/task_guide/tests/fixtures/` です。[モデル管理の作例](../components/model_registry/examples/)のように、利用者向けの作例を試験でも使う場合は、その配置を正本として参照し、試験用に重複コピーしません。

補助コードは各機能の `tests/` 配下に置きます。専用の依存一覧を持つ場合は、各機能の `tests/requirements.txt` に置き、同じ機能の実行時依存を参照できます。共通管理の試験は `components/extensions/tests/` に集め、特定機能の試験用データを共通領域へ混在させません。

同じ機能でも、依存するライブラリの固定版が異なる実行環境は分けます。この場合に限り、同じ `tests/` に `requirements-<環境>.txt` を置き、実行入口と対応付けます。本体接続の `requirements-backend.txt` はCVATサーバーの依存へ追加する一覧です。モデル管理APIとはNumPyの固定版が異なるため、通常の共通管理試験の依存一覧を本体環境へ重ねません。両環境で共通のpytestと計測ツールは `requirements-quality.txt` を参照し、版を一か所で管理します。

実GPU、Redis、モデル管理の実サービスを対象に、明示的に起動する確認スクリプトも各機能の `tests/` に置きます。単体試験と同じ機能を担当し、実行条件だけが異なるためです。通常のテスト収集では実サービスへ接続せず、実行方法と必要条件は各機能の検証手順へ記載します。

テストは実在するソースと設定を参照します。関数の配備設定を生成する機能では、[共通の生成処理](../components/extensions/functions.py)と各機能の `build.json` を使います。使われていない旧YAMLや旧配布物を試験用に再作成しません。

削除済みの配布物だけを対象にして常にスキップするテストは削除するか、現行の配布方法を検証する内容へ更新します。GPU、実Docker、外部の参照実装などを条件とする有効な試験は、必要条件と対象範囲を文書に残します。必要な機能のソースが見つからない場合に、旧配置を探したりスキップして成功扱いにしたりしません。

## 機能ごとに試験を実行する

開発時の独自Pythonテストはpytestへ統一します。通常の `assert`、`pytest.raises`、fixture、入力ごとの `parametrize` を使います。標準ライブラリの `unittest.mock` はモックの部品なので使用できます。各機能の依存関係を準備し、リポジトリのルートから機能ごとに別プロセスで実行します。

```bash
python -m pytest -q components/itgformat/tests
python -m pytest -q components/filtered_export/tests
python -m pytest -q components/extensions/tests
python -m pytest -q components/task_guide/tests
```

状態を順番に変える確認や、一度の外部呼び出しで得た多数の結果を照合する場合は、pytest 9以降の `subtests` fixtureを使えます。独立して準備できる入力表は `parametrize` を優先します。必要なpytestの版は各機能の `tests/requirements.txt` で指定します。実際のCVATモデルを使うアノテーション取り込みの試験は、[その機能の手順](extensions/project-annotation-import.md#回帰テストと実環境の確認)に従って別環境で実行します。

配備後の [ITGformat診断](../components/itgformat/tests/check_in_cvat.py)だけは標準ライブラリの `unittest` を使います。`cvatctl check` がこのコードを稼働中のCVATサーバーとワーカーへ送り、pytestのない本番イメージ内で実行するためです。開発時の単体試験はpytestで実行し、この診断のためだけに本番イメージへ試験用の依存を追加しません。

全機能を一つのPython環境と一つの試験プロセスへまとめることは配置規則に含めません。モデル管理と推論機能では依存するPythonやライブラリが異なり、試験用のモジュール置き換えや同名の `conftest.py` もあるためです。必要な機能の文書から、その機能の依存一覧と実行コマンドを確認してください。

上流の `pytest tests/python/` は `components/` 配下の試験を収集しません。このフォークでは[共通管理の開発手順](extensions/management.md)に従ってローカルで試験を実行します。自動実行を導入する場合も、各機能の実行環境とコマンドを明示し、対象の試験が収集されることを確認します。

画面の変更と上流の更新では、機能単位の試験に加えてUI全体の型を確認します。JavaScript依存関係を準備したチェックアウトのルートで、[package.json](../package.json)の型検査を実行します。

```bash
yarn run type-check
```

プラグインを含む本番用ビルドは、[更新手順の受入確認](extensions/update.md#devで受入確認を行う)にある `cvatctl build` で行います。設定ファイルの `CVAT_EXTENSIONS` による選択を反映し、文書に固定のプラグイン一覧を複製しません。

実GPU、実Redis、ブラウザー、バックアップ復元を使う試験は、必要な環境を用意したうえで明示的に起動します。通常の単体試験の成功と実環境での受入確認を区別し、実行結果には対象、条件、実行したコマンド、スキップと未実施事項を記載します。結果をこのページや機能の文書へ累積しません。

## 本体との接続を確認する

APIの入口、権限、形式の登録、応答項目など本体への接続を変更した場合は、[本体接続の試験](../components/extensions/tests/run_core_tests.py)も実行します。実際のCVATモジュールを読み込み、URLの解決からリクエストの振り分け、ファイルの受け取り、バックグラウンド処理への投入までを確認します。

[上流の開発環境](../site/content/en/docs/contributing/development-environment.md)で案内するCVATサーバーのPython依存を準備し、その仮想環境へ[本体接続の試験用依存](../components/extensions/tests/requirements-backend.txt)を追加します。軽量な単体試験と混ぜず、専用のPythonプロセスで実行してください。

```bash
python -m pip install -r components/extensions/tests/requirements-backend.txt
python -m pytest -q components/extensions/tests/run_core_tests.py
```

この試験は一時領域とSQLiteを使用し、リポジトリのモデルやマイグレーションを変更しません。認可サービス（OPA）の判断、Redisとワーカー間のロックは代替実装を使います。認可へ渡す操作範囲と対象、拒否時にファイルやキューを変更しないことを検証しますが、本番の認可ルールや並行処理を保証する試験ではありません。実際のPostgreSQL、Redis、OPAと配備後の確認は、各機能の受入確認として実施します。

隔離したCVAT環境の準備は、[共通の補助コード](../components/extensions/tests/cvat_backend.py)へまとめています。アノテーション保存の[モデルを使う試験](../components/project_annotations/tests/run_orm_tests.py)もこれを再利用し、実行はそれぞれ別プロセスにします。

## 独自コードの品質を計測する

Pythonの計測は `cvatctl quality` で行います。カバレッジはテストが実行した行と分岐の割合、循環的複雑度は関数内の独立した処理経路の数を表します。CRAPスコアは複雑度と未実行の割合を組み合わせ、変更前に詳しく確認する関数を探すために使用します。

計測には [Coverage.py](https://coverage.readthedocs.io/en/7.13.5/) と [Radon](https://radon.readthedocs.io/en/latest/) を使用します。集計に使う環境は[計測用の共通依存](../components/extensions/tests/requirements-quality.txt)から準備します。各機能の試験用依存は別途必要です。`extensions` を動かす環境へは[通常の共通管理試験用依存](../components/extensions/tests/requirements.txt)を入れ、本体用の環境と分けます。別のPython環境で試験を動かす場合は、その環境にも同じ版のCoverage.pyを導入します。推論用のPythonやPyTorchを、集計用の環境へ揃える必要はありません。

```bash
python -m pip install -r components/extensions/tests/requirements-quality.txt
# 別の試験環境では、その環境のPythonで計測用ライブラリを追加します。
/srv/cvat-test-envs/sam2/bin/python -m pip install coverage==7.13.5
```

### 分母と対象を揃える

計測対象は、取り込み済みの上流との共通祖先から自動的に調べます。既定の比較先は `upstream/master` です。別の接続先やリリースを使う場合は、各コマンドの `--upstream` にGit参照を指定します。

| 対象 | 集計方法 |
| --- | --- |
| 独自に追加したPythonの実装 | 未実行、未読み込みのファイルも含め、実行可能な全行と全分岐を分母にします。 |
| 本体のPythonファイルへ追加、変更した箇所 | 追加、変更した実行可能行のカバレッジを別に集計します。追加、変更、削除が及んだ現存する関数は、その関数全体の複雑度とカバレッジを評価します。削除済みの行を分母へ加えません。 |
| 試験コード、作例、マイグレーション、品質集計コード | 実装のカバレッジから除外します。テストや計測補助コードを多く実行して、製品コードの率が高く見えることを防ぎます。 |
| TypeScriptとJavaScript | 未計測としてファイル一覧を出します。既存のテストでは変換後のJavaScriptを仮想環境で実行するため、元のソース位置を復元する対応が必要です。Pythonの率へ含めません。 |

関数ごとの値を対応付けて、`CRAP = C² × (1 − L)³ ＋ C` を計算します。`C` はその関数の循環的複雑度、`L` はその関数の行カバレッジを0から1で表した値です。分岐カバレッジは別列に出します。ファイル全体の率を各関数へ流用せず、ネストした関数も分けます。[指標の定義と制約](https://dx42.github.io/gmetrics/metrics/CrapMetric)を参照してください。

### 試験を収集して集計する

結果の保存先はGit管理外のディレクトリを指定します。各機能の試験用依存を準備したうえで、例えば次のように実行します。`quality` は環境の配備や推論を行いません。

```bash
CVAT_QUALITY_DIR="$HOME/cvat-quality/current"
bash components/extensions/cvatctl quality scope
bash components/extensions/cvatctl quality run --suite extensions --output "$CVAT_QUALITY_DIR"
bash components/extensions/cvatctl quality run --suite task_guide --output "$CVAT_QUALITY_DIR"
bash components/extensions/cvatctl quality run --suite sam2 \
  --python /srv/cvat-test-envs/sam2/bin/python --output "$CVAT_QUALITY_DIR"
bash components/extensions/cvatctl quality report --output "$CVAT_QUALITY_DIR"
```

試験名は `extensions`、`itgformat`、`filtered_export`、`task_guide`、`dicom_import`、`project_annotations`、`model_registry`、`sam2`、`sam31`、`ultrasam`、`core_integration` です。本体との接続を確認する `core_integration` も、全体の品質判定に必要な標準の試験に含めます。CVATサーバーのPython依存を持つ別環境で、`--python` を指定して実行します。アノテーション保存をさらに確認する `project_orm` は、同じ依存環境で追加実行できます。GPU、実Redis、Dockerの確認は、各機能の手順で明示的に実行します。

集計結果には、使用したソース、試験、Pythonとライブラリの版、失敗と未実施を残します。異なるソースの計測結果は混合しません。未実施の機能がある集計は部分的な結果として扱い、全機能を確認した値とは比較しません。出力するJSON、CSV、HTMLと実行ログを、恒久文書としてコミットしません。

通常の動作試験は元ソースを読み込み、配備用コピーの構成確認とは分けます。例えばSAM3.1は元ソースに対して動作を検証し、別の試験で配備コピーの内容と読み込みを確認します。一時ディレクトリにある同名ファイルの実行結果を元ソースへ自動的に合算しません。別実装を混同せず、どのソースを実行した数値か確認できるようにするためです。

### 変更時の判断に使う

変更した関数について、未実行の異常系や分岐を確認します。複雑度10超またはCRAP30超をレビューの目安とし、CRAP60超は優先して確認します。これらはこのフォークで確認順を決める目安であり、バグの有無を判定する基準ではありません。分割やテスト追加を行わない場合も、その理由を変更の説明へ残します。

標準の試験をすべて実行した後は、計測対象の全関数へ明示した上限を適用し、終了コードで判定することもできます。未実施または失敗があると、この判定は不合格になります。既存コードも対象なので、導入時は集計結果を確認してから上限を決めます。

```bash
bash components/extensions/cvatctl quality report \
  --output "$CVAT_QUALITY_DIR" --max-crap 60
```

計測条件を揃えて変更前後を比較し、合計のカバレッジだけで判断しません。カバレッジは実行を示すもので、正しい結果を検証したことを保証しません。CRAPが低くても、認証、データ保持、後始末、実GPUでの挙動などの受入確認は必要です。既存のGitHub Actions無効化の運用は維持し、ローカルで計測する手順として管理します。

## 編集を完了する前に確認する

1. 変更が属する機能と文書の正本を確認します。新しいページやディレクトリが必要な場合は、既存のものと異なる責務を説明します。
2. 影響するコード、設定例、操作説明、試験を同じ変更で更新します。設定値の変更と説明だけの変更を区別し、既定値や必要条件は実装で照合します。
3. 移動または削除するパスを `rg` で検索し、実行コード、試験、入力例、文書、リンクを更新します。配備コマンドがホスト側の試験スクリプトを読む場合も対象です。
4. 移動後の場所から対象の試験を実行し、必要な補助コードと入力が読み込まれることを確認します。参照先ファイル、見出しへのリンク、古いコマンド、`git diff --check` も確認します。
5. 差分を読み直し、日付付きの作業記録、固定の合格件数、旧配置への転送、不要な重複文書を追加していないか確認します。実施結果は作業報告へ記載します。

新たな配置の例外が必要になった場合は、対象の所有者、実行環境、既存配置を使えない理由をこのページへ記載し、関連する入口も更新します。機能を追加した順序や作業者の都合を、配置の理由にしません。
