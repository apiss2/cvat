# 独自拡張の実装案内

[ドキュメントの入口](../docs/README.md)

このディレクトリには、CVAT本体とは分けて管理する独自拡張の配備設定とサービスを配置しています。初回導入は[共通の導入手順](../docs/extensions/installation.md)を参照してください。

| ディレクトリ | 役割 | 説明 |
|---|---|---|
| `extensions/` | 設定の解決と`cvatctl`による一括管理 | [共通運用](../docs/extensions/README.md) |
| `model_registry/` | ONNXモデルの登録、版管理、中継、推論ワーカー | [モデル管理](../docs/model-registry/README.md) |
| `sam2/` | SAM2のビルド定義と専用Redis | [SAM2](../docs/sam2/README.md) |
| `sam31/` | SAM3.1の推論処理、ビルド定義、専用Redis | [SAM3.1](../docs/sam31/README.md) |
| `ultrasam/` | UltraSAMのビルド定義と配備設定 | [UltraSAM](../docs/ultrasam/README.md) |
| `itgformat/` | ITGformat入りバックエンドの起動構成 | [ITGformat](../docs/itgformat/README.md) |

UIの拡張は[cvat-uiのプラグイン](../cvat-ui/plugins/README.md)にあります。SAM2とUltraSAMの推論実装は`serverless/`、ITGformatの変換実装は`cvat/apps/dataset_manager/formats/itgformat/`にあります。
`analytics/`などの上流CVAT由来の構成は、この独自拡張案内の対象外です。
