# 画面プラグインの案内

[追加機能の一覧](../../docs/README.md) / [導入と共通設定](../../docs/extensions/installation.md)

このディレクトリには、CVATのメニューや作業画面に機能を追加するコードを配置しています。以下は開発者が実装と操作説明を探すための一覧です。

## データと説明の取り込み

| 役割 | ディレクトリ | 操作説明 |
| --- | --- | --- |
| DICOMの画像と動画を登録する | `dicom-import/` | [DICOM取り込み](../../docs/annotation/dicom-import.md) |
| 既存タスクのアノテーションを一括更新する | `project-annotations/` | [アノテーション取り込み](../../docs/extensions/project-annotation-import.md) |
| タスクに保存した作業説明を表示する | `task-context/` | [タスク情報の表示](../../docs/extensions/task-context.md) |

## 画像の確認と出力

| 役割 | ディレクトリ | 操作説明 |
| --- | --- | --- |
| 他の図形を表示したまま条件で画像を移動する | `frame-navigation-filter/` | [フレーム移動](../../docs/annotation/frame-navigation-filter.md) |
| 画素間隔から折れ線の長さを表示する | `physical-measurements/` | [長さの表示](../../docs/annotation/physical-measurements.md) |
| 条件に合う画像を出力する | `filtered-export/` | [条件付きエクスポート](../../docs/annotation/filtered-export.md) |

## モデルの利用

| 役割 | ディレクトリ | 操作説明 |
| --- | --- | --- |
| モデル管理画面を開き、モデル一覧を更新する | `model-registry/` | [モデル登録](../../docs/model-registry/README.md) |
| 対象画像を選んで自動アノテーションする | `selective-ai/` | [画像を選んで実行](../../docs/annotation/selective-ai.md) |
| SAM2で図形を追跡する | `sam2/` | [動画の追跡](../../docs/annotation/tracking.md) |
| SAM3.1で図形を追跡する | `sam31/` | [動画の追跡](../../docs/annotation/tracking.md) |
| 上流CVATのSAMを実行する | `sam/` | [SAMプラグイン](sam/README.md) |

## 有効化と再ビルド

独自プラグインは共通設定の `CVAT_EXTENSIONS` で選びます。設定名はディレクトリ名と異なる場合があるため、[機能の管理一覧](../../docs/extensions/management.md)を参照してください。管理コマンドが必要なパスをUIビルドへ追加するため、管理対象のプラグインを `CVAT_CLIENT_PLUGINS` へ手動追記しません。

変更後は[更新手順](../../docs/extensions/update.md)に従って再ビルドし、起動します。DICOMやアノテーションの取り込みなど、サーバー側の処理もある機能はサーバーとワーカーを同じ版へ更新してください。無効にする場合も、機能の選択を外して再ビルドします。ソースや設定を編集しただけでは配備済みの画面に反映されません。

一覧にない独自パスは `CVAT_CLIENT_PLUGINS` で指定できます。UIを直接開発する場合のビルド変数は `CLIENT_PLUGINS` です。

## CVAT本体との接続

フレーム移動のプラグインは、このフォークの `cvat-core/src/plugins.ts` にある引数差し替え機能 `methodArguments` と組み合わせて使用します。タスク情報のプラグインは、Job応答に追加した読み取り専用項目 `task_guide_id` を使います。どちらも未変更の上流CVATへ画面のコードだけをコピーする構成には対応しません。接続の仕様は各操作説明の開発者向けの節を参照してください。

## テストと文書を編集する

独自プラグインのテストと補助コードは、そのプラグインの `tests/` に置きます。サーバー側のPythonテストは `components/<機能>/tests/` で管理します。実行方法は各機能の文書、配置と文書の更新基準は[文書とテストの保守ルール](../../docs/development.md)を参照してください。
