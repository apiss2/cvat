# タスク説明の取り込みの開発案内

このディレクトリでは、タスク説明をまとめて取り込む処理のテストと入力例を `tests/` で管理します。実装は [task_guide.py](../../cvat/apps/dataset_manager/formats/task_guide.py) と [task_guide_core.py](../../cvat/apps/dataset_manager/formats/task_guide_core.py) にあり、CVAT標準の形式登録を使います。

入力形式と試験の手順は[タスク説明の一括登録](../../docs/extensions/task-guide-import.md)、配置の基準は[文書とテストの保守ルール](../../docs/development.md)を参照してください。
