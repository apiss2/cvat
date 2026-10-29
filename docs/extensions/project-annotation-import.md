# Projectへのアノテーション取り込み

[拡張機能の管理](management.md)

Projectの `Import annotations` は、既存のTaskへアノテーションを追加または置換します。Taskや画像を新しく作成せず、通常形式ではTask名、DICOM形式では保存済みのDicomTagを使って更新先を決定します。

## 有効化と操作

既存の `.env` の `CVAT_EXTENSIONS` に `project_annotations` を追加し、共通の更新手順でサーバー、取り込みワーカー、UIを同じ版へ更新します。既存の拡張名は保持してください。

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry,project_annotations
```

この拡張は既存のサーバーと取り込みワーカーを使用します。追加サービス、GPU、DBマイグレーションは不要です。`dicom_import` は独立した拡張なので、保存済みのDicomTagがある場合は無効でも照合できます。

画面ではProjectの操作メニューから `Import annotations` を開き、形式、`replace` または `append`、ZIPファイルを指定します。アップロードの受付後は標準のRequestsで処理状態を確認でき、完了時に成功Task数、失敗Task数、未一致レコード数が通知されます。

無効化する場合は `project_annotations` を選択から外して再配備します。保存済みのアノテーション、画像、DicomTag、AnnotationGuideは保持されます。`config`、`doctor`、`build`、`up`、`down`、`status`、`check` は共通管理に従い、取り込みを自動実行しません。

## 通常形式のZIP

通常のアノテーション形式には既存のインポータを使用します。ZIP内の直下にTask名と完全一致するフォルダを置き、その中へ形式が要求するファイルと内部構造をそのまま格納します。画像名とフレームの対応付けも既存インポータが行います。

インポータが単独のXML、JSON、TSVなどを受け取る場合は、そのファイルをTaskフォルダに入れます。既存形式のZIPをTaskフォルダに1個だけ入れる方法も使用できます。形式用のZIPも同じ安全性検証と合計サイズ制限の対象で、さらにZIPを入れ子にする構成は受け付けません。

例えば、Task `scan_A` と `scan_B` にCVAT形式を取り込む場合、ZIPには次のファイルを格納します。

```text
scan_A/annotations.xml
scan_B/annotations.xml
```

Windowsで親フォルダごと圧縮した場合は、次のような共通親フォルダを最大1階層だけ除外できます。親フォルダ名がZIPファイル名と一致する必要はありません。

```text
exported/scan_A/annotations.xml
exported/scan_B/annotations.xml
```

照合では現在の階層のTask名一致を優先します。例えば `exported` 自体がTask名なら、そのフォルダをTaskの入力として扱い、内部を別のTask名として推測しません。2階層以上の親フォルダの自動除外、部分一致、大文字小文字の補正、Task名の推測は行いません。同名Taskが複数ある場合や照合が曖昧な場合、その候補Taskを更新しません。

通常のProject `Import dataset` とTask `Import annotations` は従来どおり使用します。Task Guide形式は従来のProject `Import dataset` で扱い、新しい `Import annotations` の対象外です。DICOMの画像と動画そのものを作成する `DICOM 1.0` も対象外です。

## replaceとappendの範囲

更新方法は次のとおりです。

| 方法 | 通常形式 | Annotation Tags DICOM Import |
| --- | --- | --- |
| `append` | 既存のTask用Import annotationsと同じ追加処理を使います。既存アノテーションを保持し、Tagの重複も標準仕様に従います。 | 既存アノテーションを保持してTagを追加します。同じフレームの同じラベルのTagは、既存Tagとの重複も含めてエラーにします。 |
| `replace` | 対象Task内のDicomTag以外のアノテーション全体を削除し、入力内容を登録します。 | 対象Task内のDicomTag以外のアノテーション全体を削除し、そのTaskに対応する全レコードのTagをまとめて登録します。 |

`replace` はTask全体の置換です。入力に含まれない画像のアノテーションも削除します。DICOM形式でも既存のPolygon、Mask、Rectangle、Trackなどは置換対象です。この範囲は画面で `replace` を選ぶ際に表示します。

DicomTagとそのAttributeは、両形式の `replace` と `append` で保持します。通常形式の入力に含まれるDicomTagは取り込みから除外します。DICOM形式で `tags[].label` に `DicomTag` を指定した場合は、そのTaskの取り込みをエラーにします。

再取り込みでは、`replace` は再びTask全体を置換します。通常形式の `append` は標準のTask取り込みと同様に追加します。DICOM形式の `append` では前回登録したTagとの重複が検出され、そのTaskを変更しません。

## Annotation Tags DICOM Import 1.0

この形式では、既存のDicomTagから対象Taskと画像を照合し、Tagアノテーションだけを登録します。`sample.json` などのJSONファイル1個をZIPに格納してアップロードします。通常形式で使用するTask名のフォルダは不要です。

```json
{
  "records": [
    {
      "study_instance_uid": "1.2.3",
      "sop_instance_uid": "1.2.3.4",
      "patient_id": "P001",
      "study_date": "20250101",
      "tags": [
        {
          "label": "ImageQuality",
          "attributes": {
            "quality": "good",
            "contact": "true"
          }
        }
      ]
    },
    {
      "study_instance_uid": "1.2.3",
      "sop_instance_uid": "1.2.3.5",
      "tags": [
        {
          "label": "ImageQuality",
          "attributes": {
            "quality": "poor",
            "contact": "false"
          }
        }
      ]
    }
  ]
}
```

各レコードの `study_instance_uid`、`sop_instance_uid`、`tags` は必須です。ラベル名とAttribute名はProjectに存在する名前を完全一致で指定します。存在しないラベルやAttributeを自動作成しません。ラベルの種類とAttributeの値も登録前に検証します。

識別情報では次の別名を受け付けます。同じ項目を複数の別名で指定した場合は正規化後の値が一致している必要があります。

| 項目 | 受け付ける名前 | 用途 |
| --- | --- | --- |
| StudyInstanceUID | `study_instance_uid`、`study_uid`、`uid`、`StudyInstanceUID` | 必須。完全一致で更新先を照合します。 |
| SOPInstanceUID | `sop_instance_uid`、`sop_uid`、`SOPInstanceUID` | 必須。完全一致で画像を照合します。 |
| PatientID | `patient_id`、`pid`、`PatientID` | 任意。指定時は保存済みの値との整合性を検証します。 |
| StudyDate | `study_date`、`date`、`dt`、`StudyDate` | 任意。指定時は日付を正規化して整合性を検証します。 |
| SeriesInstanceUID | `series_instance_uid`、`series_uid`、`SeriesInstanceUID` | 任意。指定時は保存済みの値との整合性を検証します。 |

識別子の表記と日付の正規化は[Task Guide DICOM](task-guide-import.md)の共通処理を再利用します。PatientIDの接頭辞を取り除いたり、Task名から識別情報を推測したりしません。StudyDateは `20250101`、`2025-01-01` などの対応形式を同じ暦日へ正規化し、時差による日付変更を行いません。

画像の照合にはStudyInstanceUIDとSOPInstanceUIDの両方を使用します。UIDの一部だけが一致する場合や同じ患者と日付だけの場合は更新しません。対応画像が存在しないレコードは未一致として報告します。

照合結果が複数のTaskまたは異なる静止画にまたがる場合は曖昧な照合として拒否し、候補Taskを変更しません。動画では、同じSOPInstanceUIDを持つ同一動画内の複数フレームを一つの対象として扱い、全フレームに同じTagを適用します。フレーム番号をJSONへ指定する必要はありません。保存された動画のDicomTagに欠落や矛盾がある場合は更新しません。

同じ画像を複数のレコードで指定することはできますが、同一フレームに同じラベルのTagが重複するとエラーです。`append` は既存Tagも含めて検査します。`replace` は置換後に残るTagについて検査し、削除される既存Tagとの重複はエラーにしません。

## 検証と失敗時の動作

取り込みはTask単位で確定します。対象Task、画像、ラベル、Attribute、重複を検証してから更新し、一つのTaskに属するレコードの一部だけが誤っている場合も、そのTask全体を変更しません。別のTaskの処理は継続します。

DB更新はTaskごとのトランザクションで保護します。DICOM形式の `replace` は照合と検証を終えてからTaskごとに全Tagをまとめ、削除と登録を同じトランザクションで一度ずつ行います。途中で登録に失敗した場合は、そのTaskで行った削除もロールバックします。

通知の成功件数と失敗件数はTask数です。未一致件数は、通常形式ではTaskフォルダ数、DICOM形式ではレコード数です。Taskを特定できない入力エラーも別に表示します。有効なUIDに対応する画像がなく、Tagの指定にも不備がある場合は、未一致と入力エラーの両方に数えます。複数レコードが同じTaskに対応する場合も、Task件数は一度だけ数えます。通知と取り込み処理のエラーには患者ID、UID、Task名、入力Attribute値を不用意に含めません。

ZIPのパス、ファイル種別、暗号化、重複、展開後の大きさを検証します。絶対パス、親ディレクトリへの参照、シンボリックリンクなどを拒否します。アップロードした外側のZIPや、DICOM形式のrecords JSON全体の構造エラーは、アノテーションを変更する前に取り込み全体を失敗させます。通常形式の各Taskフォルダに入れた形式用ZIPやJSONなどの不備は、そのTaskだけを失敗させます。

通常形式の上限はアップロード512 MiB、展開後の合計2 GiB、100,000エントリー、Taskごとの読み込み済みアノテーション500,000個です。Taskフォルダ内の形式用ZIPも展開後サイズとエントリー数の合計に含めます。DICOM形式はTask Guideの共通ZIP読み込みを使用し、ZIPと展開後のJSONは32 MiB、ZIPは50,000エントリー、JSONは50,000レコードまでです。動画の全フレームへ展開した後の入力Tagは250,000個、Attribute値は1,000,000個までです。

## 実装と開発規約

独自の処理は `components/project_annotations`、画面は `cvat-ui/plugins/project-annotations` に置きます。標準のTask用インポータと部分削除を再利用し、DicomTag保護はProject用の独立した更新処理で行います。標準のTask用Import annotationsへDicomTagの例外処理を追加しません。

新しい入口はProjectのアノテーションアップロードです。既存のアップロード処理、取り込みキュー、Requestsの監視を使用します。

```text
POST /api/projects/{project_id}/annotations
Content-Type: multipart/form-data

annotation_file: ZIPファイル
format: クエリで指定する既存形式名またはAnnotation Tags DICOM Import 1.0
import_mode: クエリで指定するreplaceまたはappend
location: local
```

本体の接続箇所はアップロード入口、権限確認、形式の登録などに限定します。新しい永続テーブルや専用キューを作る方式は採用せず、既存のアノテーションモデルへ保存します。独自拡張のためのDBスキーマ変更、マイグレーションの追加、既存モデルの構造変更は完全に禁止します。

## 回帰テストと実環境の確認

追加したPythonテストは次で実行できます。

```sh
python3 -m pytest -q components/project_annotations/tests
```

拡張管理のテストは実際のManagerを使用し、Dockerの応答を代替実装に置き換えています。選択の有効化と解除、DICOM取り込みとの独立性、Composeの選択、`config` と `doctor` の表示、共通イメージの `build` と `up --no-build`、記録された構成による `down` と `status`、`check` のイメージ照合を検証します。データ削除、モデル配備、アノテーション処理を配備コマンドから開始しないことも確認します。

通常のCVAT用Python依存と `fakeredis` がある環境では、実際のモデルとインポータを使う次の検証も実行できます。DBと作業ファイルは一時領域に作成し、リポジトリのモデルやマイグレーションを変更しません。

```sh
python3 components/project_annotations/tests/run_orm_tests.py
```

この検証は実際のCVATモデル、CVAT 1.1インポータ、TaskData、シリアライザ、アノテーションの書き込み処理、SQLiteのトランザクションを使用します。DicomTagとAttributeの行ID維持、入力外フレームの削除、重複するセグメント内の削除、書き込み後の例外による削除と登録のロールバック、無関係なTaskの成功を確認します。DICOM形式では動画の全フレーム、曖昧な照合、同じTask内の不正レコード、再取り込みを確認します。標準のTask取り込みについても、replaceでDicomTagを削除し、appendで重複Tagを追加する従来動作を確認します。

リクエスト管理は実際のProjectとアップロード用ファイルオブジェクトを使い、`annotation_file` の受け取りと一時保存、モードの解析、コールバック引数、キュー投入前の無効なモード・対象外形式・無効化設定の拒否、自動リトライの無効化、RQ IDの生成と解析を確認します。既存のProject Import datasetが引き続き `dataset_file` と `import_dataset_as_project` を使うことも確認します。これらはHTTPサーバーを経由せず、リクエスト管理クラスを直接実行する検証です。

この検証のOPAの判断と実行中ワーカーの取得は代替実装で、RQメタデータのRedis保存先には `fakeredis` を使用します。配備時の起動シグナル、HTTPアップロード、実Redisワーカー、PostgreSQLの行ロックによる同時更新制御は対象外です。

実環境ではDockerビルドと再配備後に、通常形式とDICOM形式それぞれで `replace`、`append`、DicomTagとAttributeの維持、同名Task、Tag重複、別Taskの部分成功、同一Taskのロールバック、再取り込みを確認してください。実際のPostgreSQL、OPA、RQ、ブラウザーを接続した検証は、代替実装を使用するテストと区別して記録してください。
