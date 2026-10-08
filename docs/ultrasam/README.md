# UltraSAMによる超音波画像の分割

この拡張は、CVAT Communityの標準AI ToolsへUltraSAMの画像分割を追加します。
利用者はInteractorsから`UltraSAM (GPU)`を選び、対象を含める正例点と除外する負例点を指定して、1個の対象の注釈を作成します。
関数IDは`pth-ultrasam-interactor`です。

処理対象は現在の画像です。
正例点を1個以上指定し、必要に応じて正例点と負例点を追加できます。
矩形プロンプト、動画追跡、フレーム間の状態保持は実装していません。
CVATへ保存するクラス名は利用者がラベルから選択します。

## 実行構成

UltraSAMは専用のNuclio関数で推論します。
CVAT本体と標準UIを追加変更せず、SAM2と同じinteractorの呼び出し方式へ接続します。
UltraSAM用のPython、PyTorch、OpenMMLabの依存関係は関数イメージに含め、CVAT本体やSAM2の実行環境には追加しません。

管理コマンドは`CVAT_EXTENSIONS`に`ultrasam`がある場合に、Nuclioの構成と関数を管理します。
専用のUIプラグイン、Redis、ホスト上の重み保存ディレクトリは不要です。
SAM2と同時に使用する場合のRedisは、SAM2の動画追跡のために起動します。

| 配置 | 内容 |
|---|---|
| `serverless/pytorch/camma/ultrasam/nuclio/` | Nuclio関数、推論処理とビルド設定です。 |
| `serverless/pytorch/camma/ultrasam/deploy.sh` | `cvatctl deploy-ultrasam`を呼び出すスクリプトです。通常は`cvatctl`を使用します。 |
| `serverless/pytorch/camma/ultrasam/tests/` | 入力処理、マスク変換、ポイント処理と実GPUの確認です。 |
| `components/ultrasam/` | Nuclio dashboardの管理ポートをループバックへ公開するComposeです。 |
| `components/extensions/` | 拡張機能の選択、起動、停止と確認です。 |

入力からマスク返却までの契約と保守対象は[設計](design.md)に記載しています。

## 導入と起動

導入先は[共通運用手順](../extensions/README.md)のLinux、Docker Compose、NVIDIA GPUとNVIDIA Container Toolkitの構成を使用します。
`nuctl`は対象CVATのNuclio dashboardと同じ版に合わせます。
初回ビルドにはGitHub、PyTorchとOpenMMLabの配布元、公式のモデル配布元への接続が必要です。
SAM2と同じGPUを選ぶ場合は、両方の関数がモデルを保持した状態でメモリ容量と応答時間を確認します。

既存環境へ追加する場合は、現在の設定と管理コマンドで停止してから、同じ`.env`の`CVAT_EXTENSIONS`へ`ultrasam`を追加します。
設定に含まれる他の機能名と秘密情報は維持します。
既存の`.env`は自動変更されません。

```bash
./components/extensions/cvatctl down
```

全機能を有効にする場合の設定例は次のとおりです。
`ULTRASAM_GPU_DEVICE`は`nvidia-smi`で確認した1個のGPUの数値IDまたはGPU UUIDにします。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry
ULTRASAM_GPU_DEVICE=0
```

GPUの指定は関数へ渡す`CUDA_VISIBLE_DEVICES`に反映されます。
Nuclioのローカル実行はコンテナへGPUを公開するため、この設定は推論先を選ぶものであり、他の関数とのGPUの専有やアクセス分離を保証しません。

初回構築では、設定を確認してから必要なイメージを準備し、起動します。
ビルド前に対象コードをコミットしてソースを固定します。
イメージの参照はソースから決まるため、検証後にコードを変更した場合は再度ビルドと確認を行います。
次のコマンドは完全なCVATチェックアウトのルートで実行します。
設定ファイルを別の場所で管理している場合は、全コマンドに同じ`--env-file /絶対パス/cvat.env`を付けます。

```bash
./components/extensions/cvatctl config
./components/extensions/cvatctl doctor
./components/extensions/cvatctl pull
./components/extensions/cvatctl build
./components/extensions/cvatctl up
./components/extensions/cvatctl status
./components/extensions/cvatctl check
```

`up`は必要に応じてUltraSAMの関数イメージをビルドし、デプロイします。
`build`だけではNuclio関数のビルドは完了しません。
初回ビルドで公式ソースとモデル重みを取得し、重みを関数イメージ内の`/opt/nuclio/checkpoints/UltraSam.pth`へ保存します。

ソースと共通設定が起動時と同じ環境でUltraSAMの関数を再デプロイする場合は、次を使用します。
`all`も同じ画像関数を対象にし、`tracker`は受け付けません。

```bash
./components/extensions/cvatctl deploy-ultrasam image
./components/extensions/cvatctl check
```

コードを更新すると、cvatctlが生成するイメージのタグと保存済みの構成が変わります。
更新前のコードと設定で`down`を実行し、変更の適用とコミット後に通常の`up`で再ビルドします。
更新直後の`deploy-ultrasam`は、保存済みの構成と一致しなければ拒否されます。
修正した関数イメージをまだ作成していない状態で`up --no-build`を使っても、依存関係は更新されません。

本番へ更新するときは[更新手順](../extensions/update.md)に従い、実GPUで検証した関数イメージを移送して`up --no-build`で起動します。
バックアップと復元の対象は[共通の保存手順](../extensions/backup-restore.md)に記載しています。

## 停止と無効化

通常の停止は`cvatctl down`で行い、再開は同じ設定と検証済みイメージで`cvatctl up --no-build`を実行します。
停止時にはUltraSAMの関数コンテナも停止し、CVATに保存済みの注釈は保持します。

UltraSAMを無効にする場合は、現在の設定で`down`を実行してから、`CVAT_EXTENSIONS`の`ultrasam`だけを削除して`up --no-build`を実行します。
GPUを変更する場合も、停止後に`ULTRASAM_GPU_DEVICE`を変更して再開します。
`.env`の編集だけでは稼働中の構成は変わらず、`check`は保存済みの稼働記録を検証します。

## 画面での使い方

1. 超音波画像を含むジョブを開き、AI ToolsのInteractorsから`UltraSAM (GPU)`を選択します。
2. 作成する注釈のラベルを選び、対象の内部を左クリックして正例点を置きます。
3. 対象から漏れた領域を左クリックして正例点を、含めたくない領域を右クリックして負例点を追加し、表示された輪郭を確認します。
4. 標準のinteractor操作で結果を確定し、必要な修正を行ってSaveで保存します。

点を追加した直後に表示される推論結果と、CVATへ保存済みの注釈は区別します。
保存後にジョブを開き直し、ラベルと形状が保持されることを確認します。
関数は画像の分割結果を返すため、対象の意味と境界の妥当性は利用者が確認します。

## 確認方法

関数イメージのビルド時には、`libnvrtc.so`の読み込みとCUDAソースのコンパイルを実行します。
関数の起動時には、子プロセスで同じ検査とcuDNNのGPU畳み込みを実行し、その後にワーカー内でUltraSAMの画像特徴抽出と正負ポイントの推論を行います。
これらが成功した場合だけ初期化を完了します。
試行推論に使った画像特徴は破棄し、利用者の画像へ引き継ぎません。

導入時には、CPUでの自動試験、実GPUでの推論、CVAT画面での受入確認を行います。
`cvatctl check`は構成、関数登録、実イメージと稼働状態を調べます。
その成功だけでは、実際の画像に対する推論品質や画面操作の成功は確認できません。

CPUの自動試験には、専用のPython 3.10環境を使用します。
次の手順は入力、キャッシュ、マスク変換に加え、PyTorchのテンソル処理と重みの整合性検査も実行します。
これらの試験にはモデル重みとOpenMMLabのインストールは不要です。

```bash
python3.10 -m venv /srv/cvat-test-envs/ultrasam
/srv/cvat-test-envs/ultrasam/bin/python -m pip install \
  -r serverless/pytorch/camma/ultrasam/tests/requirements.txt
/srv/cvat-test-envs/ultrasam/bin/python -m pip install \
  'torch==2.0.0+cpu' --index-url https://download.pytorch.org/whl/cpu
/srv/cvat-test-envs/ultrasam/bin/python -m pytest -q \
  serverless/pytorch/camma/ultrasam/tests
```

実GPUの確認は、デプロイした関数と同じ環境で`gpu_smoke.py`を実行します。
次のコマンドで対象コンテナを確認し、`ULTRASAM_CONTAINER`を実際のIDへ置き換えます。
他の利用者の推論を止めた検証環境で実行してください。

```bash
docker ps --filter label=nuclio.io/function-name=pth-ultrasam-interactor \
  --format '{{.ID}} {{.Names}}'
ULTRASAM_CONTAINER=CONFIRMED_CONTAINER_ID
docker cp serverless/pytorch/camma/ultrasam/tests/gpu_smoke.py \
  "${ULTRASAM_CONTAINER}:/opt/nuclio/gpu_smoke.py"
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/ultrasam_runtime.py --mode cuda
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/gpu_smoke.py
```

この試験は内部で生成する画像を使い、公式の初回推論とのlogitと予測IoUの比較、負例点の処理、元画像の寸法、CVATのmask形式の往復を確認します。
実画像でも実行する場合は、画像をコンテナへコピーし、その画像上の正例点と負例点を指定します。
次のファイル名と座標は実際の確認画像に合わせます。

```bash
docker cp /srv/cvat-test-data/ultrasound.png \
  "${ULTRASAM_CONTAINER}:/opt/nuclio/example.png"
docker exec "$ULTRASAM_CONTAINER" python /opt/nuclio/gpu_smoke.py \
  --image /opt/nuclio/example.png --positive 100,120 --negative 40,50
```

既定ではCUDAを必須とし、利用できない場合は試験をエラーで終了します。
明示的に`--device cpu`を指定した実行はCPUでの診断として記録し、GPUの確認には含めません。

実GPUと画面では次の項目を確認し、使用した画像、ポイント、実イメージIDと結果を運用記録へ残します。

| 確認対象 | 合格条件 |
|---|---|
| 一覧表示 | 一般メンバーがAI ToolsのInteractorsで`UltraSAM (GPU)`を選択できます。 |
| 正例点 | 正例点1個から結果を生成し、追加の正例点を指定してもエラーになりません。 |
| 負例点 | 正例点に負例点を加えて再実行でき、除外したい領域に対する結果を比較できます。 |
| 画像座標 | 縦長と横長の画像で、クリック位置とマスクが元画像の座標へ一致します。 |
| 注釈保存 | 結果の確定、Undo、Save後の再読込ができ、形状とラベルが保持されます。 |
| 異常入力 | 正例点なし、画像外の点、不正な画像を使った要求を拒否し、次の正常要求を処理できます。 |
| 併用 | SAM2の画像分割と動画追跡、ITGformat、モデル管理の既存機能が各受入条件を満たします。 |
| 停止と再開 | `cvatctl down`で関数も停止し、`up --no-build`の後に同じ注釈を開いて推論を再開できます。 |
| ワーカーの安定性 | 起動ログの`GPU warmup passed`を確認し、複数回の実画像推論後も`RestartCount`が増えず、共有ライブラリのエラーが出ません。 |

## 重みと実行環境の記録

公式ソースはコミットを固定して取得します。
モデル重みは[公式README](https://github.com/CAMMA-public/UltraSam)が案内する配布元を使用します。
ビルド設定は`serverless/pytorch/camma/ultrasam/nuclio/function-gpu.yaml`に記載しています。

関数はPython 3.10、CUDA 11.8とPyTorch 2.0.0を使用します。
NVRTC本体と開発用リンクは、NVIDIAの`cuda-nvrtc-11-8`と`cuda-nvrtc-dev-11-8`の`11.8.89-1`を使用します。
開発用パッケージに含まれる`libnvrtc.so`のリンクと、同じ版の`libnvrtc-builtins.so.11.8`を使用し、CUDA 11.8のライブラリの場所を`ldconfig`へ登録します。
GPUドライバーの探索パスは既存の設定を使用します。
OpenMMLabの主要依存はMMCV 2.1.0、MMEngine 0.10.7、MMDetection 3.2.0、MMPretrain 1.2.0に固定します。
ビルド時にはこれらの版、拡張モジュールの読み込み、公式重みのキーと形状を検査し、不一致があればビルドを失敗させます。

| 対象 | 固定する値と保存先 |
|---|---|
| UltraSAMのソース | `ff3157b1fca8b1d963d9138372768e1fecad71e9`です。 |
| モデル重み | [公式配布ファイル](https://s3.unistra.fr/camma_public/github/ultrasam/UltraSam.pth)を関数イメージ内の`/opt/nuclio/checkpoints/UltraSam.pth`へ保存します。 |
| 重みのハッシュ | 関数イメージ内の`/opt/nuclio/checkpoints/SHA256SUMS`へ保存します。 |
| ビルド時の記録 | 関数イメージ内の`/opt/nuclio/build-validation.json`と`/opt/nuclio/build-requirements.txt`へ保存します。 |
| 実行イメージ | `cvatctl config`で参照を確認し、`docker image inspect`の実イメージIDとともに保存します。 |

固定する重みのSHA-256は、公式配布URLから取得したファイルの実測値です。
ビルド時と関数の初期化時に次の値と照合し、一致しないファイルを読み込みません。

```text
d7c223dd03f56b0b77cd246aa3edfae651e99f0ee2dde2e8c50eda7b21fa8a0c
```

関数イメージには推論コード、依存パッケージ、公式ソースと重みが含まれます。
同じタグでの再ビルド結果を検証済みの実イメージと同一とみなさず、本番では保存したイメージIDを照合します。

## 利用条件

公式リポジトリの[LICENSE](https://github.com/CAMMA-public/UltraSam/blob/ff3157b1fca8b1d963d9138372768e1fecad71e9/LICENSE)はCC BY-NC-SA 4.0です。
この条件には非商用の制限、出典表示と、改変物を配布する際の同じライセンスの継承が含まれます。[条件の日本語説明](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.ja)
企業内の研究用途で利用できるかは用途と権利者の条件に依存するため、運用前にモデル重みを含めた利用条件を確認します。
公式ソースのライセンス表示はビルド時に取得したソース内へ保持します。
この拡張に含むコードの出典と条件は[NOTICE](../../serverless/pytorch/camma/ultrasam/NOTICE.md)と[ライセンス本文](../../serverless/pytorch/camma/ultrasam/LICENSE-UltraSAM)に記載しています。

## 問題がある場合の確認先

| 症状 | 確認する内容 |
|---|---|
| 一覧へ表示されない | `CVAT_EXTENSIONS`、Nuclioの名前空間、`cvatctl status`と`check`、画面の再読込を確認します。 |
| 関数のビルドに失敗する | デプロイログで、公式ソース、依存パッケージ、モデル配布元のどの取得段階で失敗したかを確認します。 |
| `libnvrtc.so`または`libcudnn_cnn_infer.so.8`の読み込みで失敗する | 修正したソースから関数イメージを再ビルドし、`ultrasam_runtime.py --mode build`の成功を確認します。起動済みコンテナへの一時的なリンク追加だけでは、再デプロイ時に設定が失われます。 |
| 503または504が発生する | 関数ログと再起動回数を確認し、ワーカーの異常終了があれば先に解消します。再起動がなくても504が続く場合は、実推論時間と各中継の待機時間を確認します。 |
| GPU初期化に失敗する | ホストの`nvidia-smi`、NVIDIA Container Toolkit、選択したGPU、関数ログを確認します。 |
| メモリ不足になる | 同じGPUを使用しているSAM2と他の関数を確認します。現在の設定で`down`を実行し、`ULTRASAM_GPU_DEVICE`を空き容量のあるGPUへ変更してから`up`を実行します。 |
| 輪郭の位置や品質が合わない | 元画像の寸法、正負のポイント、モデル重み、関数イメージを確認し、受入確認に使った画像と比較します。 |

PyTorch 2.0.0のCUDA 11.8向け配布には、畳み込み時に`libnvrtc.so`の読み込みで異常終了する[既知の報告](https://github.com/pytorch/pytorch/issues/97041)があります。
`torch.cuda.is_available()`はGPUを認識できるかを調べるため、この読み込みと実演算の確認にはなりません。
なお、CUDA 11.8のNVRTCが`libnvrtc.so.11.2`という名前を使うこと自体は[NVIDIAの版管理仕様](https://docs.nvidia.com/cuda/archive/11.8.0/nvrtc/index.html#versioning-scheme)に従っています。
実際の版は`nvrtcVersion()`で検査し、ファイル名の`11.2`だけを理由にライブラリを置き換えません。
