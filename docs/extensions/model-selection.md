# SAMモデルの選択と配備

[共通運用](README.md) / [SAM2](../sam2/README.md) / [SAM3.1](../sam31/README.md)

このページでは、画像分割と追跡に使うSAM2とSAM3.1のモデルを設定します。画像分割は画像内の対象領域を求める処理、追跡はその領域を連続するフレームへ対応付ける処理です。初回の起動方法は[導入と共通設定](installation.md)を参照してください。

モデルの種類と学習済み重みは、共通管理コマンド `cvatctl` が読む非公開の `.env` で指定します。同じ系列の画像用処理と追跡用処理には同じ重みを使いますが、メモリー上のモデルは別々に読み込みます。

| 選ぶモデル | 必須の準備 | 重みの取得 |
| --- | --- | --- |
| SAM2 | `CVAT_EXTENSIONS` に `sam2` を追加し、生成済みのRedis接続用パスワードを保持します。 | 標準モデルは自動取得します。独自重みを使う場合だけファイルを用意します。 |
| SAM3.1 | `CVAT_EXTENSIONS` に `sam31` を追加し、専用のRedis接続用パスワードと互換重みを用意します。 | 自動取得しません。新規配備時に `SAM31_CHECKPOINT_HOST` の指定が必要です。 |

Redisは追跡途中の状態を保存するサービスです。両モデルで保存ボリュームとパスワードを分けます。GPUなどの共通要件は[導入手順](installation.md#ホストと保存先を準備する)で確認してください。

## SAM2.1のモデルを選ぶ

| SAM2_MODEL | 対応する構成 |
|---|---|
| sam2.1_hiera_tiny | configs/sam2.1/sam2.1_hiera_t.yaml |
| sam2.1_hiera_small | configs/sam2.1/sam2.1_hiera_s.yaml |
| sam2.1_hiera_base_plus | configs/sam2.1/sam2.1_hiera_b+.yaml |
| sam2.1_hiera_large | configs/sam2.1/sam2.1_hiera_l.yaml |

既定はsam2.1_hiera_smallです。空欄でも既定値を使い、未知の名前は拒否します。SAM2_CONFIGとSAM2_CHECKPOINTはモデル名から決まる実行時設定で、別々に上書きする選択項目ではありません。

```dotenv
SAM2_MODEL=sam2.1_hiera_small
SAM2_CHECKPOINT_HOST=
```

共通配備処理は選んだ公式重みを一時的なビルドディレクトリへ取得し、画像用と追跡用の両イメージに同梱します。
学習済みの独自重みを使う場合は、SAM2_MODELで対応する構造を選び、チェックアウト外の空でないファイルへの絶対パスをSAM2_CHECKPOINT_HOSTに指定します。この場合、公式重みは取得しません。

## SAM3.1の重みを配置する

SAM3.1には、互換性のある結合済みsam3.1_multiplex.ptを事前に用意します。正当な配布元から利用条件に従って取得し、Gitと公開イメージレジストリへ無断で登録しないでください。

```dotenv
SAM31_CHECKPOINT_HOST=/opt/models/sam3.1_multiplex.pt
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

sam31をCVAT_EXTENSIONSへ追加し、initまたは既存環境の設定更新でSAM31_REDIS_PASSWORDを準備します。SAM2とは独立した32文字から128文字のランダムなパスワードを使います。既定のボリューム名は`<COMPOSE_PROJECT_NAME>_sam31_redis_data`です。

重みのホストパスはチェックアウト外の絶対パスにします。配備処理は起動前にファイルを検査し、/opt/nuclio/sam3.1_multiplex.ptとして両イメージへコピーします。
重みがなければ失敗し、自動ダウンロードへ切り替えません。SAM3の画像専用重みや単に再梱包したsafetensorsは代用できません。
実行時はHF_HUB_OFFLINE=1で、ホストの重みのマウントや外部取得を行いません。
手動の期待SHA指定は不要ですが、厳密な重み読み込みと自動の内容識別による保存状態の互換性検査は行います。

## 重みの変更と再開

新規操作を止め、現在の設定で停止してから重みの設定を変更します。

```bash
./components/extensions/cvatctl --env-file /srv/cvat-config/cvat.env down
# 同じ設定ファイルのモデル名または重みのパスを変更します。
./components/extensions/cvatctl --env-file /srv/cvat-config/cvat.env up
./components/extensions/cvatctl --env-file /srv/cvat-config/cvat.env check
```

ローカル重みを指定した通常の配備では、同じパスのファイルを差し替えた場合もイメージを再ビルドします。ソース、モデルの選択、ホストのパス、ビルド定義がイメージの識別に関係します。

up --no-buildは対応する既存イメージを使います。重みのパス設定は維持してください。元のホストファイルは不要ですが、後からそのファイルだけを差し替えてもイメージ内の重みは変わりません。必要なイメージがなければ起動前に失敗します。

```bash
./components/extensions/cvatctl up --no-build
```

推論サービスNuclioの `neverBuild` 指定は、重みを持たない状態での意図しない再ビルドを防ぎます。関数コンテナの再開時には変化したIPに合わせてNuclioの経路を更新します。
同じGPUで複数の画像用関数と追跡用関数を動かす場合は、それぞれのモデルが常駐した状態のGPUメモリーを確認してください。

## SAM3.1を明示的に再配備する

同じソースと設定ですでに起動したSAM3.1を再ビルドして配備する場合は、`deploy-sam31` を使用します。この操作には `SAM31_CHECKPOINT_HOST` で指定した元の重みファイルが必要です。新規操作を止め、実行中の推論が完了した後に行ってください。

```bash
# 指定済みの重みファイルを用意してから実行します。
./components/extensions/cvatctl deploy-sam31
# 対象を限定する場合は末尾にimageまたはtrackerを指定します。既定はallです。
```

ソースや設定を変更した場合は、先に上記の `down` と `up` の手順で構成を更新します。既存イメージだけで再開する `up --no-build` と、この再ビルド操作は別の手順です。

## 配備処理を変更する場合

共通処理はcomponents/extensions/functions.pyが担当し、各モデルの依存関係は`components/<モデル>/build.json`に置きます。cvatctlは排他制御、Composeの起動と停止、状態記録を担当します。
SAM2とSAM3.1はprotocol.py、geometry.py、redis_store.pyを同じ内容で利用します。配備時の文字列置換でソースを書き換えません。モデル本体、状態形式、Redisのボリュームとパスワードは別々です。
