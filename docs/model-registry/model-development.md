# ONNXモデルの実装仕様

[モデル管理](README.md) / [利用手順](usage.md)

このページはmodel.pyを作成する技術者向けです。Segmentation、Detection、Classification、Promptingの共通入力と出力の違いをまとめています。
モデル固有の前処理、座標や画像サイズの復元、信頼度による選別、NMS、分類クラスの選択は推論コードで実装します。

## 共通インターフェース

`registry.sdk.ModelBase`を継承する`Model`クラスを作成します。`load`は版を読み込むとき、`predict`は画像ごとに呼ばれます。任意の`close`はタイムアウトや異常終了時の実行を保証しません。

```python
import numpy as np
from registry.sdk import ModelBase, ModelContext, PredictParams

class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        self.session = context.create_session("model.onnx")

    def predict(self, image: np.ndarray, params: PredictParams):
        # ここに前処理、推論、後処理を実装します。
        raise NotImplementedError
```

このコードはインターフェースのひな型であり、実行可能な認識モデルではありません。[作例](../../components/model_registry/examples/)には動作確認用の実装があります。
入力はRGB順の`uint8`配列`(H, W, 3)`です。ROIが指定された場合は切り出し後の画像が入力です。返す座標とサイズは、この入力画像に合わせてください。
`PredictParams`は`request_id`とPrompting用の`pos_points`、`neg_points`、`obj_bbox`を持ちます。通常のモデルでは点は空のタプル、矩形は`None`です。`threshold`はありません。CVATから渡される閾値を管理サーバーが結果の選別に使うこともありません。

ONNXは`context.create_session`で読み込みます。マニフェストにない重み、独自演算子、モデルごとの依存ライブラリ追加には対応しません。GPU用の実行プロバイダーを初期化できない場合、CPUへ暗黙に切り替えません。
利用可能なライブラリは`components/model_registry/requirements-worker.txt`と`requirements-worker-gpu.txt`を参照してください。

## マニフェストとファイル

フォームは以下の形式のマニフェストを生成します。APIでは[登録API](architecture.md#新規登録と部分更新のapi)の`manifest`にJSONを指定します。

```json
{
  "schema_version": 1,
  "name": "臓器Segmentation",
  "description": "対象と使用条件を記載します。",
  "author_contact": "",
  "weights": ["model.onnx"],
  "labels": [
    {"id": 10, "name": "liver", "type": "polygon"},
    {"id": 20, "name": "vessel", "type": "polygon"}
  ],
  "polygon": {
    "min_distance_px": 2.0,
    "spacing_percent": 1.0,
    "min_area_px": 10.0
  }
}
```

新規登録はmodel.py、1個から16個のONNXファイル、PNGまたはJPEGの試験画像を個別に送ります。外部テンソルデータは使わず、重みをONNXファイルに内包してください。
補助Pythonモジュールの新規登録は対象外です。保存済みの旧モデルに含まれる補助モジュールは更新時に保持します。
各Pythonファイルは1 MiB、マニフェストは256 KiB、試験画像は32 MiBかつ1600万画素以下です。内部パッケージは2 GiB、展開後は3 GiB、40ファイル以下です。外部からのZIP登録を意味する上限ではありません。
リンク、ディレクトリ、同名ファイル、大文字と小文字だけが異なる重複は受け付けません。

## Segmentation

返り値は`(N, H, W)`のNumPy配列で、各画素は`bool`または整数の0か1です。`N`は`labels`の要素数、`H`と`W`は入力画像の高さと幅です。ラベルは全て`polygon`にします。
配列の順序がラベルの順序に対応します。上の例では`result[0]`が`liver`、`result[1]`が`vessel`で、クラスIDの値は配列位置を決めません。
確率配列、0と255の画像、クラスIDの二次元画像は返せません。二値化とサイズ復元はmodel.pyで行います。背景は必要な場合だけラベルとして定義します。

異なるラベルの領域が重なることは許容します。同じラベルで接触した領域を別インスタンスとして区別する情報は、この形式では保持しません。
出力は4近傍の連結領域ごとの外周ポリゴンで、領域内の穴は塗りつぶされます。

| 設定 | 既定値 | 意味 |
|---|---:|---|
| `min_distance_px` | 2.0 | 隣接頂点の直線距離の下限です。最後と最初の頂点にも適用します。 |
| `spacing_percent` | 1.0 | 元の周長に対する頂点配置間隔の百分率です。0から100で指定します。 |
| `min_area_px` | 10.0 | 変換前の領域画素数と変換後のポリゴン面積を選別する下限です。 |

頂点配置の基準間隔は`min_distance_px`と「周長 × spacing_percent / 100」の大きい方です。外周に等間隔で頂点を配置し、直線距離の条件を満たさない点を取り除きます。両方0なら輪郭の方向が変わる点を使います。
画素中心の輪郭を使うため、画素数とポリゴン面積は一致しません。3頂点未満、面積0、自己交差となる領域も出力しません。小さな対象を残す場合は、最小面積と頂点間隔を試験画像で調整してください。

二値配列は合計2400万要素以下、最終結果は2000図形以下、ポリゴンは1図形4096頂点以下かつ全体10万頂点以下です。上限を超える場合はエラーになり、黙って出力を切り捨てません。

## Detection

ラベルを`rectangle`とし、`registry.sdk.Box`のリストまたはタプルを返します。次はpredictの返り値の例です。

```python
from registry.sdk import Box

return [Box(class_id=10, score=0.92, xyxy=(12.0, 20.0, 80.0, 100.0))]
```

`class_id`は宣言したIDです。`score`は有限な0以上1以下の値、`xyxy`は入力画像の左、上、右、下の座標です。右端と下端は排他的です。
リサイズした画像で推論した場合は入力画像の座標へ戻します。低スコアの除外と重複検出の抑制はmodel.pyが行います。

## Prompting（Interactor）

SAMのように点や矩形を受けて対象領域を返すONNXモデルは、`kind: "interactor"`で登録します。省略時は従来どおり`detector`です。`interactor`設定と試験画像へのPromptが必須です。説明用ラベルは`polygon`を1件だけ宣言しますが、出力ラベルの分類には使わず、実際のタスクラベルはCVATのPromptingタブで選びます。

```json
{
  "schema_version": 1,
  "name": "ONNX prompted segmentation",
  "weights": ["encoder.onnx", "decoder.onnx"],
  "kind": "interactor",
  "labels": [{"id": 0, "name": "object", "type": "polygon"}],
  "interactor": {
    "min_pos_points": 1,
    "min_neg_points": 0,
    "startswith_box": false,
    "startswith_box_optional": true,
    "sample": {
      "pos_points": [[120.0, 80.0]],
      "neg_points": [[20.0, 20.0]],
      "obj_bbox": null
    }
  }
}
```

試験Promptは試験画像に合わせて指定します。登録、部分更新、過去の版への切り替えは、その版に保存された試験画像とPromptで推論を検証します。画像だけを変更した場合もPromptを自動移動せず、画像範囲外なら公開に失敗します。

| 入力・設定 | 仕様 |
| --- | --- |
| `params.pos_points`、`params.neg_points` | 不変の`((x, y), ...)`。各1000点以下、有限な画素座標で`0 <= x < W`、`0 <= y < H`です。 |
| `params.obj_bbox` | `((左, 上), (右, 下))`または`None`。画像端の座標で右と下は排他的、`0 <= 左 < 右 <= W`、`0 <= 上 < 下 <= H`です。CVATのAI toolsが点だけで送る空配列`[]`と試験Promptの`[]`は、矩形なしの`None`として扱います。 |
| `min_pos_points` | 矩形なしで必要な正の点数。通常は1以上、矩形必須の場合は0も指定できます。上限1000です。 |
| `min_neg_points` | 除外点の最小数。0は任意、-1は使用禁止、1から1000は必要な点数です。 |
| `startswith_box` | 矩形必須です。この場合`startswith_box_optional`はfalseにします。 |
| `startswith_box_optional` | 点から開始するか矩形から開始するかをCVATで選べます。両設定がfalseなら矩形を拒否します。 |

矩形がある場合は正の点の最小数を適用しません。除外点の条件は矩形があっても適用します。SDKへ渡すPromptはRGB入力画像と同じ座標系です。ROIがある場合はCVAT本体が切り出しとPromptの移動、応答の全画像への復元を行います。モデル側で全画像座標への移動を重ねないでください。

`predict`は入力画像と同じ`(H, W)`のNumPy二値配列を1枚返します。`bool`または整数の0/1だけを受け付け、候補の`(N, H, W)`、確率、0/255、`Box`や`Mask`のリストは拒否します。候補の選択、二値化、リサイズ復元はmodel.pyの責務です。エンコーダーとデコーダーなど複数のONNXも、それぞれ`context.create_session`で読み込めます。入力名、テンソルの形、点の正負を表すラベルなどはONNXの仕様に合わせてmodel.pyで構築します。

ワーカーは非空の結果をCVAT標準の`{"mask": [[0, 1, ...], ...]}`、対象なしを`{"shapes": []}`として返します。管理サーバーも独立して寸法と二値性を検査します。マスクの穴と離れた領域を保持し、`polygon`の変換設定は適用しません。CVAT側で「Convert masks to polygons」を選ぶ場合の変換は標準UIの処理です。画像の1600万画素とマスクの2400万要素の上限を超える要求はエラーです。

入力画像と全Promptを毎回渡すステートレスな方式です。前回のマスク、埋め込みの共有、テキストPrompt、動画追跡の登録は対象外です。モデル固有の依存追加や外部重みの取得も行いません。専用SAM2/SAM3.1の重みを自動的にONNXへ変換する機能ではありません。

## Classification

ラベルを`tag`とし、`registry.sdk.Tag`のリストまたはタプルを返します。座標は持ちません。

```python
from registry.sdk import Tag

# 単一ラベルの分類です。
return [Tag(class_id=7, score=0.92)]

# 複数ラベルの分類では異なるクラスを返します。
return [Tag(class_id=7, score=0.92), Tag(class_id=42, score=0.81)]

# 該当なし、または判定を保留します。
return []
```

これらは別々の返り値の例です。`class_id`はラベル配列の位置ではなく宣言したIDで、`score`は有限な0以上1以下の値です。
softmax、sigmoid、最上位クラスの選択、閾値判定はmodel.pyで実装します。生の確率配列は自動的にタグへ変換しません。
同じ応答内での同一クラスの重複、未宣言のクラス、型の不一致、不正なスコアは拒否します。既存のCVATタグとの重複排除は行いません。
画像全体を分類する場合はROIを指定しません。ROIの分類結果も、そのフレーム全体へのタグになります。

## 保存済みモデルとの互換性と作例

旧形式の`Mask`や`Box`と`Mask`の混在リストは引き続き受け付けます。対応するラベルを宣言し、Maskには入力画像と同じ大きさの二値画像を指定します。マスクは同じポリゴン変換を通ります。
保存済みマニフェストの`mask`は`polygon`として扱い、旧`threshold`は取り除きます。旧コードが`params.threshold`を参照する場合は修正が必要です。

作例のSegmentation、Detection、Classification、PromptingはONNX Identity演算を使う接続確認用で、学習済み認識モデルではありません。Promptingは点と矩形で明るい領域を選ぶ作例で、SAM本体ではありません。Classificationのスコアも校正済みの分類確率ではありません。
`python components/model_registry/examples/generate.py`で作例を再生成できます。生成されるZIPは内部パッケージの検査用で、画面へアップロードするためのものではありません。
実装後は[検証手順](testing.md)に従い、代表的な実画像とCVATでの保存まで確認してください。
