# 追加機能の実装案内

[追加機能の一覧](../docs/README.md)

このディレクトリには、CVAT本体と分けて管理するサービスや配備設定を配置しています。利用者向けの操作説明は `docs/`、画面の実装は[画面プラグインの案内](../cvat-ui/plugins/README.md)を参照してください。

## 設定と運用

| 役割 | ディレクトリ | 文書 |
| --- | --- | --- |
| 機能の選択、ビルド、起動、バックアップ | `extensions/` | [導入と運用](../docs/extensions/README.md) |

## データの取り込みと出力

| 役割 | ディレクトリ | 文書 |
| --- | --- | --- |
| DICOMからタスクを作成する | `dicom_import/` | [DICOM取り込み](../docs/annotation/dicom-import.md) |
| 既存タスクのアノテーションを一括更新する | `project_annotations/` | [アノテーション取り込み](../docs/extensions/project-annotation-import.md) |
| 独自形式の変換を含むサーバーを構成する | `itgformat/` | [専用形式の入出力](../docs/itgformat/README.md) |

形式変換の実装は `cvat/apps/dataset_manager/formats/` にあります。タスク説明の一括登録もこの標準の形式登録を使い、仕様は[タスク説明の一括登録](../docs/extensions/task-guide-import.md)で説明しています。

## モデルの登録と実行

| 役割 | ディレクトリ | 文書 |
| --- | --- | --- |
| ONNXモデルの登録、版管理、推論 | `model_registry/` | [モデル登録](../docs/model-registry/README.md) |
| SAM2のビルドと追跡状態の保存 | `sam2/` | [SAM2](../docs/sam2/README.md) |
| SAM3.1の推論、ビルド、追跡状態の保存 | `sam31/` | [SAM3.1](../docs/sam31/README.md) |
| UltraSAMのビルドと配備 | `ultrasam/` | [UltraSAM](../docs/ultrasam/README.md) |

SAM2とUltraSAMの推論実装は `serverless/` にあります。追加機能を変更するときは、[開発方針](../AGENTS.md)と[機能の管理手順](../docs/extensions/management.md)を確認してください。`analytics/` などの上流CVAT由来の構成は、この案内の対象外です。
