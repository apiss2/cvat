# タスク説明の取り込みの開発案内

このディレクトリでは、タスク説明の逐次読み込みと一時保存を実装し、テストと入力例を `tests/` で管理します。JSONの解析は `streaming_json.py`、本文の一時保存は `staging.py` に分けています。CVAT標準の形式登録と保存への接続は [task_guide.py](../../cvat/apps/dataset_manager/formats/task_guide.py)、入力の検証と照合は [task_guide_core.py](../../cvat/apps/dataset_manager/formats/task_guide_core.py) にあります。

入力形式と試験の手順は[タスク説明の一括登録](../../docs/extensions/task-guide-import.md)、配置の基準は[文書とテストの保守ルール](../../docs/development.md)を参照してください。
