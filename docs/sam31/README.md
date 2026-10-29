# SAM3.1でマスクを作成する

[ドキュメントの入口](../README.md)

点や矩形で指定した対象からマスクを作成し、動画では対象を追跡するモデル **SAM3.1** を、CVATから使う機能です。「SAM3.1」の画像操作は標準AI Tools、追跡操作はSAM2と共通の画面を使用します。

配備先で公式重みを使うGPU試験とブラウザーの受入確認が完了するまでは実験的な機能として扱います。CPUの試験だけでは認識精度、速度、必要なGPUメモリを確認できません。

| 目的 | 文書 |
|---|---|
| 初回に有効化する | [導入と共通設定](../extensions/installation.md) |
| 重みを配置して関数を配備する | [モデルの選択と配備](../extensions/model-selection.md) |
| ポリゴンまたはマスクを追跡する | [SAM2とSAM3.1の共通操作](../annotation/tracking.md) |
| 入出力、状態保存、実機試験を確認する | [設計と検証](design.md) |

## 画像にマスクを作成する

標準AI ToolsでSAM3.1 (GPU, experimental)を選び、正例点、負例点、任意の矩形で対象を指定します。返るのは穴と分離領域を表現できるマスクです。結果を確認して確定し、Saveで保存します。画像用関数はRedisを使いません。

追跡は開始フレームのポリゴンまたはマスクを使用し、順方向と逆方向に対応します。対象数と範囲の上限は[共通操作](../annotation/tracking.md)に記載しています。追跡結果は新しいポリゴントラックであり、穴や最大領域以外の分離領域は保持しません。既存トラックの延長とテキストによるDetectionは提供しません。

## 管理者向けの設定

画像用の関数 `pth-sam31-interactor` と追跡用の関数 `pth-sam31-tracker` を配備します。PythonとCUDA、モデル本体、追跡状態を保存するRedisとパスワードは、SAM2から分離しています。

CVAT_EXTENSIONSにsam31を追加し、互換性のある結合済みsam3.1_multiplex.ptをチェックアウト外へ事前に配置します。SAM31_CHECKPOINT_HOSTはその絶対パスです。現在の構成はSAM3.1の重みを自動取得しません。SAM3の画像専用重みや別形式に詰め直したファイルは代用できません。

```dotenv
SAM31_CHECKPOINT_HOST=/opt/models/sam3.1_multiplex.pt
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

SAM31_REDIS_PASSWORDは共通initで準備する独立した秘密情報を使います。Redisボリュームの既定名は`<COMPOSE_PROJECT_NAME>_sam31_redis_data`で、SAM2と共有しません。画像用と追跡用はupで両方起動します。重みの取得と利用条件は配備担当者が確認し、Gitや公開イメージへ無断で含めないでください。

通常のupはローカル重みをイメージへ同梱します。up --no-buildは対応する既存イメージを使用するため、同じ設定を維持すれば元の重みファイルは不要です。停止、再ビルド、部分的な関数配備の手順は[共通のモデル配備](../extensions/model-selection.md)を参照してください。
