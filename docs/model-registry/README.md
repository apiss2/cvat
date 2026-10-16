# ONNXモデル管理

[ドキュメントの入口](../README.md)

Segmentation、Detection、ClassificationのONNXモデルをブラウザーで登録し、CVATから実行する機能です。登録時に試験推論を行い、成功した版を公開します。

## 読む順序

| 読者と目的 | 文書 |
|---|---|
| メンバーがモデルを登録、更新、実行する | [利用手順](usage.md) |
| 技術者がmodel.pyと出力ラベルを作成する | [モデルの実装仕様](model-development.md) |
| 管理者が同じホストまたは別ホストへ導入する | [配備](deployment.md) |
| 管理者が資源、ログ、保存先、復旧を管理する | [保守](operations.md) |
| 開発者がAPI、認証経路、版管理を変更する | [設計とAPI](architecture.md) |
| 管理者が実行権限と情報の保護を確認する | [セキュリティ](security.md) |
| 変更後に試験と受入確認を行う | [検証](testing.md) |

初回のCVAT構築は[共通の導入手順](../extensions/installation.md)から始めます。
タグ条件で対象画像を絞る場合は[選択した画像への自動アノテーション](../annotation/selective-ai.md)を参照してください。

## 実装の配置

サービス、ワーカー、モデル作成用SDKは[components/model_registry](../../components/model_registry/README.md)、CVAT側のリンクと一覧更新は[UIプラグイン](../../cvat-ui/plugins/model-registry/README.md)にあります。
Classificationと部分更新の説明はそれぞれ利用手順、実装仕様、設計とAPIに統合しています。機能追加順ではなく、実施する作業の順に読み進めてください。
