# UltraSAMの導入と運用

[UltraSAMの案内](README.md) / [共通の導入手順](../extensions/installation.md)

## 導入と起動

NVIDIA GPU、対応ドライバーとNVIDIA Container Toolkit、Nuclio dashboardと同じ版のnuctlを用意します。初回ビルドは公式ソース、PyTorch、OpenMMLabとモデル配布元へ接続します。他のモデルと同じGPUを使う場合は、同時起動時のメモリと応答時間を検証します。

既存環境では旧コードと設定で停止してから、.envのCVAT_EXTENSIONSにultrasamを追加します。既存の機能名と秘密情報は維持してください。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry
ULTRASAM_GPU_DEVICE=0
```

ULTRASAM_GPU_DEVICEは1個のGPU番号またはUUIDです。関数のCUDA_VISIBLE_DEVICESに反映しますが、Nuclioがコンテナへ公開するGPUを隔離する設定ではなく、GPUの専有も保証しません。

ソースを確定してコミットし、完全なチェックアウトのルートで[共通手順](../extensions/installation.md#ビルドして起動する)のビルドと起動を行います。buildはCVATなどのイメージを準備し、upが必要な関数イメージをビルドして配備します。初回取得した重みは関数イメージ内へ同梱します。

同じソースと設定で関数だけを再配備する場合は次を実行します。allも同じ画像関数を対象にし、trackerは受け付けません。

```bash
./components/extensions/cvatctl deploy-ultrasam image
./components/extensions/cvatctl check
```

ソース変更後は保存済み構成と一致しないため、旧構成で停止し、コミット後に通常のupを使って再構築します。対応イメージがないままup --no-buildを実行しても依存関係は更新されません。本番反映は[更新手順](../extensions/update.md)に従い、検証済みの実イメージを移送します。

## 停止と無効化

通常はcvatctl downで関数も停止し、同じ設定でup --no-buildにより再開します。無効化は停止後にCVAT_EXTENSIONSからultrasamだけを外して再開します。GPUの変更も停止後に行います。設定ファイルの編集だけでは稼働構成は変わりません。CVATへ保存したアノテーションは保持します。

## ビルドと実行環境の記録

関数定義は[共通生成処理](../../components/extensions/functions.py)が[ビルド設定](../../components/ultrasam/build.json)から生成します。個別の関数YAMLを併設せず、このビルド設定を更新します。

公式ソースはff3157b1fca8b1d963d9138372768e1fecad71e9に固定します。Python 3.10、CUDA 11.8、PyTorch 2.0.0と、固定したOpenMMLab依存関係を使用します。NVRTC本体と開発用リンクを同じ11.8.89-1で導入し、CUDAライブラリの場所をldconfigへ登録します。MMCV 2.1.0、MMEngine 0.10.7、MMDetection 3.2.0、MMPretrain 1.2.0の読み込みと、重みのキーと形状も検査します。

重みは[既定の公式配布ファイル](https://s3.unistra.fr/camma_public/github/ultrasam/UltraSam.pth)です。現在の[定数](../../serverless/pytorch/camma/ultrasam/nuclio/ultrasam_constants.py)が持つSHA-256をビルド時と初期化時に照合します。

```text
d7c223dd03f56b0b77cd246aa3edfae651e99f0ee2dde2e8c50eda7b21fa8a0c
```

関数イメージ内の/opt/nuclio/checkpoints/UltraSam.pth、同じ場所のSHA256SUMS、/opt/nuclio/build-validation.json、build-requirements.txtを記録します。cvatctl configの参照と実際のイメージIDを保存し、同じタグの再ビルドを同一の検証済みイメージと扱いません。

## 自動試験と実GPUの確認

ビルドではlibnvrtc.soの読み込みとCUDAコードのコンパイルを確認します。関数起動時は子プロセスでCUDAとcuDNNのGPU畳み込みを検査し、その後に画像特徴抽出と正負点の推論を実行します。成功後に受付を開始し、試行画像の特徴は破棄します。cvatctl checkは構成と稼働の確認であり、実画像の品質評価ではありません。

CPU試験は専用環境で実行します。モデル重みとOpenMMLabは不要ですが、テンソル処理にはPyTorchを使用します。

```bash
python3.10 -m venv /srv/cvat-test-envs/ultrasam
/srv/cvat-test-envs/ultrasam/bin/python -m pip install -r components/ultrasam/tests/requirements.txt
/srv/cvat-test-envs/ultrasam/bin/python -m pip install 'torch==2.0.0+cpu' --index-url https://download.pytorch.org/whl/cpu
/srv/cvat-test-envs/ultrasam/bin/python -m pytest -q components/ultrasam/tests
```

実GPU試験では他の利用者の推論を止め、配備した関数コンテナを確認します。

```bash
docker ps --filter label=nuclio.io/function-name=pth-ultrasam-interactor --format '{{.ID}} {{.Names}}'
ULTRASAM_CONTAINER=CONFIRMED_CONTAINER_ID
docker cp components/ultrasam/tests/gpu_smoke.py "${ULTRASAM_CONTAINER}:/opt/nuclio/gpu_smoke.py"
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/ultrasam_runtime.py --mode cuda
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/gpu_smoke.py
```

試験は合成画像で公式初回推論のlogitと予測IoU、負例点、元の寸法、マスク形式の往復を確認します。実画像では、実際の画像と座標に置き換えて実行します。

```bash
docker cp /srv/cvat-test-data/ultrasound.png "${ULTRASAM_CONTAINER}:/opt/nuclio/example.png"
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/gpu_smoke.py \
  --image /opt/nuclio/example.png --positive 100,120 --negative 40,50
```

既定ではCUDAを必須とし、--device cpuを指定した診断をGPU確認に含めません。ブラウザーでは正例点の追加、負例点、縦長と横長の座標、確定、Undo、Save後の再読込、不正入力後の正常操作を確認します。有効な他の拡張と併用し、再開後の推論とGPU warmup passedのログ、RestartCountが増えないことも確認します。画像、点、イメージIDと結果を記録してください。

## 問題がある場合の確認先

| 症状 | 確認内容 |
|---|---|
| 一覧にない | CVAT_EXTENSIONS、名前空間、status、check、画面の再読込。 |
| ビルド失敗 | ソース、依存パッケージ、重みのどの取得または検証段階で失敗したか。 |
| libnvrtc.soやcuDNNの読み込み失敗 | 修正したビルドから再構築し、ultrasam_runtime.py --mode buildを確認します。一時的なリンク追加だけで済ませません。 |
| 503または504 | 関数ログと再起動回数。異常終了を解消してから推論時間と中継の待機時間を確認します。 |
| GPU初期化失敗、メモリ不足 | nvidia-smi、Toolkit、選択GPU、同時稼働するSAMなど。必要なら停止してGPUを変更します。 |
| 座標や品質が合わない | 元画像、正負点、重みとイメージを受入確認時の条件と比較します。 |

PyTorchの[CUDA 11.8でのNVRTC読み込みに関する報告](https://github.com/pytorch/pytorch/issues/97041)を参照します。torch.cuda.is_availableだけでは実演算を確認できません。libnvrtc.so.11.2という名前だけで誤った版と判断せず、[NVIDIAの版管理仕様](https://docs.nvidia.com/cuda/archive/11.8.0/nvrtc/index.html#versioning-scheme)に沿ってnvrtcVersionで確認します。

ライセンスと出典の原文は[UltraSAMの案内](README.md#配備と利用条件)から参照してください。バックアップと復元は[共通手順](../extensions/backup-restore.md)を使用します。
