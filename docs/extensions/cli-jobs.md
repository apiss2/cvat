# CLIからJobを操作する

[追加機能の一覧](../README.md) / [機能の選択と管理](management.md)

このフォークの `cvat-cli job` は、Jobの担当者（Assignee）、作業段階（stage）、作業状態（state）の変更、Job情報とアノテーションの取得、個別のアノテーション操作、Issueの登録・確認・解決に対応します。既存APIを使用するため、サーバーの再ビルドや追加サービスは必要ありません。

## 導入

フォークのチェックアウトのルートで、クライアント用の仮想環境を作成し、フォーク版のCLIをインストールします。例えば uv を使う場合は次のとおりです。

```bash
uv venv
uv pip install -e ./cvat-cli
uv run --no-project cvat-cli job update --help
```

CLIの依存するSDKの版は `cvat-cli/pyproject.toml` に従います。公開されている公式CLIのインストールだけではこのコマンドは追加されません。接続先のCVATとCLI・SDKの互換性を確認してください。

## 操作

接続先、認証、Organizationは既存の共通引数を使います。次の例はJob IDが42、担当者のユーザーIDが7の場合です。`--auth user` のパスワードは対話入力または既存の認証設定を使用します。

```bash
cvat-cli --server-host https://cvat.example.org --auth user --org my-org job update 42 --assignee 7
cvat-cli --server-host https://cvat.example.org --auth user --org my-org job update 42 --stage validation --state new
cvat-cli --server-host https://cvat.example.org --auth user --org my-org job update 42 --state rejected
cvat-cli --server-host https://cvat.example.org --auth user --org my-org job update 42 --unassign
```

複数の項目を指定すると、1回のPATCHリクエストで更新します。

| 引数 | 値と動作 |
| --- | --- |
| `--assignee` | 正の整数のユーザーID。ユーザー名ではありません。 |
| `--unassign` | 担当者を解除します。`--assignee` との併用はできません。 |
| `--stage` | `annotation`、`validation`、`acceptance` |
| `--state` | `new`、`"in progress"`、`completed`、`rejected` |

少なくとも1項目の指定が必要です。指定しない項目はリクエストに含めません。ただし、stageを変更してstateを指定しない場合、CVAT標準の処理によりstateは `new` になります。状態を指定して引き継ぐ場合はstageとstateを同時に指定してください。処理が成功すると終了コードは0です。APIエラーは既存CLIのエラー処理で報告され、終了コードは1になります。不正な引数は終了コード2になります（更新項目がない場合は1）。権限と担当者の存在確認は既存サーバーに委ねます。

## Jobの情報を取得する

以下の例では共通の接続先・認証引数を省略しています。必要な場合は `job` より前に指定します。

```bash
cvat-cli job ls --json --task-id 12
cvat-cli job ls --json --project-id 3
cvat-cli job get 42
cvat-cli job labels 42
```

`job ls` は既存のTask一覧と同じく、標準ではIDを1行ずつ、`--json` ではJSON配列を返します。Task ID、Project IDはサーバー側の絞り込み条件です。`get` はJob詳細、`labels` はラベルやAttributeの定義をJSONで返します。ラベル一覧はJobのOrganizationを参照し、SDKのページ取得を利用します。

## アノテーションの読み取りと更新

読み取り結果はCVAT APIのJSON形式です。Tag、Shape、Track、音声のIntervalに対応します。

```bash
cvat-cli job annotations get 42
cvat-cli job annotations get 42 --type tag --frame 100 --label-id 5
cvat-cli job annotations get 42 --type shape --id 456
```

`--type` は `tag`、`shape`、`track`、`interval` です。指定した条件はANDで絞り込みます。Job内のアノテーションを取得してからCLIで選択するため、通信量は減りません。IDとラベルIDはサーバー上のIDです。種類が異なるアノテーションのIDは重複し得ます。フレーム番号はCVAT APIのフレーム番号であり、Trackでは保存されたフレームとキーフレームへの一致を扱います。補間されたフレームの図形を生成する機能ではありません。Intervalは音声の時間区間であり、画像のフレーム条件の対象外です。フィルター結果でもCVATのコレクション形式を保持します。

### 追加

```bash
cvat-cli job annotations create 42 --file new.json
```

`new.json` の例です。既存のIDを指定せず、必要なフィールドをCVATのAPIに従って記載します。複数種類・複数件をまとめて追加できます。

```json
{
  "tags": [
    {
      "frame": 100,
      "label_id": 5,
      "attributes": [
        {"spec_id": 8, "value": "Yes"}
      ]
    }
  ]
}
```

`--file -` は標準入力からJSONを読み取ります。追加するアノテーションが空の場合や、既存のIDが含まれる場合は拒否します。

### 特定のアノテーションを更新する

```bash
cvat-cli job annotations update 42 --type tag --id 123 --attribute 8=Yes
cvat-cli job annotations update 42 --type shape --id 456 --points 10 20 30 20 30 40 10 40
cvat-cli job annotations update 42 --type track --id 789 --file changes.json
```

`--attribute SPEC_ID=VALUE` は指定した属性だけを追加・更新します。複数回指定できます。VALUEは文字列で、空値も指定できます。`--points` はShapeの座標全体を置換します。`--attribute` と `--points` は併用できます。`--file` は1個のアノテーションの変更フィールドをJSONオブジェクトで指定します。他の変更引数とは併用できません。

```json
{"group": 2}
```

たとえば上記はgroupだけを変更します。指定しないフィールドは取得した値を維持します。`--file` で指定できるフィールドは、取得した対象アノテーションのJSONに既に存在するキーに限ります。サーバーが空値などの理由で返さなかったフィールドは、APIで有効な項目であっても `--file` から新規に設定できません。JSONで `attributes`、`points`、`shapes`、`elements` などの配列を指定すると、その配列全体が置換されます。JSONでIDを指定する場合は対象のIDと一致する必要があります。対象は各コレクションの最上位のアノテーションです。TrackのキーフレームやSkeletonの要素を独立したIDで直接更新するのではなく、親のTrackやShapeを指定してください。

### 特定のアノテーションを削除する

```bash
cvat-cli job annotations delete 42 --type tag --id 123
```

更新と削除では、種類とIDが一致する対象をJobから取得し、存在しなければ更新せずエラーにします。作成、更新、削除はそれぞれ標準APIの `PATCH` の `action=create`、`action=update`、`action=delete` を使用し、Job全体を置換したり削除したりしません。APIの応答JSONを標準出力に返します。

CVATの更新APIは対象アノテーションを取得してから更新する処理に対して排他制御やリビジョン照合を提供しません。別の利用者が同一アノテーションを同時に編集すると、先に取得した内容で変更を上書きする可能性があります。更新・削除は作業が停止しているJobや、操作対象の編集者を管理できる環境で実行してください。属性や図形の妥当性と権限は既存のサーバーに委ねます。

## Issueを登録・解決する

```bash
cvat-cli job issue ls 42 --unresolved
cvat-cli job issue create 42 --frame 100 --position 0 0 100 100 --message "必須Attributeが未設定"
cvat-cli job issue comment 42 --issue-id 15 --message "修正済み"
cvat-cli job issue comments 42 --issue-id 15
cvat-cli job issue resolve 42 --issue-id 15
cvat-cli job issue reopen 42 --issue-id 15
```

`ls` は全IssueをJSON配列で返し、`--unresolved`、`--resolved`、`--frame` でサーバー側の絞り込みを行います。`create` は対象フレーム、画素座標のx/yの組、最初のコメントを必須とし、`--assignee` でユーザーIDも指定できます。座標のないTagの指摘でもCVAT標準APIはpositionを要求するため、利用者が対象のフレーム領域を指定します。座標の自動生成は行いません。フレームはJobの開始・終了番号の範囲内に限定します。疎なフレーム選択の詳細や図形の妥当性はサーバーの仕様に従います。

Issueの登録とコメントの追加は作成されたIDを標準出力に返します。`comments` はコメントの一覧をJSONで取得します。解決と再開は状態のみを変更します。コメントの取得・追加、解決、再開は、指定したIssueが対象Jobに属することを確認してから実行します。認証と権限の判定は標準APIを利用します。

## 開発と検証

CLIの標準のコマンド登録に2行を追加し、Jobの基本操作は `commands_jobs.py`、アノテーション操作は `commands_job_annotations.py`、Issue操作は `commands_job_issues.py`、共通の引数・JSON処理は `job_cli_common.py` に配置します。CLIパッケージとしてインストールできるようにするため、実装を `components/` に置かず、CLIのパッケージ内に置きます。SDK、サーバー、データベース、画面の実装は変更しません。上流が同名のJobコマンドを追加した場合は、機能を比較して登録と実装を統合してください。

`--stage` と `--state` の選択肢は、サーバーの [`StageChoice` と `StateChoice`](../../cvat/apps/engine/models.py) の値をCLI側で個別に定義しています。自動同期は行わないため、上流の更新で列挙値が追加・変更・削除された場合は、[`commands_jobs.py`](../../cvat-cli/src/cvat_cli/_internal/commands_jobs.py) の選択肢、回帰テスト、この文書を手動で更新してください。追随しない場合、新たにサーバーが受理する値をCLIが引数解析時に拒否することがあります。[列挙値の同一性テスト](../../components/cli_jobs/tests/test_job_choices.py) はCLIの選択肢とサーバーソースの列挙値を双方向に照合し、不一致があれば不足・余分な値を表示して失敗します。Djangoや実サーバーは不要です。列挙値の定義方法自体が変わった場合は、試験の抽出処理も見直してください。

開発時はルートから次のコマンドで試験します。

```bash
uv pip install -e ./cvat-cli pytest
uv run --no-project python -m pytest -q components/cli_jobs/tests
```

試験は既存のCLIのコマンド登録と引数解析を使用し、SDKの呼び出しをモックに置き換えます。Jobの更新、アノテーションの取得・絞り込み・作成・個別更新・削除、Issueの登録・一覧・コメント・解決、対象Jobの照合と入力エラーを確認します。実サーバーの認証、Organization、権限、stage変更時のstate、アノテーションの保存結果とIssueの表示は別途、検証用Jobで確認してください。

品質計測には既存の `cvatctl quality` の `cli_jobs` 試験を使用します。CLIをインストールした仮想環境に計測用依存を追加してください。

```bash
uv pip install -r components/extensions/tests/requirements-quality.txt
bash components/extensions/cvatctl quality run --suite cli_jobs --python .venv/bin/python --output "$HOME/cvat-quality/current"
```

この機能はクライアント側だけの変更なので、`CVAT_EXTENSIONS`、サーバーの起動・停止・ビルド・確認処理、保存データに対する新しい設定や処理はありません。クライアントを公式版に戻せば独自コマンドは利用できなくなりますが、すでに変更したJobの値は保持されます。
