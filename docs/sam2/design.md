# SAM2の設計と検証

[SAM2の案内](README.md) / [共通の追跡操作](../annotation/tracking.md)

このページは推論とプラグインを変更する開発者向けです。モデルの選択と初回配備は[共通手順](../extensions/model-selection.md)を参照します。

## 構成とコードの責務

CVATの標準認証、画像取得、追跡状態の署名、アノテーション保存を再利用します。モデル専用APIやデータベースはCVAT本体へ追加しません。

```text
標準AI Tools -> interactor API -> pth-sam2-interactor -> マスク
追跡プラグイン -> tracker API -> pth-sam2-tracker -> 専用Redis
               <- ポリゴンと署名付き状態
               -> 全範囲の成功後に図形を置換 -> 利用者がSave
```

| 配置 | 責務 |
|---|---|
| serverless/pytorch/facebookresearch/sam2/nuclio/main.py、image_model.py | 画像関数とSAM2ImagePredictorの呼び出し |
| tracker_main.py、redis_tracker.py | 追跡要求、初期化、状態更新 |
| temporal_video.py、state_codec.py | track_step、記憶の保持範囲、CPUテンソルの保存 |
| protocol.py、geometry.py、redis_store.py | 入力検査、マスクとポリゴン、Redisの更新 |
| cvat-ui/plugins/sam2/src/ts/ | 対象選択、範囲、要求、中止、結果の反映 |
| components/sam2/build.json | モデル用の依存関係とビルド設定 |
| components/extensions/functions.py | 関数定義の生成、重みの準備、配備 |

表の短いファイル名は上段のnuclio/内です。関数IDを変更する場合は共通生成処理、アクション定義とテストを合わせます。関数定義は共通生成処理から生成し、個別の関数YAMLを併設しません。SAM3.1と共有するUI、画像デコード、形状変換、Redisの補助コードは、モデル本体と状態の保存形式から分離しています。

## 画像用関数

画像、正例点、負例点と任意の矩形を検証してSAM2ImagePredictor.predictへ渡し、最も高いスコアのマスクをCVATのRLE形式に変換します。同じ画像の特徴は再利用できますが、点やマスクの履歴を要求間で共有しません。

## 追跡要求と保持する記憶

初期要求にはポリゴンまたはマスク、継続要求には直前の署名付き状態を渡します。応答の図形と状態は対象の順序を維持します。対象は1〜16個で固定し、別状態の混在、一部対象だけの継続、古い状態からの分岐を拒否します。

標準tracker APIはNuclioへ元の絶対フレーム番号を渡しません。UIがジョブ範囲、有効なフレーム列、順方向と逆方向を管理し、削除済みフレームを飛ばします。スキップによって指定範囲を広げません。

保存するのは開始フレームと後続処理に必要な直近の記憶です。maskmem_features、maskmem_pos_enc、obj_ptrを中心に、num_maskmem、memory_temporal_stride_for_eval、max_obj_ptrs_in_encoderから保持範囲を決めます。過去画像全体や完全なVideoPredictor状態は保持しません。

テンソルはCPUへ移し、dtypeを保持したsafetensorsとJSONで保存します。復元前に名前、次元、サイズとメタデータを検査し、pickleは使用しません。互換性はモデルのコミット、構成、重みの内容、記憶設定、PyTorchと演算精度などから識別します。内容ハッシュは状態の互換性確認であり、配布元を保証する署名ではありません。

## Redisと再試行

専用Redisのキー接頭辞はcvat:sam2:です。追跡状態、モデル構成、画像寸法、更新番号、直前要求の識別値と保存済み応答を保持します。全対象の推論後にLuaで更新番号を比較して保存し、GPU推論中はRedisの排他ロックを保持しません。

同じ状態と画像の再送には保存済み応答を返し、状態を二重に進めません。初期化は自動再送せず、継続要求は最大3回試行します。既定の有効期限は28,800秒で正常更新時に延長し、保存済み応答の再取得だけでは延長しません。標準APIに解放操作がないため、終了後の状態は有効期限で削除します。

Redisの状態はCVATの保存済みアノテーションではありません。Redisの永続化や再起動後の再試行ができても、閉じたブラウザーの操作を自動再開する機能にはなりません。

## アノテーションへの反映

BaseCollectionActionは開始図形を複製して処理し、全範囲の成功後に元の図形が変更されていないことを再確認します。選択図形の削除と新規トラックの作成を一つの操作として返します。失敗、中止、対象の変更では途中結果を適用しません。

ラベルとグループを保持し、可変属性はフレーム、不変属性はトラックへ移します。見つからない対象と範囲の境界はoutsideで表し、逆方向でも結果はフレーム順に並べます。マスク入力は穴と分離領域を含みますが、追跡結果は最大の外周ポリゴンです。保存は利用者のSave操作で行います。

## 制限と配備上の確認

対象は2Dジョブで、フレーム番号の差は最大10,000です。現在の共通配備では状態の上限は256 MiB、UIが保持する結果は200万座標要素です。対象数や範囲の上限内でも、形状とメモリ量によって失敗する場合があります。既存トラックの延長と追跡途中の対象追加は提供しません。

関数とRedisのポートは外部公開せず、Nuclio dashboardはループバックへ限定します。画像関数と追跡関数は別のモデルを読み込むため、他モデルと併用したGPU使用量を確認します。関数イメージの識別は関数ソース、共通生成処理、モデル選択、重みの配置先、ビルド設定などに基づきます。秘密情報の手動ハッシュを文書へ記録する運用は追加しません。

## 自動試験と実環境の確認

専用のPython環境を用い、チェックアウトのルートで実行します。

```bash
python3.11 -m venv /srv/cvat-test-envs/sam2
/srv/cvat-test-envs/sam2/bin/python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
/srv/cvat-test-envs/sam2/bin/python -m pip install -r components/sam2/tests/requirements.txt
/srv/cvat-test-envs/sam2/bin/python -m pytest components/sam2/tests -q
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

Nodeの試験にはリポジトリのTypeScript依存関係が必要です。小さいCVAT契約用の定義で型と制御を確認し、CVAT全体のビルドやブラウザーの描画は行いません。CPU試験も公式重みのGPU推論を保証しません。

[GPU確認](../../components/sam2/tests/gpu_smoke.py)と[Redis確認](../../components/sam2/tests/redis_integration.py)は実際の対応環境で別に実施します。状態復元、応答消失時の再送、競合、マスク入力、逆方向、削除フレーム、中止、Undo、Save後の再読込を確認します。モデルや状態形式の変更時は、実画像と長い動画の結果も記録してください。
