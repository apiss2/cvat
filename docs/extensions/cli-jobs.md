# CLIからJobを更新する

[追加機能の一覧](../README.md) / [機能の選択と管理](management.md)

このフォークの `cvat-cli job update` は、Jobの担当者（Assignee）、作業段階（stage）、作業状態（state）を変更します。既存のJob更新APIを使用するため、サーバーの再ビルドや追加サービスは必要ありません。

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

## 開発と検証

CLIの標準のコマンド登録に2行を追加し、処理は独立した `commands_jobs.py` に配置します。CLIパッケージとしてインストールできるようにするため、実装を `components/` に置かず、CLIのパッケージ内に置きます。SDK、サーバー、データベース、画面の実装は変更しません。上流が同名のJobコマンドを追加した場合は、機能を比較して登録と実装を統合してください。

`--stage` と `--state` の選択肢は、サーバーの [`StageChoice` と `StateChoice`](../../cvat/apps/engine/models.py) の値をCLI側で個別に定義しています。自動同期は行わないため、上流の更新で列挙値が追加・変更・削除された場合は、[`commands_jobs.py`](../../cvat-cli/src/cvat_cli/_internal/commands_jobs.py) の選択肢、回帰テスト、この文書を手動で更新してください。追随しない場合、新たにサーバーが受理する値をCLIが引数解析時に拒否することがあります。

開発時はルートから次のコマンドで試験します。

```bash
uv pip install -e ./cvat-cli pytest
uv run --no-project python -m pytest -q components/cli_jobs/tests
```

試験は既存のCLIのコマンド登録と引数解析を使用し、SDKのJob更新呼び出しをモックに置き換えます。送信項目、担当解除、複数項目、入力エラー、APIエラーを確認します。実サーバーの認証、Organization、権限、stage変更時のstateは別途、検証用Jobで確認してください。

品質計測には既存の `cvatctl quality` の `cli_jobs` 試験を使用します。CLIをインストールした仮想環境に計測用依存を追加してください。

```bash
uv pip install -r components/extensions/tests/requirements-quality.txt
bash components/extensions/cvatctl quality run --suite cli_jobs --python .venv/bin/python --output "$HOME/cvat-quality/current"
```

この機能はクライアント側だけの変更なので、`CVAT_EXTENSIONS`、サーバーの起動・停止・ビルド・確認処理、保存データに対する新しい設定や処理はありません。クライアントを公式版に戻せば独自コマンドは利用できなくなりますが、すでに変更したJobの値は保持されます。
