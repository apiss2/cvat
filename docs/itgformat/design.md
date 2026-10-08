# ITGformatの設計とコード構成

## CVATとの接続

ITGformatは、CVATの既存のimporter/exporter登録機構に接続します。登録箇所は [registry.py](../../cvat/apps/dataset_manager/formats/registry.py) の `itgformat` importです。形式本体は専用パッケージに置き、CVATのモデル、API、データベースは変更しません。

```python
@exporter(name="ITGformat", ext="ZIP", version="1.0")
def _export(dst_file, temp_dir, instance_data, save_images=False): ...


@importer(name="ITGformat", ext="ZIP", version="1.0")
def _import(src_file, temp_dir, instance_data, load_data_callback=None, **kwargs): ...
```

## モジュールの責務

| ファイル | 役割 |
|---|---|
| `__init__.py` | adapterを読み込み、形式を登録する |
| `codec.py` | HDR、RE4、矩形の構文と数値検証、読書き、ビット合成 |
| `archive.py` | ZIP展開、ファイル一覧、画像寸法、manifest、画像対応 |
| `adapter.py` | CVATの抽出器とDatumaroのデータセットを相互変換する |
| `registry.py` | ITGformatの登録importを追加する既存ファイル |

RE4の圧縮と展開には既存のNumPyを使います。新たなC++拡張、PyTorch、CUDA、Nuclio依存関係は追加しません。

## 入力

ZIPを安全に展開し、HDR/RE4の対、形状、連続長、対応表、矩形、画像寸法を検証します。検証とDatumaroへの変換が終わるまで、プロジェクトの画像作成コールバックは呼びません。

既存タスクへの入力では画像の相対パスを優先し、ディレクトリを省略した場合も一意な後方一致だけを許します。同名画像が複数ある場合、寸法が異なる場合、数値をフレーム番号として推測する必要がある場合はエラーにします。

マスクはDatumaroの `RleMask`、矩形は `Bbox` に変換し、既存の `import_dm_annotations` に渡します。マスクを多角形へ変換しないため、穴や小領域を保持できます。

```text
ZIP
  -> パスとファイルの検証
  -> 画像との対応付け
  -> Datumaroの画像、RleMask、Bbox
  -> 必要な場合だけload_data_callback
  -> import_dm_annotations
```

## 出力

`GetCVATDataExtractor` から画像と形状を取得します。未アノテーション画像も対象に含め、ストリームを二度読まないよう画像単位でアノテーションを確定します。

出力対象全体を走査してセグメンテーションのクラス集合と矩形の有無を確定し、全画像で共通のビット対応表と画素幅を選びます。セグメンテーションが1つでもあれば全画像にマスク一式を、矩形が1つでもあれば全画像に `.bb` を作成します。

多角形はpycocotools、楕円はCVATの既存変換でマスク化します。同一クラスはビットORで合成し、異なるクラスの重なりを保持します。回転矩形、点、折れ線、タグ、骨格など表現できない形状はエラーにします。

## 保守上の注意

- 対応する画素幅とビット数の判断は `codec.py` に集約しています。
- 入力容量の保護上限は `codec.py` と `archive.py` の先頭定数で定義しています。
- CVATを更新した場合は、登録関数、抽出器、マスク変換、プロジェクト入力の呼出し規約を確認します。
- 更新後は [check_in_cvat.py](../../tests/itgformat/check_in_cvat.py) を実依存関係入りのバックエンドで再実行します。

## 運用構成との接続

`components/itgformat/docker-compose.itgformat.yml`は、APIと全バックエンドworkerのイメージを統一します。ビルドと構成検査は`components/extensions`が担当します。形式の変換コードからDockerやNuclioの管理処理を呼び出しません。
