# アノテーション条件による画像のエクスポート

タスクとプロジェクトのActionsに「条件を指定してエクスポート」を追加します。表示フィルターと同じQuery BuilderとJsonLogicの表現を使い、条件に合うアノテーションがある画像を選択します。表示フィルターの状態は変更しません。

## 条件の指定

編集内容を保存してからActionsを開き、ObjectsでLabel、Type、Shape、Attributesなどの条件を組み合わせます。AND、OR、NOT、等値比較、大小比較、範囲比較、部分一致、候補リストとの一致を使用できます。Skeletonがある場合はElementsでキーポイントのLabel、Occluded、Attributesを指定できます。

例えばTypeがtag、かつLabelがreadyという条件は、readyタグがある画像を選択します。その画像にある別のタグ、矩形、Segmentation、Trackは、選択条件に合わなくても出力側に渡します。フィルターは画像の選択だけを担当し、元データも出力用アノテーションも削除しません。

条件は表示フィルターと同じく、一つのアノテーションに対して評価します。LabelがA、かつLabelがBという条件は、AとBの両方が付いた画像を選ぶ意味ではありません。またLabelがAではないという条件は、A以外のアノテーションが一つでもある画像を選びます。Aタグが存在しない画像という意味ではありません。Elementsの条件はObjectsに一致した同じSkeletonのキーポイントに適用します。

空の条件は、アノテーションのない画像も含めて、削除済みフレームを除くすべての有効画像を選択します。入力途中のルールを無視して対象を広げることはせず、条件が完成するまで件数確認と出力を無効にします。

対応フィールドはLabel、Type、Shape、Occluded、Source、Width、Height、Rotation、ServerID、Score、Z order、Attributesです。ブラウザー内だけで割り当てるObjectIDと、一時的なVotesは対象外です。Trackの形状、可変属性、Occludedなどは対象フレームでの補間結果で評価します。独自のフレーム範囲、抽出間隔、タグ一致モードはありません。

## 件数確認と出力

「対象件数を確認」でタスクごとの件数を確認し、出力形式と画像同梱の有無を指定してエクスポートします。プレビュー処理には出力形式を渡しません。条件を変えるとプレビューを破棄しますが、出力形式や画像同梱の変更では画像の選択結果は変わりません。完了確認とダウンロードには既存のRequests画面を使用します。

選択条件の評価と形式変換は独立しています。各エクスポーターには、選択フレームと加工していないアノテーションを渡します。タグ削除の選択肢、ITGformatだけに対するタグ除外の強制、分類ラベルをフィルター側で自動削除する処理はありません。

保存できる情報と未対応データの扱いは通常の各形式の仕様に従います。例えば現在のITGformatエクスポーターは分類タグを受け取るとエラーにします。このPRはその仕様を変えず、エラーを回避するためのタグ削除も行いません。通常のITGformatの動作変更は画像フィルターとは別の変更になります。

## 有効化

ルートの`.env`の`CVAT_CLIENT_PLUGINS`に、`plugins/filtered-export`をコロン区切りで追加します。既存の指定は残してください。例えば既存のUI拡張との併用時は以下です。

```dotenv
CVAT_CLIENT_PLUGINS=plugins/frame-navigation-filter:plugins/selective-ai:plugins/filtered-export
```

サーバー、UI、エクスポートワーカーを同じコミットから再作成します。既存の拡張管理手順を使用します。

```sh
./cvatctl build
./cvatctl up
```

UIの直接開発では`CLIENT_PLUGINS`を指定します。マージだけでは実行環境に反映されません。プラグイン指定を外してUIを再ビルドするとメニューは消えますが、新APIはサーバーに残ります。APIも除去する場合はこの変更を含まないサーバーとワーカーに戻します。

## 実装の分離

既存ファイルの変更は`cvat/urls.py`のAPI登録1行です。新しい処理は`cvat/apps/dataset_manager/filtered_export/`とこのUIプラグインに分離しています。既存の表示フィルター、形式変換、権限定義、キュー管理は変更しません。DB変更、追加パッケージ、追加サービスはありません。

`selection.py`と`logic.py`は形式名を扱いません。表示フィルターのフィールド名、属性の型変換、ドットを含む名前の扱いを踏襲し、Query Builderが生成する比較と論理式を評価します。受け入れる演算子、フィールド、式の深さと大きさを制限し、Pythonのevalや任意関数は使用しません。

サービスは選択時に既存のAnnotationManagerからフレームごとの形状を取得し、出力時には同じrepeatable-readトランザクション内で新しいアノテーションストリームを読み直します。これにより選択で消費したストリームを出力に再利用せず、タグやTrackのキーフレームも保持します。出力対象は既存のTaskData.included_framesで制限します。プロジェクトのファイル名重複回避、subset、フレーム番号変換は既存のProjectDataに任せます。

プレビュー結果のリビジョンには条件と保存データの状態を含め、受付時とワーカー開始時に再検証します。タグや形状、ラベル属性、タスク構成、削除フレームなどが変わった場合は再プレビューが必要です。更新日時はCVATの通常の更新経路を前提とし、DBの直接編集は対象外です。

プレビュー、受付、ダウンロードには既存のexportアクションとTaskPermissionまたはProjectPermissionを使い、各処理でget_objectによる権限確認を行います。ダウンロードは既存の所有者確認に加えてリクエストの利用者IDを検証します。ブラウザーでは組織を固定し、利用者や組織の変更でダイアログを破棄します。POSTは自動再送せず、応答が失われた場合はRequestsで確認するよう表示します。

通常出力と条件ごとの出力はリクエストIDとキャッシュを分離します。既存のID構造を変更しないため、ID内の形式名にfilteredとSHA-256を付けます。Requestsの形式欄にも識別子が表示されますが、実際のエクスポーターには通常の形式名を渡します。

## 対応範囲

2D画像と動画のタスク、および一つのプロジェクトを対象にします。ジョブ単位、複数プロジェクト、音声、3D、GT-pool、クラウドストレージへの直接出力、個別画像の手動選択には対応しません。プロジェクトのメディア未設定タスクは通常出力と同じく対象外とし、メディアのある非対応タスクが混在する場合はエラーにします。

利用できる形式は、選択画像を扱う既存経路に対応するCVAT for images 1.1、COCO 1.0、YOLO 1.1、Ultralytics YOLOのDetection、Segmentation、Oriented Bounding Boxes、Poseの各1.0、およびITGformat 1.0です。この一覧は画像選択アダプターの対応範囲であり、形式ごとのアノテーション削除規則ではありません。Track全体を直接読むCVAT for videoは対象外です。動画は画像単位で出力します。

プレビューは同期処理です。大規模な動画やプロジェクトでは、Trackの補間と条件評価がHTTPタイムアウトになる可能性があります。出力本体は既存のエクスポートキューで動きます。受け付け済みの出力はブラウザーを閉じても取り消されません。

## 拡張API

以下のtasksはprojectsにも置き換えられます。既存の公開SDKと生成OpenAPIスキーマは変更しません。

```text
GET  /api/filtered-export/tasks/{id}/options
POST /api/filtered-export/tasks/{id}/preview
POST /api/filtered-export/tasks/{id}/export
GET  /api/filtered-export/tasks/{id}/download?rq_id=...
```

プレビュー本文はfiltersだけです。表示フィルターと同じく、先頭がObjectsのJsonLogic、2番目が任意のElementsのJsonLogicです。空配列は条件なしを意味します。通常の組織指定に加えて、既存の権限処理がデフォルトのクラウド出力先を参照しないよう`location=local`を指定します。

```json
{
  "filters": [{
    "and": [
      {"==": [{"var": "type"}, "tag"]},
      {"==": [{"var": "label"}, "ready"]}
    ]
  }]
}
```

出力時の本文には、同じfiltersに加えて、プレビュー応答のrevisionをexpected_revisionとして渡します。出力のクエリにだけ`format`と`save_images`を指定し、`location=local`と組織指定を付けます。旧版のfilter、mode、tag_ids、start、stop、step、omit_tagsは受け付けません。無視して対象を広げず、入力エラーを返します。

出力受付は202とrq_idを返します。同じ処理が進行中の場合は409と既存のrq_idを返します。状況確認には既存の`/api/requests/{rq_id}`を使用します。

## 検証

```sh
python -m unittest discover -s tests/filtered_export -v
node --test cvat-ui/plugins/filtered-export/tests/*.test.cjs
npx tsc --noEmit --strict --target es2022 --lib es2022,dom \
  cvat-ui/plugins/filtered-export/src/ts/api.ts
python -m compileall -q cvat/apps/dataset_manager/filtered_export
```

Pythonの純粋な条件評価、画像選択、形式への未加工の受け渡し、ストリーム再読込み、変更検出、旧指定の拒否をテストします。JavaScriptの比較演算との照合にはNode.jsを使い、未導入時はその2件だけスキップします。NodeのテストはAPI、条件フィールド、入力途中のルール、メニュー登録、利用者変更を検証します。

サービスとリクエスト管理の単体テストはDB、補間、形式変換、IAM、Redisの境界を代替実装に置き換えています。CVAT全体のビルド、実ブラウザー、実DB、Redis、OPA、ネイティブ形式変換を組み合わせた検証は未実施です。API以外のTSXは構文検査であり、依存関係を含む型検査ではありません。

レビュー環境では、タグとMaskが同じ画像にある例、属性の条件、ORとNOT、条件に合うTrack補間フレームを用意し、プレビューと実際の出力画像を照合します。選択画像の別アノテーションが各形式の通常仕様に従って渡ること、ITGformatの通常エラーがそのまま返ることも確認対象です。さらに、削除フレーム、取り込み時に間引いた動画、複数subsetと同名画像、権限拒否、プレビュー後の編集、通常出力との同時実行を確認します。
