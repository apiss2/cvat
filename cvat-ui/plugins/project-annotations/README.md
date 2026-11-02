# Project Import annotations

Projectのメニューに `Import annotations` を追加します。既存形式のTask別ZIPと `Annotation Tags DICOM Import 1.0` を受け付けます。

`CVAT_EXTENSIONS` の `project_annotations` でサーバー側と画面側を一緒に管理します。操作と入力形式は [Projectへのアノテーション取り込み](../../../docs/extensions/project-annotation-import.md) を参照してください。

初期値は `append` です。`replace` を選択した場合は、入力に含まれない画像を含め、対象Task全体のDicomTag以外のアノテーションを削除することを画面上に表示します。取り込み後の通知はTaskの成功数と失敗数、未一致数と入力不備数だけを表示します。通常形式の未一致はTaskフォルダ数、DICOM形式の未一致はレコード数です。

送信には既存の認証付きHTTPクライアントを使い、進捗は標準のRequestsに反映します。アップロードの自動再送は行いません。応答を確認できない場合は、その画面での再送を止めてRequestsの確認を案内します。アカウントや組織を切り替えた場合はダイアログを閉じ、前のアカウントの結果を通知しません。

回帰テストはTypeScriptをインストールした環境で実行します。

```sh
node --test cvat-ui/plugins/project-annotations/tests/*.test.cjs
```
