# CVAT v2.81.0 更新時の互換性確認

[運用の案内](README.md) / [更新手順](update.md)

この記録は、2026年10月29日に実施した上流 CVAT の更新と独自機能の点検をまとめたものです。
削除されたモデル情報の参照を1件修正しました。以下に示す他の変更については、独自機能の呼び出し先と設定を確認しました。
本番環境での動作や既存データの移行を保証するものではなく、実施した試験と未実施の確認を末尾に区別しています。

## 確認した範囲

上流の変更は [v2.75.0 から v2.81.0 までの差分](https://github.com/cvat-ai/cvat/compare/v2.75.0...v2.81.0)で確認しました。
独自機能は、作業開始時の `dev` のコミット `dd37a6db` と上流 v2.80.0 の差分から特定し、v2.81.0 を取り込んだ後の実装と照合しました。
リリースノートに加え、REST API、画面側のモデルやアノテーション操作、サーバー内部の入出力処理、設定、データベースの変更を読みました。

| 独自機能の範囲 | 主な確認箇所 |
|---|---|
| データの取り込み | DICOM、プロジェクトのアノテーション、タスクの説明文、ITGformat の登録と取り込み処理 |
| アノテーション操作 | 条件付きエクスポート、選択画像への自動処理、フレーム移動、物理長表示、タスク情報表示 |
| 推論とモデル管理 | ONNX モデル管理、推論要求の中継、SAM2、SAM3.1、UltraSAM の画面側とサーバー側の接続 |
| 導入と運用 | このフォークの管理コマンド `cvatctl` による構成合成、ワーカーの起動条件、停止中のバックアップ、上流のデータベース移行 |

## 公開 API と画面側の変更

| 上流の変更 | 独自機能での使用と判定 | 根拠 |
|---|---|---|
| v2.81.0 で `MLModel.version` を削除 | **修正済みです。** モデル一覧の更新処理が削除されたプロパティを参照していました。更新判定をモデルの ID と名称に変更しました。モデル管理サービスは版を更新するたびに ID を変えるため、更新とロールバックも検出できます。 | [上流 PR #11329](https://github.com/cvat-ai/cvat/pull/11329)、[現行のモデル型](../../cvat-core/src/ml-model.ts)、[修正箇所](../../cvat-ui/plugins/model-registry/src/ts/index.tsx)、[版ごとの ID 生成](../../components/model_registry/registry/schema.py) |
| v2.80.0 で `consensus_replicas` を非推奨にし、`initial_replicas` を追加。ジョブの種類を `replica` に変更し、`consensus_enabled` などを応答から削除 | 独自機能から旧名称を送信、参照する箇所は見つかりませんでした。DICOM のタスク作成は本体の作成処理を使います。プロジェクトの取り込みは通常のアノテーションジョブと検証用ジョブを明示しており、旧名称に依存していません。 | [v2.80.0](https://github.com/cvat-ai/cvat/releases/tag/v2.80.0)、[DICOM の登録](../../components/dicom_import/service.py)、[取り込み対象の選択](../../components/project_annotations/service.py) |
| v2.77.0 で一覧取得の `page_size=all` を削除 | 独自機能での使用は見つかりませんでした。選択画像への自動処理は `page_size=100` とページ番号を使い、件数と次ページの有無を確認しています。 | [上流 PR #11188](https://github.com/cvat-ai/cvat/pull/11188)、[画像選択処理](../../cvat-ui/plugins/selective-ai/src/ts/engine.ts) |
| v2.78.0 で画面側の `annotations.bulkSave` を `saveStates` に変更。v2.81.0 で推論操作の一時保留と旧形式のキャンバスヒントを削除 | 独自プラグインに `bulkSave`、`toolsBlockerState`、`CanvasHint` の使用は見つかりませんでした。SAM の追跡や物理長表示が使う既存の取得、推論、保存用メソッドは残っています。 | [上流 PR #11084](https://github.com/cvat-ai/cvat/pull/11084)、[上流 PR #11362](https://github.com/cvat-ai/cvat/pull/11362)、[画面側の共通 API](../../cvat-core/src/session.ts)、[独自プラグイン](../../cvat-ui/plugins/README.md) |
| v2.81.0 で複製ジョブのフレーム削除と復元を禁止 | 独自プラグインから該当するフレーム削除、復元 API を呼ぶ箇所は見つかりませんでした。 | [上流 PR #11344](https://github.com/cvat-ai/cvat/pull/11344)、[画面側の共通実装](../../cvat-core/src/session-implementation.ts) |
| v2.80.0 で Python SDK と CLI の Python 3.10 対応および `typing_extensions` 依存を削除。v2.81.0 で `python-dateutil` 依存を削除 | 独自機能から `cvat_sdk`、`dateutil`、`typing_extensions` を読み込む箇所は見つかりませんでした。UltraSAM の Python 3.10 は独立した推論コンテナーの実行環境で、CVAT SDK を使っていません。 | [v2.80.0](https://github.com/cvat-ai/cvat/releases/tag/v2.80.0)、[v2.81.0](https://github.com/cvat-ai/cvat/releases/tag/v2.81.0)、[UltraSAM の構成](../../components/ultrasam/build.json) |

モデル管理の認証については、CVAT の既存ログインセッションを使い、`/api/users/self` で利用者を確認する実装を読みました。
この確認範囲では、更新に伴って非推奨になった認証方式への依存は見つかりませんでした。
モデル管理サービス間で使う Bearer トークンは独自サービス間の認証であり、CVAT の認証トークンを置き換えるものではありません。
実装は[認証処理](../../components/model_registry/registry/auth.py)と[推論要求の中継](../../components/model_registry/registry/gateway.py)を参照してください。

## サーバー内部の処理と設定

サーバー側の独自機能は、本体の取り込みや出力処理を再利用しています。
これらは REST API と異なり内部実装への依存を含むため、非推奨指定の有無だけでなく、引数、継承先、完了時の処理も照合しました。

| 上流の変更 | 独自機能での使用と判定 | 根拠 |
|---|---|---|
| v2.76.0 で非同期要求の利用者情報を拡張し、メタデータ生成を `request` 引数へ変更 | 独自処理が旧 `user` と `uuid` 引数でメタデータを生成する箇所は見つかりませんでした。DICOM とプロジェクトの取り込みは、本体で作成された `ImportRQMeta` を読みます。 | [上流 PR #11106](https://github.com/cvat-ai/cvat/pull/11106)、[本体の要求情報](../../cvat/apps/engine/rq.py)、[DICOM の登録](../../components/dicom_import/service.py) |
| v2.77.0 で入出力ジョブの自動再試行を追加し、旧出力再試行設定を無効化 | 条件付きエクスポートは現行の `DatasetExporter` を継承しています。プロジェクトのアノテーション取り込みはタスクごとに確定するため、追記の重複を防ぐ目的で `job_retry=None` としています。DICOM の取り込みは本体のプロジェクト取り込みトランザクション内で処理します。旧 `CVAT_EXPORT_LOCKED_RETRY_INTERVAL` とその別名の使用は見つかりませんでした。 | [v2.77.0](https://github.com/cvat-ai/cvat/releases/tag/v2.77.0)、[条件付き出力](../../cvat/apps/dataset_manager/filtered_export/views.py)、[プロジェクト取り込み](../../components/project_annotations/background.py)、[本体の取り込み処理](../../cvat/apps/dataset_manager/project.py) |
| v2.77.0 で取り込み後の入力ファイル削除を完了通知へ移行 | 独自の `ProjectAnnotationImporter` は、継承元だけに登録された通知処理が自動では呼ばれない点に対応済みです。成功と失敗の通知を受け、本体の削除処理を呼びます。削除された旧関数の使用は見つかりませんでした。 | [上流 PR #11154](https://github.com/cvat-ai/cvat/pull/11154)、[独自の完了通知処理](../../components/project_annotations/background.py)、[本体の入力ファイル削除](../../cvat/apps/engine/request_handlers.py) |
| v2.77.0 以降にデータ表現を追加し、v2.78.0 以降に入力ファイルのパス検査を強化 | 独自の ITGformat と条件付きエクスポートが利用する `TaskData`、`ProjectData`、`GetCVATDataExtractor`、`import_dm_annotations` は残っています。削除された `format_for` などの使用は見つかりませんでした。ITGformat は独自の相対パス検査も実施しています。実ファイルの入出力確認は別途必要です。 | [本体の形式変換](../../cvat/apps/dataset_manager/bindings.py)、[ITGformat の接続処理](../../cvat/apps/dataset_manager/formats/itgformat/adapter.py)、[Datumaro の変更](https://github.com/cvat-ai/datumaro/compare/6ade96fa82043fc5bccd59a27218892899fe10ce...335fc3e55fe699b9401091f6af11f10c3b0e08cb) |
| v2.77.0 で動画も既定の外部保存先へ自動移動するよう変更 | 独自 DICOM 取り込みはローカル保存を前提とし、既定の外部保存先が設定されている場合は登録前に拒否します。この制限を迂回する変更はしていません。 | [上流 PR #11169](https://github.com/cvat-ai/cvat/pull/11169)、[DICOM の登録条件](../../components/dicom_import/service.py) |
| v2.77.0 で入出力ワーカーにスケジューラーを追加 | 独自 Compose 構成はワーカーの起動コマンドを置き換えておらず、上流の `--with-scheduler` を引き継ぎます。v2.81.0 の標準 Compose の変更は CVAT イメージの版で、データベースサービスのイメージと保存先は v2.80.0 から変わっていません。 | [標準 Compose](../../docker-compose.yml)、[独自 Compose](../../components/extensions/docker-compose.extensions.yml) |
| v2.76.0 以降の Helm 用データサービス変更 | `cvatctl` は Docker Compose を使用し、Helm チャートを使用しません。Helm 用 Redis、PostgreSQL、イメージ取得先の変更を、独自バックアップの保存先変更とは扱っていません。 | [上流 PR #11160](https://github.com/cvat-ai/cvat/pull/11160)、[上流 PR #11190](https://github.com/cvat-ai/cvat/pull/11190)、[上流 PR #11378](https://github.com/cvat-ai/cvat/pull/11378)、[構成管理](../../components/extensions/manage.py) |

## データベースとバックアップ

データベースについては、v2.75.0 から v2.81.0 の間に上流の移行処理があります。
主な変更は、Webhook の種類、利用者プロフィール、複製ジョブの名称、タスクの `initial_replicas`、品質確認のジョブ条件、既存タスクの合意形成設定です。
非同期処理を保存する Redis にも、利用者情報と取り込みジョブの実行関数を更新する移行処理があります。

今回の直接の更新元である v2.80.0 と v2.81.0 の間には、データベースと Redis の移行ファイルの追加、変更はありませんでした。
独自機能による CVAT 本体のモデル定義や移行ファイルの追加もありません。
確認箇所は [engine の移行](../../cvat/apps/engine/migrations/)、[合意形成設定の移行](../../cvat/apps/consensus/migrations/)、[品質確認設定の移行](../../cvat/apps/quality_control/migrations/)、[Webhook の移行](../../cvat/apps/webhooks/migrations/)、[Redis の移行](../../cvat/apps/engine/redis_migrations/)です。

独自バックアップは、停止した構成の実際の保存領域と設定を記録して保管するため、タスクの個々の列名を解釈して保存する方式ではありません。
この点では上記の列名変更に直接依存しませんが、復元したデータを旧コードで読めることまで意味しません。
旧版へ戻す場合は、記録したコード、イメージ、設定、データを一組で復元する必要があります。
根拠は[バックアップ実装](../../components/extensions/backup.py)と[復旧手順](backup-restore.md#更新に失敗した場合の復旧)を参照してください。

## 実施した試験と確認の限界

モデル管理プラグインについて、試験用の型定義から削除済みの `version` を除くと、修正前の実装が TypeScript の `TS2339` で失敗することを確認しました。
修正後は型検査と Node.js の試験が成功しました。
追加した試験では実際のプラグインを読み込み、版更新、ロールバック、削除、名称変更、順序変更、同じ内容を再取得した場合の動作を確認しています。
`version` を参照すると試験が失敗するようにして、古い型定義が不整合を隠していた状態も修正しました。

既存のモデル管理試験については、構成の例に残っていた旧設定を現行の `CVAT_EXTENSIONS` に修正しました。
また、独自の証明書を使う試験は、実行環境が最初から登録する証明書を考慮し、初期の証明書集合と指定した証明書だけが登録されることを厳密に確認する形にしました。
どちらも更新前から同じだった試験と実装の確認で見つかった問題です。TLS の実装は変更せず、証明書検証とホスト名検証が有効であることの確認も維持しています。
修正後は、該当試験だけでなくモデル管理全体の試験も再実行しました。

| 実施した確認 | 結果 |
|---|---|
| 拡張管理、バックアップ処理、ITGformat の Python 試験 | 622件成功、12件スキップ。別途12件のサブテストも成功しました。 |
| DICOM、プロジェクトのアノテーション取り込み、条件付きエクスポートの Python 試験 | 310件成功。別途4,763件のサブテストも成功しました。 |
| モデル管理の Python 試験 | 367件成功、3件スキップ。 |
| タスク説明の取り込み試験 | 79件成功。 |
| 実際の CVAT モデルと標準インポータを使う SQLite 結合試験 | 28件成功。DICOM 情報の行ID保持、置換と追記、途中失敗時のロールバック、入力ファイルの削除を確認しました。 |
| 画面操作とモデル管理画面の Node.js 試験 | 246件成功。 |
| モデル一覧の更新と追跡プラグインの Node.js 試験 | それぞれ11件と66件が成功し、各試験用の型検査も成功しました。 |
| UI 全体の TypeScript 型検査 | 成功しました。試験用の型定義だけでなく、v2.81.0 の実際の型で独自プラグインを確認しました。 |
| 独自の画面プラグイン10個を含む UI の本番用ビルド | 成功しました。警告2件は成果物の容量に関するもので、コンパイルエラーはありませんでした。 |
| 設定例と文書 | 設定キー34個と値を維持し、文書30ファイルの相対リンク298件が解決することを確認しました。`git diff --check` も成功しました。 |

Python 試験は Python 3.12 の隔離環境で実施しました。サーバーの結合試験では、固定された Datumaro のコミットと CVAT 本体の依存関係を使用しました。
画面側は `yarn.lock` を変更せずに依存関係を導入し、次の全体検査とビルドを実施しました。

```bash
yarn tsc -p cvat-ui/tsconfig.json --noEmit
CLIENT_PLUGINS=plugins/sam2:plugins/sam31:plugins/model-registry:plugins/selective-ai:plugins/filtered-export:plugins/frame-navigation-filter:plugins/physical-measurements:plugins/dicom-import:plugins/project-annotations:plugins/task-context \
  yarn run build:cvat-ui
```

スキップした試験は、実 Docker のバックアップ復元1件、別配布の ITGformat 用インストーラーとサンプル10件、外部の形式参照実装1モジュール、ONNX 実行環境を必要とする3件です。
モデル管理試験で出た非推奨警告は、試験用の Starlette クライアントが AnyIO の別名を参照するものでした。独自機能が呼び出す CVAT 本体の API とは区別しています。

この確認には、実 GPU による推論、ブラウザーを操作する受入試験、稼働中 CVAT と接続した入出力、既存本番データの移行、実保存領域へのバックアップと復元は含まれません。
SQLite の結合試験では、PostgreSQL の行ロック、実 Redis ワーカー、権限判定サービスを接続した動作は確認していません。
内部 API は将来も変更される可能性があるため、次の上流更新でも同じ依存箇所を点検し、[更新手順](update.md)の受入確認を行います。
