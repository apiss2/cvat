# 拡張機能の共通運用

[ドキュメントの入口](../README.md)

独自拡張をCVATと同じComposeプロジェクトへ組み込み、cvatctlでビルド、起動、停止を管理します。
初回導入、モデルの選択、導入後の更新、復旧を次の文書に分けています。

| 作業 | 文書 |
|---|---|
| 初めて環境を構築する、機能を有効にする | [導入と共通設定](installation.md) |
| 機能の管理対象、設定変更、追加時の開発手順を確認する | [拡張機能の管理](management.md) |
| 標準AnnotationGuideの背景情報をInfoから参照する | [タスクの背景情報](task-context.md) |
| DICOMの画像と動画をタスクへ取り込む | [DICOM取り込み](../annotation/dicom-import.md) |
| 構造化JSONを各タスクのAnnotationGuideへ反映する | [タスク情報のJSON取り込み](task-guide-import.md) |
| SAM2とSAM3.1の重みを選び、関数を配備する | [モデルの選択と配備](model-selection.md) |
| devで確認した変更を本番へ反映する | [更新手順](update.md) |
| データ、設定、使用イメージを保存して復旧する | [バックアップと復元](backup-restore.md) |
| ONNXモデル管理の接続を設定する | [モデル管理サービスの配備](../model-registry/deployment.md) |

## 機能の選択

`CVAT_EXTENSIONS`には`itgformat`、`sam2`、`sam31`、`ultrasam`、`model_registry`、`frame_navigation_filter`、`selective_ai`、
`filtered_export`、`physical_measurements`、`task_context`、`dicom_import`をカンマで区切って指定します。
SAM3.1は任意の機能です。新規に配備する場合は、互換性のある重みを事前に配置してください。
画面だけの機能も同じ設定で管理します。選択に応じた構成と有効化・無効化の手順は[拡張機能の管理](management.md)を参照してください。

`itgformat`の選択はバックエンドの起動構成と確認を管理します。形式の登録自体はソースのimportで行うため、設定から外しても、同じソースを使うサーバーからITGformatが消えるわけではありません。

## 更新と復旧で共通に守る条件

現在のコードと設定で処理を止めてから、コード、機能選択、保存先を変更します。バックアップにはCVATのデータに加えて、有効な拡張の保存先と秘密情報を含めます。
SAM2とSAM3.1のRedisは別のボリュームです。SAM3.1を使う環境では`SAM31_REDIS_VOLUME`、独立したパスワード、重みの取得元と検証済みイメージも記録してください。Redisの推論状態は、未保存のアノテーションやブラウザーの途中再開を保証するものではありません。

`up --no-build`は、そのコードと設定に対応する既存イメージを使います。新しいモデルを選んだだけではイメージは作成されません。構成確認後も、実際に有効な全機能の受入確認を行ってから利用者へ公開します。
