# 拡張機能の管理

[共通運用](README.md)

このフォークで追加する機能は、画面プラグインだけの場合も含め、必ず `cvatctl` の管理に組み込みます。利用者からの個別指示は不要です。機能を追加する変更と同時に、設定、起動と停止、確認処理、文書、回帰テストを更新してください。

## 管理対象

設定ファイルの `CVAT_EXTENSIONS` に、使用する拡張名をカンマ区切りで指定します。

| 拡張名 | 対象 | 管理する構成 |
| --- | --- | --- |
| `itgformat` | ITGformatの入出力 | Compose構成と登録確認。ソースに登録済みの形式自体は、選択から外しても残ります。 |
| `sam2` | SAM2の画像分割と追跡 | 画面プラグイン、Nuclio関数、専用Redis。 |
| `sam31` | SAM3.1の画像分割と追跡 | 画面プラグイン、Nuclio関数、専用Redis、配置済み重み。 |
| `ultrasam` | UltraSAMの画像分割 | Nuclio関数とGPU設定。専用の画面プラグインはありません。 |
| `model_registry` | ONNXモデル管理 | 管理画面へのプラグイン、ゲートウェイ、ローカル構成の管理サービスとワーカー。部分更新と画像分類もこの拡張に含まれます。 |
| `frame_navigation_filter` | フィルター条件によるフレーム移動と全図形表示 | `plugins/frame-navigation-filter` を共通UIイメージに含めます。推論サービスは不要です。 |
| `selective_ai` | タグ条件と画像選択による自動アノテーション | `plugins/selective-ai` を共通UIイメージに含め、推論APIに必要な既存Nuclio構成を有効にします。 |
| `filtered_export` | アノテーション条件に合う画像のエクスポート | `plugins/filtered-export` を共通UIイメージに含めます。標準のサーバーと出力ワーカーを使用し、推論サービスは不要です。 |

`selective_ai` は `model_registry` を自動選択しません。登録ONNXモデルを使う場合は両方を選択します。通常のNuclioモデルも使用できますが、画面プラグインの有効化だけでモデルを新しく配備するわけではありません。GPUも自動要求しません。推論の開始と中止は画面で行い、`cvatctl` は配備と停止を管理します。

この一覧はフォークの追加機能が対象です。upstream由来の `plugins/sam` など、この一覧にないプラグインの指定は `CVAT_CLIENT_PLUGINS`、独自のサイト設定は `CVAT_EXTRA_COMPOSE_FILES` で引き続き扱えます。今後このフォークに追加する機能を、個別パス指定だけで管理対象外にしてはいけません。

## 有効化と無効化

既存の選択を保持し、必要な拡張名を追加します。例えば、既存構成に新しい画面機能を追加する場合は次のように指定します。SAM3.1を使用している場合は `sam31` も保持してください。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry,frame_navigation_filter,selective_ai,filtered_export
```

管理対象のプラグインパスを `CVAT_CLIENT_PLUGINS` に重ねて指定する必要はありません。`cvatctl` はこの変数から管理対象のパスをいったん除き、`CVAT_EXTENSIONS` の選択に応じて再構成します。旧手順でプラグインパスだけを記載していた場合も、新しい拡張名へ変更してください。一覧にない独自のプラグインパスは保持されます。

稼働中の構成を変更する場合は、編集内容を保存し、旧構成で停止してから `.env` を編集します。リポジトリのルートからの操作例です。既存の設定ファイルを `init` で作り直してはいけません。

```sh
bash components/extensions/cvatctl down
# .env の CVAT_EXTENSIONS を編集する
bash components/extensions/cvatctl config
bash components/extensions/cvatctl up
bash components/extensions/cvatctl check
```

`up` は必要なビルドを行います。事前ビルドには `build`、必要な全イメージが既に存在する場合の再利用には `up --no-build` を使います。`--env-file` と `--state-dir` を使用している場合は、全コマンドで同じ値を指定してください。

画面機能を無効にする場合は、拡張名を選択から外して同じ停止と再ビルドの手順を実施します。設定の編集やPRのマージだけでは、配備済みのUIも開いているブラウザーも変わりません。再配備後は画面を再読み込みしてください。

`filtered_export` を外すと画面メニューが消えます。ソースに登録されたエクスポートAPIはサーバーに残り、通常の権限確認が引き続き適用されます。APIの削除まで必要な場合は、この機能を含まないサーバーと出力ワーカーへ戻してください。

## 確認コマンドの範囲

`config` と `doctor` は、選択された拡張名と、解決済みの `client_plugins`、UIイメージを表示します。選択した画面プラグインの入口ファイルがない場合や、Composeでそのビルド引数が失われた場合は失敗します。選択が変わるとUIイメージの識別子も変わります。

`status` はコンテナーの状態を表示します。画面プラグインは個別のサービスではなく `cvat_ui` に含まれます。`down` は保存された起動構成を使って停止し、データと共有ネットワークを保持します。`check` は記録済みイメージと実行中イメージなどを照合します。ブラウザーでのプラグイン登録や操作、実際の推論の成功までを保証するものではありません。

## 機能を追加するときの手順

1. [manage.py](../../components/extensions/manage.py)の `EXTENSION_COMPOSE` に拡張名を登録します。専用Composeファイルが不要な画面機能は空のタプル `()` を使用し、ダミーのサービスを作りません。
2. 画面プラグインを `EXTENSION_PLUGINS` に登録します。推論APIが必要なら `SERVERLESS_EXTENSIONS` も確認します。GPU関数は既存の `functions.py` と各機能の `build.json` へ組み込み、独立した配備経路を増やしません。
3. 既存の設定読み取り、選択に応じたビルド、起動記録、停止、確認処理を再利用します。必要な秘密情報、保存先、重み、追加サービスは事前検証し、不要な構成を自動追加しません。
4. [設定例](../../components/extensions/config.example.env)、この管理対象一覧、機能のREADMEと回帰テストを同じ変更で更新します。新しい独自プラグインを、パスの手動追記だけで管理対象外のまま提出しません。

今回の画面プラグイン管理の回帰テストは次で実行できます。

```sh
python3 -m pytest -q components/extensions/tests/test_client_plugins.py
```

構成生成、有効化と無効化、既存拡張との併用、ビルド対象、保存された起動構成による停止を確認します。Dockerの応答と無関係なモデル管理の初期化は代替実装を使用します。実配備と画面の確認は、それぞれのREADMEに記載した受入確認を別途実施してください。
