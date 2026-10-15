# ONNXモデルの実装仕様

[モデル管理](README.md) / [利用手順](usage.md)

このページはmodel.pyを作成する技術者向けです。Segmentation、Detection、Classificationの共通入力と出力の違いをまとめています。
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
`PredictParams`が持つのは`request_id`で、`threshold`はありません。CVATから渡される閾値を管理サーバーが結果の選別に使うこともありません。

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

作例のSegmentation、Detection、ClassificationはONNX Identity演算を使う接続確認用で、学習済み認識モデルではありません。Classificationのスコアも校正済みの分類確率ではありません。
`python components/model_registry/examples/generate.py`で作例を再生成できます。生成されるZIPは内部パッケージの検査用で、画面へアップロードするためのものではありません。
実装後は[検証手順](testing.md)に従い、代表的な実画像とCVATでの保存まで確認してください。
