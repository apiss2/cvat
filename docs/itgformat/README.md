# ITGformatの機能と導入

## 目的と対象

この拡張は、CVATのデータセット入出力に`ITGformat 1.0`を追加します。対象は2D画像のタスク、ジョブ、プロジェクトです。CVATの既存の形式登録を使い、画面やデータベースの構造は変更しません。

## 利用できる機能

入力では、HDRの画素幅とビット対応表を読み取り、RE4の領域とBBの矩形をCVATへ変換します。出力では、対象に含まれるクラスから対応表と画素幅を選び直します。固定の16bit形式として扱いません。

領域の変換では、同一クラスを和集合にし、異なるクラスの重なりとマスクの穴を保持します。表現できない形状、不正なファイル、曖昧な画像対応は、黙って無視せずエラーにします。詳細は[形式仕様](format_spec.md)を参照してください。

## ファイルの配置

```text
cvat/apps/dataset_manager/formats/itgformat/  HDR、RE4、BBとCVATの変換
components/itgformat/                       バックエンド用Compose
cvat/apps/dataset_manager/formats/registry.py  登録のimport 1行
tests/itgformat/                            単体試験と実依存確認
```

各モジュールの責務と変換順序は[設計](design.md)に記載しています。Dockerfileはルートの公式ファイルを使用し、専用Dockerfileは複製しません。

## 導入と運用

導入は[共通運用手順](../extensions/README.md)に従います。共通の設定ファイルと管理コマンドを使います。APIだけでなく、すべてのバックエンドworkerへ同じITGformat入りイメージを適用します。

```bash
./components/extensions/cvatctl up
./components/extensions/cvatctl check
```

更新は[更新手順](../extensions/update.md)に従います。形式ごとの説明に停止やバックアップ手順を重複して掲載しません。

## 確認方法

単体試験は`tests/itgformat`、実際のCVATとDatumaroを使う確認は`tests/itgformat/check_in_cvat.py`です。`cvatctl check`は登録とイメージの一致を全バックエンドで調べ、実依存関係の確認をAPI、import worker、export workerで実行します。

これらは、実データを使ったHTTP経由の入出力や画面操作の代わりにはなりません。実ファイルを別の試験タスクへ取り込み、出力して比較してください。

## 制限と保守対象

ITGformatでは、入力時のビット番号、画素幅、属性、追跡ID、描画順、インスタンスの区別をCVATとの往復で保持しません。許容する形状と情報損失は、[形式仕様](format_spec.md)で確認してください。

CVAT更新時は、形式の登録方法、抽出器、マスク変換、プロジェクト入力の呼出し規約を確認します。既存workerを新しい公式イメージのまま動かす構成は、起動前検査で拒否します。
