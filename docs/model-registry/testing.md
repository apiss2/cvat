# モデル管理の検証

[モデル管理の案内](README.md)

このページは変更後の検証担当者向けです。以下は実行手順であり、この文書の存在だけで対象環境の動作を保証しません。自動試験、実サービス、実ブラウザー、認識精度を区別して記録します。

## 自動試験

リポジトリのJavaScript依存関係と、[Python試験用の依存関係](../../components/model_registry/tests/requirements.txt)を準備します。実行場所はチェックアウトのルートです。

```bash
PYTHONPATH=components/model_registry python -m pytest components/model_registry/tests/test_api.py components/model_registry/tests/test_deletion.py components/model_registry/tests/test_runtime.py components/model_registry/tests/test_cli.py -q
PYTHONPATH=components/model_registry python -m pytest components/model_registry/tests/test_classification.py -q
python -m pytest --noconftest components/model_registry/tests/test_partial_updates.py -q
python -m pytest --noconftest components/extensions/tests/test_registration.py -q
node --test components/model_registry/tests/classification-ui.test.cjs components/model_registry/tests/ui-copy.test.cjs
node --check components/model_registry/static/app.js
```

Classificationの試験は出力と独立した応答検査、旧出力形式、内部パッケージ、ワーカーのHTTP処理を確認します。通常のONNXセッションは代替実装を使い、onnxとonnxruntimeがある場合だけIdentityモデルの実行試験も動きます。部分更新の試験は経路と候補の組み立てを確認しますが、認証やサービスを代替しています。

削除の試験は公開済みと未公開のモデル、所有者と管理者、全利用者の一覧からの除外を確認します。実ファイルを使い、推論、版の切り替え、更新のコピー・展開・検証、実行待ちの登録と削除を重ね、候補ファイルの残留や再公開を防ぐことも確認します。ワーカー停止・清掃の失敗、再試行、再起動、削除・清掃ログと最小限の記録の保持を含みます。CVAT認証とDockerは代替実装です。JavaScriptの試験は実際の登録、更新、削除のハンドラーをDOMの代替実装で呼び出します。実ブラウザーの描画や実CVAT認証の確認ではありません。接続試験用の作例は[examples](../../components/model_registry/examples/)にあり、学習済み認識モデルではありません。generate.pyで重み、画像、内部パッケージ検査用ZIPを再生成できますが、登録画面には個別ファイルを指定します。

## 公開前の受入確認

所有者A、別の一般ユーザーB、管理者を用意し、業務データとは分離したモデルとタスクで確認します。

| 対象 | 確認内容 |
|---|---|
| 公開URLと認証 | CVATから再ログインなしに一覧を開き、ログアウトと期限切れ後は認証が必要になることを確認します。 |
| 権限 | BはAの公開モデルを実行でき、更新、削除、試験、ログを操作できないことを確認します。 |
| 操作要求 | 不一致のOriginやX-Registry-Requestがない操作要求を拒否します。 |
| 3種類の登録 | Segmentation、Detection、Classificationを登録し、図形またはタグと信頼度を確認します。 |
| 出力 | 実画像の輪郭、穴、小領域、ラベル順、元の画像座標、分類の意味を確認します。 |
| 部分更新 | 設定、コード、試験画像、個別ONNXだけの更新で他の値を保持し、失敗時は公開中の版を維持します。 |
| CVATからの実行 | 単一画像と一括処理でラベルを対応付け、図形とタグを保存して再読込します。 |
| 版と削除 | 更新後の選び直し、旧版の実行、公開版の切り替え、削除後の新規推論拒否を確認します。削除後は一覧に戻り、全利用者の一覧と件数から消え、全版の保存ファイル、ワーカー用ディレクトリ、更新用の一時ファイルと版情報が残らないことを確認します。推論・更新中の削除と清掃失敗後の再試行も確認します。 |
| 配備と停止 | 管理サーバーとワーカーのイメージが揃い、downと再起動後もモデルと権限を保持します。 |
| 保存先 | モデルと秘密情報がGitとDockerのビルド対象に含まれません。 |
| 接続障害 | 接続エラーをセッション失効と区別し、認証をやり直すだけで隠しません。 |
| 併用 | 有効なITGformat、SAM2、SAM3.1、UltraSAM、選択実行、フィルターを回帰確認します。 |

## 実サービスの接続試験

専用の試験用CVATアカウントで次を実行します。パスワードは実行時に入力し、引数に書きません。

```bash
python3 components/model_registry/tests/smoke_registry.py \
  --url https://cvat.example.internal/model-registry/ \
  --username ACTUAL_CVAT_TEST_USERNAME --run
```

このスクリプトは2個の試験モデルを登録し、推論、更新、旧版の実行、公開版の切り替えと削除を行います。CVATのアノテーションは変更しません。成功した削除では保存ファイルを消去し、削除・清掃ログと最小限の記録だけを残します。

CVATの標準ログインAPIで取得したCookieを使用し、パスワードやトークンを管理サーバーへ渡さず、Cookieをファイルへ保存しません。終了時の標準ログアウトは同じアカウントの既存RESTトークンも無効にする場合があります。日常業務や連携に使うアカウントで実行しないでください。

[CVAT側の確認スクリプト](../../components/model_registry/tests/verify_in_cvat.py)は、CVATサーバーと自動アノテーション用ワーカーのPythonで実行します。必要に応じて--require-sam2と--require-registry-modelを指定します。これは設定と関数一覧の確認であり、実推論やブラウザー操作の代わりにはなりません。
