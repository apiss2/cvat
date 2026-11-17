# ONNXモデルの実装仕様

[モデル管理](README.md) / [利用手順](usage.md)

このページはmodel.pyを作成する技術者向けです。Segmentation、Detection、Line detection、Classification、Promptingの共通入力と出力の違いをまとめています。
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
    "max_error_px": 1.0,
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
| `max_error_px` | 1.0 | 元の輪郭上の点と対応する近似線分との距離の上限（px）です。0なら輪郭を変形させません。 |
| `min_area_px` | 10.0 | 変換前の領域画素数と変換後のポリゴン面積を選別する下限です。 |

外周の画素中心をたどる輪郭をDouglas–Peucker法で近似します。頂点は輪郭上の点から選び、輪郭上の各点が対応する辺（有限の線分）から`max_error_px`以内に収まるようにします。直線上には頂点を追加しません。隣接頂点の距離が`min_distance_px`に満たない箇所は、その周辺にある輪郭上の別の点を使い、両方の条件を満たす頂点へ置き換えます。探索は短い辺の周辺に限るため、条件を満たす解が存在しても見つからない場合があります。条件を両立できない輪郭や自己交差が生じる輪郭は出力しません。
幅1画素の部分は、画素中心の輪郭では面積を持ちません。幅1画素の突起は輪郭から除きます。幅1画素の橋でつながった領域は、橋の両側を別のポリゴンとして出力します。除いた画素は近似誤差の対象に含めません。
画素中心の輪郭を使うため、画素数とポリゴン面積は一致しません。3頂点未満、面積0となる輪郭も出力しません。`min_distance_px`を大きく、`max_error_px`を小さくするほど、細い先端などで条件を両立できず、出力されない領域が増えます。小さな対象や細い形状を残す場合は、3つの設定を試験画像で調整してください。

二値配列は合計2400万要素以下、最終結果は2000図形以下、ポリゴンは1図形4096頂点以下かつ全体10万頂点以下です。上限を超える場合はエラーになり、黙って出力を切り捨てません。

## Detection

ラベルを`rectangle`とし、`registry.sdk.Box`のリストまたはタプルを返します。次はpredictの返り値の例です。

```python
from registry.sdk import Box

return [Box(class_id=10, score=0.92, xyxy=(12.0, 20.0, 80.0, 100.0))]
```

`class_id`は宣言したIDです。`score`は有限な0以上1以下の値、`xyxy`は入力画像の左、上、右、下の座標です。右端と下端は排他的です。
リサイズした画像で推論した場合は入力画像の座標へ戻します。低スコアの除外と重複検出の抑制はmodel.pyが行います。

## Line detection

亀裂、境界線、車線などの開いた線を検出するモデルは、ラベルを`polyline`とし、`registry.sdk.Polyline`のリストまたはタプルを返します。1枚の画像から任意の本数を返せ、各要素がCVATのポリライン1本になります。該当がない場合は`[]`を返します。

```python
from registry.sdk import Polyline

return [
    Polyline(class_id=3, score=0.88, points=[(10.0, 40.0), (60.0, 42.5), (120.0, 47.0)]),
    Polyline(class_id=3, score=0.71, points=[(5.0, 90.0), (140.0, 90.0)]),
]
```

`points`は`(x, y)`の組のリスト、タプル、または数値の`(K, 2)`配列です。座標は入力画像の連続座標で、`0 <= x <= W`、`0 <= y <= H`です。頂点は2個以上4096個以下で、連続する同じ頂点は拒否します。線の交差と自己交差は許容し、始点と終点を自動で閉じません。
`class_id`と`score`の扱いはDetectionと同じです。線の抽出、細線化、近い線の統合、頂点の間引きはmodel.pyが行います。ポリゴン変換設定は使用しません。

`Box`、`Polyline`、`Tag`は、対応する型のラベルを宣言すれば同じ返り値に混在できます。上限はSegmentationと共通で、最終結果は2000図形以下、ポリゴンとポリラインの頂点は合計10万以下です。上限を超える場合はエラーになり、黙って切り捨てません。

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
| `min_pos_points` | 必要な正の点数。矩形を描いた場合も適用します。通常は1以上、矩形必須の場合は0も指定でき、矩形だけで推論できます。上限1000です。 |
| `min_neg_points` | 除外点の最小数。0は任意、-1は使用禁止、1から1000は必要な点数です。 |
| `startswith_box` | 矩形必須です。この場合`startswith_box_optional`はfalseにします。 |
| `startswith_box_optional` | 点から開始するか矩形から開始するかをCVATで選べます。両設定がfalseなら矩形を拒否します。 |

正の点と除外点の最小数は、矩形があっても適用します。CVATの編集画面も同じ条件で推論を開始するため、CVAT本体は変更していません。矩形だけで推論するモデルは、矩形を必須にして`min_pos_points`を0にします。`min_neg_points`が-1のモデルでも、CVATの編集画面は右クリックで除外点を追加できます。この場合は管理サーバーが推論要求を拒否します。SDKへ渡すPromptはRGB入力画像と同じ座標系です。ROIがある場合はCVAT本体が切り出しとPromptの移動、応答の全画像への復元を行います。モデル側で全画像座標への移動を重ねないでください。

`predict`は入力画像と同じ`(H, W)`のNumPy二値配列を1枚返します。`bool`または整数の0/1だけを受け付け、候補の`(N, H, W)`、確率、0/255、`Box`や`Mask`のリストは拒否します。候補の選択、二値化、リサイズ復元はmodel.pyの責務です。エンコーダーとデコーダーなど複数のONNXも、それぞれ`context.create_session`で読み込めます。入力名、テンソルの形、点の正負を表すラベルなどはONNXの仕様に合わせてmodel.pyで構築します。

ワーカーは非空の結果を`{"mask": [[0, 1, ...], ...], "polygons": [[x1, y1, ...], ...]}`、対象なしを`{"shapes": []}`として返します。管理サーバーは寸法、二値性、ポリゴンの座標、頂点数、面積、自己交差、最小頂点距離を検査します。マスクは穴と離れた領域を維持します。「Convert masks to polygons」が有効なときだけ、Segmentationと共通の`polygon`設定で生成された外周ポリゴンをCVATが使用します。離れた領域は個別のポリゴンとなり、内部の穴は塗りつぶされます。条件を満たす領域がない場合は`polygons`が空になり、ポリゴンは作成されません。ポリゴンが図形数または頂点数の上限を超える場合は、エラーにせず`polygons`を省いてマスクだけを返します。この場合、CVATは他のInteractorと同じ標準の近似処理でポリゴンへ変換します。画像の1600万画素とマスクの2400万要素の上限を超える要求はエラーです。

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
保存済みマニフェストの`mask`は`polygon`として扱い、旧`threshold`は取り除きます。
旧`polygon.spacing_percent`は読み込み時に`max_error_px`へ自動変換します。周長100 pxを仮の基準として旧値の数値をpxに置き換える近似移行です（例: 1% → 1 px）。旧設定の頂点間隔と新設定の形状誤差は意味が異なるため、変換結果の同一性は保証できません。`min_distance_px`と`min_area_px`は維持し、新旧のキーが共存する場合は新設定を優先します。設定がなければ既定値（2 px、1 px、10 px²）を使用します。保存済みの全リビジョンと旧ZIPに読み込み時の移行を適用するため、登録し直す必要はありません。既存パッケージのファイル、ダイジェスト、重みは変更せず、次回の設定更新時に新形式で保存します。既存のアノテーションは変更しません。旧コードが`params.threshold`を参照する場合は修正が必要です。

作例のSegmentation、Detection、Line detection、Classification、PromptingはONNX Identity演算を使う接続確認用で、学習済み認識モデルではありません。Line detectionは明るい領域ごとに中央線と開いた外周線の2本を返す作例です。Promptingは点と矩形で明るい領域を選ぶ作例で、SAM本体ではありません。Classificationのスコアも校正済みの分類確率ではありません。
`python components/model_registry/examples/generate.py`で作例を再生成できます。生成されるZIPは内部パッケージの検査用で、画面へアップロードするためのものではありません。
実装後は[検証手順](testing.md)に従い、代表的な実画像とCVATでの保存まで確認してください。
