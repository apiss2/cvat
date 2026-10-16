# フレーム移動用フィルター

[アノテーション操作](README.md) / [UIプラグイン](../../cvat-ui/plugins/README.md)

アノテーションのフィルターをフレーム移動の条件として使い、条件に一致しない図形やタグも表示する機能です。例えば、特定タグのある画像へ移動しながら、その画像のマスクを編集できます。

## 有効化と操作

既存の.envのCVAT_EXTENSIONSへframe_navigation_filterを追加します。既存の機能名は保持してください。次はこの機能だけを選ぶ最小例です。

```dotenv
CVAT_EXTENSIONS=frame_navigation_filter
```

保存と停止、設定変更、再ビルドと起動は[共通管理の手順](../extensions/management.md)に従ってください。推論サービスやGPUは不要です。直接UIを開発する場合だけCLIENT_PLUGINSへplugins/frame-navigation-filterを指定します。

フィルターダイアログと移動のショートカットは従来のものを使用します。フィルターによる前後の移動は条件に合うフレームを探しますが、キャンバスとオブジェクト一覧から他のマスク、図形、タグを除外しません。
手動で非表示にしたオブジェクトは非表示のままです。新しく作成した図形も再表示時に残ります。

この動作はタグの条件だけに限定されません。有効化中のフィルター付きJob.annotations.get全体に適用します。Task API、エクスポート、一覧画面のフィルター、音声のinterval APIは対象外です。
CVAT_EXTENSIONSからframe_navigation_filterを除き、UIを再ビルドすると元の表示方式へ戻ります。

## coreとの接続

このフォークのcvat-core/src/plugins.tsにあるmethodArguments対応と一緒に使用します。未変更の上流coreへプラグインだけをコピーしても動作しません。

プラグインのenterフックは[frame, allTracks, []]を指定し、coreが通常の実装へ渡す引数だけを置き換えます。公開APIの再呼び出し、呼出元の配列変更、非公開implementationの直接実行は行いません。
引数検査、キャッシュ、トラックの補間、手動の非表示状態は元の実装を使います。annotations.searchは元のフィルターを使用します。
coreに登録解除APIがないため、UIプラグインの破棄後はフックを無効にして通過させます。

正常なAPI呼び出しでは各enterとleaveを1回ずつ実行します。フックには元の引数、実装には置換した引数を渡します。併用プラグインの登録順によって前後処理を二重実行しません。
別のフックが実行を止めるか戻り値を指定した場合は、実装を呼び出しません。preventMethodCallWithReturnがpreventMethodCallより優先される関係は維持します。
enterまたは実装で例外が起きた場合はleaveへ進まず、leaveの例外もそのまま返します。

複数のmethodArgumentsがある場合は登録順で最後の指定を使います。配列を浅くコピーし、空配列なら引数なしで実行します。省略は元の引数を保持しますが、undefinedを含む明示的な非配列の指定はエラーです。置換は呼び出しごとに独立しています。

## 検証

```bash
node --test cvat-ui/plugins/frame-navigation-filter/tests/filter.test.cjs
npx tsc --noEmit --strict --target ES2020 --lib ES2020,DOM   cvat-ui/plugins/frame-navigation-filter/src/ts/filter.ts
```

試験は実際のプラグイン登録処理を使い、表示条件、検索条件の維持、無効化、手動非表示、新規図形、両方の登録順でのフック実行回数、呼び出しの競合、戻り値の置換、例外を確認します。アノテーションの実装と例外の依存は代替実装であり、全体のビルドと描画の試験ではありません。
ブラウザーではタグとマスクがある画像で、タグによる前後移動、マスク編集、新規作成、手動非表示、保存と再読込を確認してください。
