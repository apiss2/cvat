# UltraSAMで超音波画像にマスクを作成する

[ドキュメントの入口](../README.md)

超音波画像の対象領域を推定するモデル **UltraSAM** を、CVATから使う機能です。対象に含める点と除外する点を指定すると、一つの対象のマスクを生成します。「UltraSAM」は標準のAI Toolsから操作します。矩形による指定や動画の追跡には対応しません。

| 目的 | 文書 |
|---|---|
| 画像にマスクを作成する | このページの操作説明 |
| 配備、停止、問題の調査 | [導入と運用](operations.md) |
| 入出力、座標変換、モデルの接続を変更する | [設計](design.md) |

## 画面で使用する

超音波画像のジョブを開き、AI ToolsのInteractorsからUltraSAM (GPU)を選択します。保存するラベルを選び、対象の内部を左クリックして正例点を置きます。含める領域は左クリック、除外する領域は右クリックで点を追加し、輪郭を確認します。

正例点は1個以上、正例点と負例点の合計は128個までです。標準の操作で結果を確定し、必要な修正後にSaveで保存します。保存後の再読込でラベルと形状を確認してください。推論成功だけで対象の意味や境界の妥当性を判断しないでください。

## 配備と利用条件

専用の画面プラグインとRedisは不要です。CVAT_EXTENSIONSへultrasamを追加し、共通cvatctlで関数を管理します。モデル用のPython、PyTorch、OpenMMLabは関数イメージに含め、CVAT本体やSAMの環境へ追加しません。コードは[Nuclio関数](../../serverless/pytorch/camma/ultrasam/nuclio/)、配備設定は[components/ultrasam](../../components/ultrasam/)です。

モデルの利用前に、[原文の出典と条件](../../serverless/pytorch/camma/ultrasam/NOTICE.md)と[ライセンス本文](../../serverless/pytorch/camma/ultrasam/LICENSE-UltraSAM)を確認してください。第三者のライセンス本文と固有名は原文を保持します。企業での用途を含め、利用条件を確認せずに重みや同梱イメージを配布しないでください。
