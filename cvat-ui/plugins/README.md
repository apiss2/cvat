# UIプラグインの案内

[ドキュメントの入口](../../docs/README.md) / [共通の導入手順](../../docs/extensions/installation.md)

各プラグインは、このフォークの既存の登録機構を通じてCVATの画面や操作を拡張します。以下はプラグインの名前と、その操作を説明する正本です。

| プラグイン | 役割 | 説明 |
|---|---|---|
| `model-registry` | モデル管理へのリンクとモデル一覧の更新 | [モデル管理](../../docs/model-registry/README.md) |
| `sam2` | SAM2の追跡操作 | [共通の追跡操作](../../docs/annotation/tracking.md) |
| `sam31` | SAM3.1の追跡操作 | [共通の追跡操作](../../docs/annotation/tracking.md) |
| `frame-navigation-filter` | 表示を隠さず、フィルターをフレーム移動に使う | [フィルター](../../docs/annotation/frame-navigation-filter.md) |
| `selective-ai` | タグ条件と画像選択による自動アノテーション | [選択実行](../../docs/annotation/selective-ai.md) |
| `sam` | 上流CVATのSAM用クライアント処理 | [SAMプラグイン](sam/README.md) |

`sam2`、`sam31`、`model-registry`は、共通設定の`CVAT_EXTENSIONS`に対応する機能を指定すると、管理コマンドがUIビルドに追加します。
`frame-navigation-filter`と`selective-ai`は、既存の`CVAT_CLIENT_PLUGINS`へパスをコロンで区切って追加します。既存値は消さないでください。

```dotenv
CVAT_CLIENT_PLUGINS=plugins/frame-navigation-filter:plugins/selective-ai
```

変更後は、共通の更新手順に従ってUIをビルドし直して起動します。マージや設定ファイルの編集だけでは、配備済みの画面に反映されません。直接UIを開発する場合のビルド変数は`CLIENT_PLUGINS`です。

`frame-navigation-filter`は、このフォークの`methodArguments`対応を含むcoreと組み合わせて使用します。未変更の上流CVATへプラグインだけをコピーする構成は対象外です。
プラグインを無効にするときも、設定から該当パスを除き、UIを再ビルドしてください。
