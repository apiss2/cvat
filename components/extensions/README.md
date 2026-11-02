# 追加機能の設定と起動

このディレクトリには、CVATと追加機能をまとめて設定、ビルド、起動、停止するコマンド **cvatctl** があります。必要な機能を設定ファイルで選ぶと、「cvatctl」が必要な画面とサービスを組み合わせます。

| 目的 | 読む文書 |
| --- | --- |
| 初めて導入する | [導入と共通設定](../../docs/extensions/installation.md) |
| 使う機能を選ぶ | [機能の選択と管理](../../docs/extensions/management.md) |
| 設定値を確認する | [設定例](config.example.env) |
| 既存環境を更新する | [更新手順](../../docs/extensions/update.md) |
| 更新前にデータを保存する | [増分バックアップ](../../docs/extensions/incremental-backup.md) |
| 管理処理を開発する | [機能追加の手順](../../docs/extensions/management.md#機能を追加するときの手順) |
| 独自コードの試験と品質を計測する | [品質計測の手順](../../docs/development.md#独自コードの品質を計測する) |

管理の実装は [manage.py](manage.py)、機能の登録は [catalog.py](catalog.py)、モデルの配備は [functions.py](functions.py)、バックアップは [backup.py](backup.py)、開発時の品質計測は [quality.py](quality.py) にあります。操作全般は[共通運用](../../docs/extensions/README.md)を参照してください。
