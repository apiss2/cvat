# 物理長の表示

画像の `DicomTag` にある `PixelSpacing` 属性 `00280030` に基づき、折れ線の長さをmmで表示します。

導入方法、参照する属性、対象範囲、検証手順は[物理長の表示](../../../docs/annotation/physical-measurements.md)を参照してください。

`CVAT_EXTENSIONS` に `physical_measurements` を追加し、共通の `cvatctl` でUIをビルドして起動します。
表示は標準のTagを参照し、手動設定画面、専用API、DB移行、追加サービス、GPUは不要です。
