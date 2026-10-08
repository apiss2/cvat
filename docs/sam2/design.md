# SAM2の設計とコード構成

## 責務の分担

構成は、画像用Nuclio関数、動画用Nuclio関数、動画状態用の専用Redis、CVATのUIプラグインに分かれます。CVAT標準の認証、画像取得、状態の署名、アノテーション保存を再利用し、CVAT本体へ専用APIやデータベース変更を追加しません。

```text
画像分割
CVAT標準AI Tools
  -> 既存interactor API
  -> pth-sam2-interactor
  -> SAM2ImagePredictor
  -> CVAT形式のマスク

動画追跡
SAM2 UIプラグイン
  -> 既存tracker API
  -> pth-sam2-tracker
     -> Redisから推論状態を読み出す
     -> CPU状態を復元して必要なテンソルだけGPUへ移す
     -> 現在画像の特徴を計算する
     -> 物体ごとにSAM2のtrack_step()を呼ぶ
     -> 必要な記憶をCPUへ移してRedisへ保存する
  <- ポリゴンと署名付き状態
  -> 全フレーム成功後にポリゴントラックをUIへ反映
  -> 利用者が標準Save操作でサーバへ保存
```

Redisに保存する「推論状態」と、CVATサーバへ保存する完成済みアノテーションは別物です。Redisからアノテーションを直接変更しません。

## 配置と責務

```text
serverless/pytorch/facebookresearch/sam2/
  nuclio/main.py、image_model.py       画像分割関数
  nuclio/tracker_main.py               動画関数の初期化
  nuclio/redis_tracker.py               tracker要求と状態更新
  nuclio/temporal_video.py              track_stepと記憶の保持範囲
  nuclio/state_codec.py                 CPUテンソルとJSONの直列化
  nuclio/redis_store.py                 RedisのCAS更新と有効期限
  nuclio/protocol.py、geometry.py       要求検証とマスク、ポリゴン変換
  nuclio/model_common.py                GPU確認とモデル設定
  nuclio/*-gpu.yaml                     Nuclio関数と依存イメージ
  deploy.sh                            共通管理コマンドへの入口
  tests/                                 単体、実GPU、実Redisテスト

cvat-ui/plugins/sam2/
  src/ts/action.ts                      注釈アクションと結果の反映
  src/ts/tracking.ts                    フレーム列挙、要求、再試行、中断
  tests/                                 UI制御とCVAT型契約のテスト

components/sam2/docker-compose.sam2.yml
  SAM2 UIビルドとdashboard経由の呼出し設定
components/sam2/docker-compose.redis.yml
  認証付き専用Redisと永続ボリューム
components/extensions/
  統一起動、停止状態記録、外部ネットワーク管理
```

関数IDは `pth-sam2-interactor` と `pth-sam2-tracker` です。関数IDを変更する場合は、Nuclio YAML、`src/ts/action.ts`、テストfixtureを同時に変更します。

## 画像分割

`pth-sam2-interactor` は既存interactor APIから `image`、`pos_points`、`neg_points`、`obj_bbox` を受け取ります。入力を検証して `SAM2ImagePredictor.predict()` へ渡し、スコアが最大のマスクをCVATのRLE形式へ変換して `shapes` として返します。画像特徴は同じ画像への要求に限って関数プロセス内で再利用できますが、点やマスクの履歴を要求間で共有しません。

## 動画要求と状態

UIプラグインは `core.lambda.call()` を使います。ブラウザからNuclioへ直接接続せず、CVATサーバが画像取得と署名検証を行います。

初期化要求には開始ポリゴンを渡し、動画関数は初期条件の推論状態を作成します。継続要求には直前の署名付き状態を渡します。`shapes` と `states` は入力物体と同じ順序で返します。1回の追跡単位は最大4物体で、別の追跡状態との混在、一部物体だけの継続、二つ以上前の状態からの分岐は拒否します。

標準tracker APIはNuclioへ絶対フレーム番号を渡さないため、UIが有効なフレーム列とジョブ範囲を管理します。削除済みフレームは飛ばしますが、指定範囲を延長しません。

## 保存する記憶

保存対象は初期条件フレームと、後続推論が参照する直近の記憶です。主に `maskmem_features`、`maskmem_pos_enc`、`obj_ptr` を保持し、過去画像や完全なVideoPredictor状態は保存しません。

保持範囲はSAM2の `num_maskmem`、`memory_temporal_stride_for_eval`、`max_obj_ptrs_in_encoder` から決めます。マスク記憶の範囲外でも物体特徴が必要なフレームでは、物体特徴だけを残します。テンソルはCPUへ移してdtypeを保ち、safetensorsとJSONで保存します。復元前にサイズ、名前、次元、メタデータを検証し、pickleは使用しません。

状態の互換性は、SAM2のコミット、モデル設定と重みの内容ハッシュ、記憶設定、PyTorch版、演算精度などから識別します。異なるモデル構成のworkerは状態を引き継げません。重みの内容ハッシュは互換性確認用であり、配布元の真正性を保証する署名ではありません。

## Redisと競合制御

追跡IDごとにRedisのハッシュを作成します。ハッシュには推論状態、モデル構成、画像寸法、更新番号、直前要求の識別値、保存済み応答を保持します。Redisのキー接頭辞は `cvat:sam2:` です。

全物体の推論が完了した後、Luaスクリプトで更新番号を比較して保存します。競合した更新は一方だけを確定し、GPU推論中にRedisの排他ロックは保持しません。同じ状態と画像の再送には保存済み応答を返し、状態を二重に進めません。初期化要求は自動再送せず、UIの継続要求は最大3回まで再試行します。

有効期限は既定28800秒で、正常な更新時に延長します。保存済み応答を再取得するだけでは延長しません。標準tracker APIに解放操作がないため、完了、中断後の状態はTTLで削除します。

## UIへの反映

プラグインは `BaseCollectionAction` として登録します。推論中は開始図形のスナップショットだけを参照し、処理範囲全体が成功した後に開始図形が変更されていないことを再確認します。その後、開始図形の削除と新しいポリゴントラックの作成を一つの注釈アクションとして返します。中断、失敗、開始図形の変更時は空の変更を返し、途中結果を反映しません。

可変属性は各フレーム、不変属性はトラックへ引き継ぎます。対象物が見つからないフレームは `outside` として扱い、指定範囲の終端を明示します。利用者がSaveするまでサーバへの保存は完了しません。

## 制限と変更時の注意

- 2Dジョブのみ、1回あたり1〜4物体
- 前方追跡のみ、開始フレームから最大1000フレーム先
- 追跡状態の上限は64 MiB、1回の結果は200万座標要素
- 逆方向追跡、既存トラックの延長、追跡途中の再指定は非対応
- ブラウザを閉じた後の途中再開は非対応

SAM2、PyTorch、モデル重み、状態形式を変更する場合は、状態の互換性識別子と `tests/gpu_smoke.py` を更新します。Redis設定を変更する場合は、認証、AOF、`noeviction`、外部ポート非公開の前提を維持します。Nuclio dashboardはホストのループバックアドレスだけに公開し、関数ポートとRedisを外部公開しません。

## 運用構成との接続

CVATのAPIとannotation workerは、同じ名前空間のNuclio dashboard経由で関数を呼び出します。関数はHTTPポートをホストへ公開せず、共有Dockerネットワーク内で応答します。dashboardはループバックだけに公開します。この指定は既存の標準Nuclio関数にも適用されるため、更新時にはその呼出しも確認します。

起動時に生成する関数イメージの識別値は、関数ソース、管理コード、Nuclio版、ネットワーク、名前空間、Redisの認証設定を含みます。CVATだけを更新しても関数の構成が同じなら再利用します。モデルを変更した場合の追跡状態互換性は、推論側の識別子で別途検証します。運用は[共通手順](../extensions/README.md)を参照してください。
