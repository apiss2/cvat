# 拡張機能の共通管理

設定ファイルの読み込み、Composeの選択、ビルド、起動、停止、関数の配備をcvatctlで管理します。

管理対象と機能追加時の手順は[拡張機能の管理](../../docs/extensions/management.md)を参照してください。

操作、導入条件、制限、検証方法は[機能のドキュメント](../../docs/extensions/README.md)を参照してください。

Projectの既存タスクへアノテーションを取り込む機能は `project_annotations` で選択します。既存インポータとDicomTagによる画像照合の両方を同じ拡張で管理します。入力形式と置換範囲は[Projectへのアノテーション取り込み](../../docs/extensions/project-annotation-import.md)を参照してください。

更新前の保存には`cvatctl backup create --stop`を使用します。初回設定、保存対象と復元方法は[増分バックアップ](../../docs/extensions/incremental-backup.md)を参照してください。
