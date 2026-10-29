# 折れ線の長さをmmで表示する

[アノテーション操作](README.md) / [拡張機能の管理](../extensions/management.md)

画像上に描いた折れ線の長さを、画素数ではなくmmで確認する機能です。DICOMから保存した画素間隔を使います。

このフォークでは、DICOMの識別情報や撮影条件を画像単位で保存するタグを **DicomTag** と呼びます。「DicomTag」の `PixelSpacing` 属性が隣接画素の間隔です。この機能は表示中のフレームに保存された値だけを参照し、CVATの折れ線 `polyline` の長さを計算します。

## 参照する属性と単位

DICOM取り込みは、標準の属性名であるKeywordをCVATの属性名として保存します。
`PixelSpacing (0028,0030)` は `DicomTag` のText属性 `PixelSpacing` です。
値は `0.30\0.25` のような、バックスラッシュで区切った2個の数値です。

| 項目 | 参照先または値 |
| --- | --- |
| Tagのラベル名 | `DicomTag` |
| PixelSpacingの属性名 | `PixelSpacing` |
| Y方向の間隔 | 第1値。隣接する行の間隔です。 |
| X方向の間隔 | 第2値。隣接する列の間隔です。 |
| 単位 | mm/px |

値の順序と単位は
[DICOMのPixel Spacing仕様](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_10.7.html)
に従います。例えば `0.30\0.25` は、X方向0.25 mm/px、Y方向0.30 mm/pxです。
別のラベルの同名属性からは取得しません。

利用する値は、0より大きい有限の10進数2個です。指数表記も受け付けます。
Tagや属性がない場合、値が空の場合、不正な値の場合はpxで表示します。
同じフレームに複数の `DicomTag` がある場合は、すべてが同じ有効な値を持つ場合だけ使用します。
別のフレームの値を引き継ぐことはありません。

## 表示と計算

作成済みのpolylineを選択するかポインターを重ねると、通常の図形ラベルと一緒に長さが表示されます。
既存の「Show all text」を有効にした場合も、その表示条件に従います。
頂点編集、Tagの変更や削除、Undo、フレーム切替に追従します。
表示フィルターや非表示の状態で `DicomTag` が画面から隠れていても参照します。

計算には、アノテーションが保持する画像座標を使用します。
各線分のX方向とY方向の差に対応する間隔を掛け、線分の長さを合計します。

```text
長さ [mm] = Σ hypot((x[i+1] - x[i]) × spacing.x,
                   (y[i+1] - y[i]) × spacing.y)
```

例えば `PixelSpacing` が `0.4\0.3`、座標が `(0, 0) → (10, 0) → (10, 10)` の場合、
長さは `3 + 4 = 7 mm` です。画面上の拡大縮小、移動、表示回転では値は変わりません。
アノテーションの座標はピクセル単位のままで、計算結果を属性として保存しません。

## ITGformatへの出力

ITGformatのエクスポートも、出力する画像の `DicomTag` を同じ規則で参照します。
HDRのX方向とY方向の間隔へ `PixelSpacing` を反映し、Z方向は常に1です。
値が利用できない場合は、従来どおりX、Y、Zをすべて1にします。

```text
DicomTag.PixelSpacing = 0.30\0.25
ITGformat HDR spacing = 0.25 0.3 1
```

この出力は既存の `itgformat` 拡張の動作で、表示用の `physical_measurements` を選択しなくても適用されます。
出力にはサーバーに保存済みのTagを使うため、画面で編集した場合は保存してから出力します。
`DicomTag` の全属性をITGformatへ書き出す処理や、ITGformatのHDRからTagを作る処理は含みません。
対応する出力対象は[ITGformatの仕様](../itgformat/format_spec.md)を参照してください。

Tagは標準のアノテーションとして保存するため、バックアップも標準の処理を使用します。
この拡張専用のDBテーブル、API、バックアップファイルは追加しません。

## 管理者向けの設定

[共通の更新手順](../extensions/update.md)に従い、既存の選択を保持して
`.env` の `CVAT_EXTENSIONS` に `physical_measurements` を追加します。
ITGformatも使用する場合の選択例は次のとおりです。

```dotenv
CVAT_EXTENSIONS=itgformat,physical_measurements
```

`cvatctl` が `plugins/physical-measurements` をUIのビルドへ追加します。
ITGformatの出力変更はサーバーと出力ワーカーへ反映します。
この変更によるDB移行、追加サービス、GPU、Nuclioは不要です。

```sh
bash components/extensions/cvatctl config
bash components/extensions/cvatctl doctor
bash components/extensions/cvatctl build
bash components/extensions/cvatctl up
bash components/extensions/cvatctl check
```

表示を無効にする場合は、同じ管理手順で `physical_measurements` を外して再ビルドします。
再配備後に画面を再読み込みすると、物理長の表示が消えます。
標準のTagアノテーションは保持され、ITGformatの出力は引き続きTagを参照します。

## 対応範囲と確認

対象は2D画像と動画のpolylineです。画像全体で一定のX方向とY方向の間隔を前提とします。
描画途中の長さ表示、3D、音声、超音波の領域ごとの校正は対象外です。
PR11が抽出しないSequence内の値や、別のDICOM属性から間隔を推測しません。
画像自体を事前に拡大縮小した場合は、Tagの値も変換後の画素に対応している必要があります。

計算と表示の接続は `cvat-ui/plugins/physical-measurements/` に置いています。
Canvasへの変更は、既存の図形ラベルへ文字列を追加する任意の `shapeText` フックです。
既存の文字位置と表示条件を利用し、プラグインを選択しない場合の表示は維持します。

回帰テストは次のコマンドで実行できます。

```sh
node --test cvat-ui/plugins/physical-measurements/tests/*.test.cjs
python3 -m pytest -q tests/itgformat components/extensions/tests/test_client_plugins.py
```

実環境では、異なるXとYの値、Tagがない画像、フレームごとの値の変更、Tag編集とUndo、
表示フィルター、ITGformatのHDR、有効化と無効化を確認します。
実CVATと実際の医療画像での確認は、自動試験の成功とは分けて記録してください。
