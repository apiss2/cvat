# SAM3.1の設計と検証

[SAM3.1の案内](README.md) / [SAM2の共通部分](../sam2/design.md)

## モデルと実行環境

アダプターはSAMのコミット0570b3a5be9c4e694f23d85232fb55f4a6f1f7fcを固定し、SAM3.1のmultiplex trackerと共有の画像バックボーンを使用します。テキスト用とDetection用のヘッドは作成しません。必要な追跡パラメーターが欠ける場合や互換性がない場合は起動を失敗させます。

[ビルド設定](../../components/sam31/build.json)ではPython 3.12、PyTorch 2.10、CUDA 12.8を使用し、コンパイルとFlashAttention 3を無効にしています。PyAV 16.0.1とpsutilはこのモデルの実行に必要な依存関係であり、動画のAV1対応とは別です。ホストには対応ドライバーとコンテナ実行環境、Nuclioに一致するnuctlが必要です。

重みは両関数イメージの/opt/nuclio/sam3.1_multiplex.ptへ同梱し、実行時はHF_HUB_OFFLINE=1です。外部マウントや実行時ダウンロードは使いません。手動の期待ハッシュは要求しませんが、厳密な重みの読み込みと自動の内容識別は行います。

## 画像用関数

画像用のimage_model.pyは、要求ごとに1対象の新しいmultiplex状態を作り、run_mem_encoder=Falseで点と矩形からマスクを生成します。直前の1画像の特徴だけを再利用し、点やマスクの履歴を要求間で共有しません。Redisへ接続せず、CVAT形式のマスクとして穴と分離領域を返します。

## 追跡用関数と状態保存

temporal_video.pyは1〜16の固定された対象を、画像ごとに一括で特徴抽出と追跡へ渡します。初期フレームの記憶、直近のマスク記憶と対象の特徴だけを保持します。CPUテンソルは検証したsafetensorsで保存し、pickleは使いません。

tracker.pyとstate_codec.pyはSAM3.1専用です。共通Redis処理をSAM31の名前空間で使用し、ボリュームとパスワードもSAM2から分離します。更新番号の比較と保存済み応答により、応答消失時の再試行とワーカー再作成に対応します。重み、アダプター、演算精度の違いは古い状態との互換性検査で拒否します。

画像デコード、輪郭変換とRedisの補助コードはSAM2のソースをそのまま共有します。配備時の文字列置換でコードを別モデルへ変換する処理はありません。追跡UIも共有し、マスク入力、逆方向、範囲の終端、選択図形だけの置換とUndoを同じ実装で扱います。詳細は[共通設計](../sam2/design.md)と[追跡操作](../annotation/tracking.md)を参照します。

## 自動試験

チェックアウトのルートから、試験用のPython環境とJavaScript依存関係を使って実行します。

```bash
python -m pytest --noconftest -q components/extensions/tests \
  components/sam31/tests/test_runtime.py \
  components/sam31/tests/test_review_limits.py \
  components/sam31/tests/test_image_parity.py \
  components/sam31/tests/test_mask_seeds.py
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

CPUの試験はモデル、外部コマンド、サービスの代替実装を使います。公式重みでの実GPU推論、実Redis、Dockerのビルド、CVAT全体の型検査とブラウザー操作の代わりにはなりません。

## GPUとブラウザーの受入確認

検証用に配備した関数コンテナで、他の推論を止めてから実行します。コンテナ名は実際に確認した値へ置き換えます。

```bash
docker cp components/sam31/tests/gpu_smoke.py ACTUAL_FUNCTION_CONTAINER:/tmp/gpu_smoke.py
docker exec ACTUAL_FUNCTION_CONTAINER python /tmp/gpu_smoke.py --objects 16 --frames 40
```

GPU試験はモデルの読み込み、有限な出力、記憶を削減した状態の復元と無制限の参照実行との比較、合成画像の点と矩形による推論を確認します。超音波画像での精度評価ではありません。

実Redisの再起動、応答消失時の再試行、同時更新の競合、長い動画と代表的な実画像を別に確認します。ブラウザーではマスクとポリゴンの選択、両方向、中止、Undo、Save後の再読込を確認してください。画像用と追跡用は別のモデルインスタンスを保持するため、SAM2やUltraSAMと同時に起動した場合のGPUメモリも測定します。
