# 上流CVATのSAMプラグイン

[UIプラグインの案内](../README.md)

このディレクトリは、上流CVAT由来のSAM用クライアント処理です。対応するSAM関数の出力を処理し、ブラウザーのワーカーでデコーダーを実行します。
独自拡張のSAM2とSAM3.1の追跡プラグインとは別です。追跡操作は[共通の追跡操作](../../../docs/annotation/tracking.md)を参照してください。

`src/ts/index.tsx`が登録と要求処理、`src/ts/inference.worker.ts`が推論ワーカー、`assets/decoder.onnx`がデコーダーです。
このプラグインの有効化だけで、SAM2やSAM3.1の関数が配備されることはありません。
