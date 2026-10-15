# 分類モデルによる画像へのタグ付け

モデル登録画面の「モデルの種類」で `Classification（画像タグ出力）` を選択します。
ONNXファイル、推論コード、試験画像、クラスIDとラベル名を登録します。
分類ではポリゴン変換設定を使いません。公開前の試験推論では、タグ名と信頼度を表示します。

公開したモデルはCVATの既存の自動アノテーションから選択できます。
モデルのラベルをタスクのタグ用ラベルへ対応付けて実行すると、画像または動画の各フレームにタグを作成します。
矩形やポリゴンを作成する処理ではありません。画像全体を分類する場合はROIを指定しないでください。
ROIを指定すると切り出した画像がモデルに渡されますが、出力先はそのフレームのタグです。

## 推論コードが返す値

`registry.sdk.Tag` のリストまたはタプルを返します。
`class_id` は `manifest.labels` に宣言したIDで、ラベル配列の位置ではありません。
`score` は0以上1以下の有限な数値です。タグに座標はありません。

```python
from registry.sdk import Tag

# 単一分類: モデル側で選んだクラスを1個返します。
return [Tag(class_id=7, score=0.92)]

# 複数分類: 採用した異なるクラスをそれぞれ返します。
return [Tag(class_id=7, score=0.92), Tag(class_id=42, score=0.81)]

# 該当なし、または判定を保留する場合です。
return []
```

これらは `Model.predict` の返り値の例です。ONNX出力に必要なsoftmaxやsigmoid、
最上位クラスの選択、しきい値判定は `model.py` で実装します。
登録機能は分類方式を推測せず、生の確率配列を自動でタグへ変換しません。
既存の検出モデルと同様に、CVATから渡されたしきい値による追加の選別も行いません。

同一画像の1回の返却で同じクラスを重複指定した場合、未宣言のクラス、型の不一致、
不正なスコアは拒否します。信頼度は試験画面と返却JSONで確認できますが、
CVAT標準タグに信頼度用の独自属性を自動追加する処理はありません。
既存アノテーションの削除や、再実行で生成するタグと既存タグの重複排除も追加していません。

## マニフェストのラベル定義

分類用のラベルは `type: "tag"` にします。スキーマの版は1のままです。

```json
{
  "schema_version": 1,
  "name": "画像の分類",
  "weights": ["model.onnx"],
  "labels": [
    {"id": 7, "name": "usable", "type": "tag"},
    {"id": 42, "name": "low_quality", "type": "tag"}
  ]
}
```

`model.py` の `ModelBase`、`load`、`predict` の呼び出し方は検出と領域分割と同じです。
実行例は `examples/classification/` にあります。モデルの登録には個別のファイルを指定します。
この例はONNX Identity演算と画像の平均輝度を使う接続試験用で、学習済み分類器ではありません。
そのスコアも分類確率の校正をしたものではありません。
`python components/model_registry/examples/generate.py` で作例の重み、画像、内部パッケージ検査用のZIPを再生成できます。

分類モデルも[部分更新](PARTIAL_UPDATES.md)に対応します。
ONNXだけの置換、Pythonコードだけの置換、設定だけの変更で、未変更のファイルや分類設定を保持します。
更新の検証に失敗した場合は、従来どおり公開中の版を維持します。

## CVATとの接続と配備

CVATへは既存の `detector` 関数形式で公開し、返却する要素の型を `tag` にします。
CVATはこの要素を `tags` に振り分け、処理中のフレーム番号とタスク側のラベルIDを付けます。
分類専用の関数種別、CVAT本体の変更、追加サービスは不要です。

配備時はモデル登録サーバーとONNXワーカーの両方を更新してください。
ワーカー側でTagを出力し、管理サーバー側でもその出力を再検査するため、画面だけの更新では使用できません。
既存の検出、領域分割、旧形式のMask出力は引き続き使用できます。

## 検証

```sh
PYTHONPATH=components/model_registry python -m pytest components/model_registry/tests/test_classification.py -q
node --test components/model_registry/tests/classification-ui.test.cjs
```

Pythonテストは分類出力、独立した応答検査、旧出力形式、ZIP検査、ワーカーのHTTP処理を確認します。
通常のワーカー試験はONNXセッションをテスト用実装へ置き換えます。
`onnx` と `onnxruntime` がある場合は、Identityモデルを実行する別の試験も動きます。
JavaScriptテストはDOMのテスト用実装を使い、実際の登録処理と部分更新処理を呼び出します。
CVAT実サーバー、実ブラウザー、Docker配備、学習済みモデルの精度を検証する試験ではありません。
