# 独自拡張のドキュメント

このフォークに追加した機能の入口です。CVAT本体の一般的な操作ではなく、独自拡張の導入、利用、運用、開発を説明します。
コマンドは、特記がなければ完全なCVATチェックアウトのルートで実行します。

## 初めて利用する場合

環境を構築する管理者は[共通の導入手順](extensions/installation.md)から始め、必要な機能を選びます。
導入済みの環境を使うメンバーは、次の表から目的の操作を選んでください。
モデルを作成する技術者は[ONNXモデルの実装仕様](model-registry/model-development.md)を参照します。

| 目的 | 読む文書 |
|---|---|
| ONNXモデルを登録、更新してCVATで使う | [モデル管理](model-registry/README.md) |
| タグ条件で画像を選んで自動アノテーションを実行する | [選択した画像への自動アノテーション](annotation/selective-ai.md) |
| アノテーション条件に合う画像をエクスポートする | [条件を指定したエクスポート](annotation/filtered-export.md) |
| DicomTagのPixelSpacingからpolylineの長さをmmで表示する | [画像の物理長を表示する](annotation/physical-measurements.md) |
| フィルターでフレームを移動し、他の図形も表示する | [フレーム移動用フィルター](annotation/frame-navigation-filter.md) |
| SAM2またはSAM3.1でポリゴンやマスクを追跡する | [共通の追跡操作](annotation/tracking.md) |
| SAM2を導入して画像にマスクを作成する | [SAM2](sam2/README.md) |
| SAM3.1を導入して画像にマスクを作成する | [SAM3.1](sam31/README.md) |
| UltraSAMで超音波画像をアノテーションする | [UltraSAM](ultrasam/README.md) |
| ITGformatを読み書きする | [ITGformat](itgformat/README.md) |

## 管理者が読む文書

[共通運用](extensions/README.md)に、導入、モデルの選択、更新、バックアップと復元をまとめています。
モデル管理サービス固有の接続設定は[配備](model-registry/deployment.md)、資源制限や障害調査は[保守](model-registry/operations.md)を参照してください。
サービスの構成確認、単体試験、実GPU推論、ブラウザーでの受入確認は別の検査です。短い試験の成功を実データでの精度や長時間実行の保証とは扱いません。

## コードから探す場合

[componentsの案内](../components/README.md)と[UIプラグインの案内](../cvat-ui/plugins/README.md)から、実装と説明を辿れます。
各実装ディレクトリのREADMEは概要と文書への入口に留め、操作や仕様の正本はこの`docs/`配下で管理します。

## 文書を追加、更新する場合

本文と見出しは日本語で記述します。Segmentation、Detection、Classification、ONNX、ROIなどの標準用語、画面上の名称、コマンド、API、設定キーはそのまま使います。ライセンス本文と第三者の著作権表示は翻訳で置き換えません。

一つの文書では一つの読者の目的を扱います。小規模な機能は操作と制限を一つにまとめ、モデル管理のように長くなる機能だけ、利用、実装仕様、配備、保守、設計、検証に分けます。機能追加のたびに追記専用の文書を増やさず、該当する章を更新してください。

共通の手順を複製せず、正本へリンクします。各ページには上位の案内を置き、移動時は相対リンクと見出しへのリンクを確認します。実装上の理由や変更履歴は操作手順に混ぜず、設計文書またはGitの履歴で管理します。
