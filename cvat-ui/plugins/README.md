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
| `filtered-export` | 条件に合う画像のエクスポート | [条件を指定したエクスポート](../../docs/annotation/filtered-export.md) |
| `physical-measurements` | DicomTagのPixelSpacingに基づく折れ線の物理長表示 | [物理長の表示](../../docs/annotation/physical-measurements.md) |
| `sam` | 上流CVATのSAM用クライアント処理 | [SAMプラグイン](sam/README.md) |

独自プラグインは共通設定のCVAT_EXTENSIONSで選びます。
画面機能の拡張名はframe_navigation_filter、selective_ai、filtered_export、physical_measurementsです。
管理コマンドが対応するパスをUIビルドへ追加するため、CVAT_CLIENT_PLUGINSへの手動追記は不要です。
既存のSAMやモデル管理の選択は保持してください。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,model_registry,frame_navigation_filter,selective_ai,filtered_export,physical_measurements
```

選択の変更と機能追加時の開発方針は[拡張機能の管理](../../docs/extensions/management.md)を参照してください。一覧にない独自パスは引き続きCVAT_CLIENT_PLUGINSで指定できます。

変更後は、共通の更新手順に従ってUIをビルドし直して起動します。マージや設定ファイルの編集だけでは、配備済みの画面に反映されません。直接UIを開発する場合のビルド変数は`CLIENT_PLUGINS`です。

`frame-navigation-filter`は、このフォークの`methodArguments`対応を含むcoreと組み合わせて使用します。未変更の上流CVATへプラグインだけをコピーする構成は対象外です。
プラグインを無効にするときも、CVAT_EXTENSIONSから該当の拡張名を除き、UIを再ビルドしてください。
