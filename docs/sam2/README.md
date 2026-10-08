# SAM2の機能と導入

## 目的と対象

この拡張は、CVAT CommunityにSAM2の画像分割と動画追跡を追加します。画像と動画のNuclio関数、追跡状態を保存する専用Redis、動画操作用のUIプラグインに分け、CVAT本体へモデルや専用APIを組み込みません。

## 利用できる機能

画像分割では、標準AI Toolsから`SAM2 (GPU)`を選び、正例点、負例点、矩形からマスクを生成します。関数IDは`pth-sam2-interactor`です。

動画追跡では、注釈アクション`SAM2: track polygon shapes`を実行し、現在フレームのポリゴン図形を前方へ追跡します。関数IDは`pth-sam2-tracker`です。全処理が成功した後にポリゴントラックへ置き換え、標準のUndoとSaveを使用します。動画機能は既存の注釈アクション登録先を維持しており、AI Toolsの表示構成を変更する機能は含めません。

## ファイルの配置

```text
serverless/pytorch/facebookresearch/sam2/nuclio/  推論と状態管理
serverless/pytorch/facebookresearch/sam2/tests/   単体試験、GPU確認、Redis確認
cvat-ui/plugins/sam2/                            動画追跡UIと試験
components/sam2/                                UIと専用RedisのCompose
```

状態の保存形式、競合時の動作、UI反映の順序は[設計](design.md)に記載しています。推論状態はCVATのアノテーションとは別であり、Redisへ保存しただけでは利用者の注釈保存は完了しません。

## 導入と運用

導入は[共通運用手順](../extensions/README.md)に従います。共通の設定ファイルで`CVAT_EXTENSIONS`に`sam2`を含めます。管理コマンドは有効機能からUIプラグインを選び、公式`Dockerfile.ui`でビルドします。

```bash
./components/extensions/cvatctl up
./components/extensions/cvatctl check
```

更新は[更新手順](../extensions/update.md)に従います。`up`は必要なときに画像関数と追跡関数をデプロイします。停止ではdashboardだけでなく関数コンテナも対象にします。

## 確認方法

単体試験はNuclioの`tests`とUIプラグインの`tests`で実行します。
CPUでの単体試験には専用のPython環境を使います。

```bash
python3.11 -m venv /srv/cvat-test-envs/sam2
/srv/cvat-test-envs/sam2/bin/python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
/srv/cvat-test-envs/sam2/bin/python -m pip install -r serverless/pytorch/facebookresearch/sam2/tests/requirements.txt
/srv/cvat-test-envs/sam2/bin/python -m pytest serverless/pytorch/facebookresearch/sam2/tests -q
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

UI試験には、完全なCVATチェックアウトに依存関係をインストールし、`tsc`へPATHを通したNode.js環境を使います。
CPUの単体試験はSAM2のモデル重みを使用した実推論を保証しません。

`cvatctl check`は、UIの実イメージ、関数の登録と稼働、外部ポート非公開を調べます。これは実GPUの推論結果を保証する試験ではありません。

実GPU、実Redis、画面操作で画像分割と動画追跡を確認します。特に、キャンセル時の不変性、全処理成功後の置換、Undo、Save後の再読込を確認します。

## 制限と保守対象

動画機能は2Dジョブ、1回あたり1から4物体、開始フレームから最大1000フレーム先の前方追跡を対象にします。入力はポリゴン図形です。既存トラックの延長、逆方向追跡、ブラウザを閉じた後の途中再開は対象外です。

SAM2、PyTorch、モデル重み、状態形式を変更した場合は、保存状態の互換性を確認します。関数のHTTPポートと専用Redisはホストへ公開せず、CVATからdashboard経由で呼び出します。dashboardの管理用ポートはループバックに限定します。モデル取得時の内容ハッシュは記録しますが、配布元が提示した既知のハッシュとの照合は現実装に含まれません。
